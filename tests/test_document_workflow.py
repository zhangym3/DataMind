"""Document + uploaded rows compute directly, without global publication UI."""
import json
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook

from insight_agent.core import AppError, sha
from insight_agent.service import AppService
from insight_agent.worker import process_job, run_once
from test_integration import prepared  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class DocumentGateway:
    def __init__(self, *, skip=False, invalid=False):
        self.skip, self.invalid = skip, invalid

    def call(self, stage, payload, schema, budget_id):
        assert stage == "definition"
        rule = json.loads((ROOT / "docs/contracts/examples/surgery-delay-count.json").read_text(encoding="utf-8"))
        node = payload["nodes"][0]
        rule["sourceEvidence"] = [{"evidenceId": "ev_delay", "documentSha256": node["documentSha256"], "locator": node["locator"],
                                   "quote": node["text"], "quoteSha256": node["quoteSha256"]}]
        if self.invalid:
            rule["sourceEvidence"][0]["quoteSha256"] = sha(b"invented evidence")
        candidate = {"id": rule["metricId"], "name": rule["name"], "status": "CLEAR", "rule": rule, "locators": [node["locator"]]}
        if self.skip:
            candidate.update(status="SKIP", reason="说明未明确缺失值处理政策")
            candidate.pop("rule")
        coverage = [{"locator": n["locator"], "disposition": "CANDIDATE", "candidateIds": [rule["metricId"]], "reason": "逐节点解析"} for n in payload["nodes"]]
        return {"candidates": [candidate], "coverage": coverage}


@pytest.fixture
def uploaded(tmp_path):
    service = AppService(tmp_path / "workspace")
    session = service.create_session()
    document = Document()
    document.add_paragraph("统计口径：以 surgery_event_id 唯一的手术事件计数，按 operation_date 归属。入室至麻醉超过20分钟计延迟，完整日期时间计算跨午夜时差；缺失、非法值和重复键不计算，空集为0。")
    document.add_paragraph("分母：当前上传数据的全部手术事件。")
    word = tmp_path / "指标说明.docx"
    document.save(word)
    book = Workbook()
    sheet = book.active
    sheet.title = "事件"
    sheet.append(["surgery_event_id", "operation_date", "entry_at", "anesthesia_start_at"])
    for key, minutes in (("synthetic-a", 20), ("synthetic-b", 21), ("synthetic-c", 40)):
        sheet.append([key, "2026-10-01", "2026-10-01T09:00:00+08:00", f"2026-10-01T09:{minutes:02}:00+08:00"])
    excel = tmp_path / "数据.xlsx"
    book.save(excel)
    import_id = service.import_paths([str(word), str(excel)], session["id"])["jobId"]
    assert run_once(service)
    imported = next(j for j in service.store.jobs() if j["id"] == import_id)
    assert not imported["result"]["failures"], imported
    return service, service.store.get("session", session["id"]), word, excel


def analyze(service, session, request="calculate"):
    result = service.submit_user_turn(session["id"], session["revision"], request, "", "ANALYZE", {"scope": "ALL_DATA"})
    assert run_once(service)
    return next(j for j in service.store.jobs() if j["id"] == result["jobId"])


def test_library_is_available_and_binding_is_explicit(prepared):  # noqa: F811
    service, _, definition, _ = prepared
    service.default_definition(definition["id"])
    assert service.dispatch("session.create", {})["definitionId"] is None
    assert definition["id"] in {d["id"] for d in service.bootstrap()["definitions"]}
    assert service.dispatch("definition.get", {"definition_id": definition["id"]})["manifestHash"] == definition["manifestHash"]
    assert service.dispatch("session.create", {"definition_id": definition["id"]})["definitionId"] == definition["id"]


def test_word_and_excel_compute_without_library_or_manual_mapping(uploaded):
    service, session, _, _ = uploaded
    assert session["definitionId"] and not session["mappings"]
    assert run_once(service, DocumentGateway())
    definition = service.dispatch("session.metrics", {"session_id": session["id"]})
    assert definition["manifest"]["validationMode"] == "AUTOMATIC_DOCUMENT"
    assert definition["manifest"]["sessionId"] == session["id"]
    session = service.store.get("session", session["id"])
    job = analyze(service, session)
    assert job["status"] == "SUCCEEDED", job
    run = service.store.get("run", job["result"]["runId"])
    assert run["results"][0]["groups"][0]["displayValue"] == "2"
    assert run["results"][0]["audit"]["inputRows"] == 3
    saved = service.store.get("session", session["id"])
    assert saved["mappings"] == run["contract"]["mappings"]
    assert saved["mappingRevisionId"] == run["contract"]["mappingRevisionId"]
    assert service.session_detail(session["id"])["metricDefinition"]["clearCount"] == 1


@pytest.mark.parametrize("remove_source", [True, False])
def test_removing_document_blocks_stale_extraction_and_clears_rules(uploaded, remove_source):
    service, session, word, _ = uploaded
    old_id = session["definitionId"]
    extracting = service.store.claim()
    source_id = service.store.get("definition", old_id)["attachmentId"]
    if remove_source:
        session = service.update_session(session["id"], session["revision"], {"attachmentIds": [a for a in session["attachmentIds"] if a != source_id]})
    else:
        service.route(source_id, "OTHER")
        session = service.store.get("session", session["id"])
    assert session["definitionId"] is None and not session["mappings"] and session["domainPackId"] is None
    with pytest.raises(AppError, match="已移除或用途已变更"):
        process_job(service, extracting, DocumentGateway())
    assert service.store.get("definition", old_id)["status"] == "DRAFT"
    service.store.finish(extracting, error=AppError("CANCELLED", "已移除说明").as_dict())
    doc = Document()
    doc.add_paragraph("统计口径：修改后的手术指标说明，分母为当前全部事件。")
    doc.add_paragraph("分母：当前上传数据的全部手术事件。")
    doc.save(word)
    service.import_paths([str(word)], session["id"])
    assert run_once(service)
    current = service.store.get("session", session["id"])
    assert current["definitionId"] != old_id
    assert service.session_metrics(session["id"])["documentHash"] != service.store.get("definition", old_id)["documentHash"]


def test_legacy_document_reimport_gets_scoped_rules_without_changing_history(uploaded):
    service, session, word, _ = uploaded
    extracting = service.store.claim()
    service.store.finish(extracting, error=AppError("CANCELLED", "模拟旧版解析中断").as_dict())
    with service.store.transaction() as db:
        legacy = service.store.get("definition", session["definitionId"], db)
        legacy.pop("sessionId")
        legacy = service.store.put("definition", legacy, db)
        session.update(definitionId=None, domainPackId=None)
        service.store.put("session", session, db)
    job_id = service.import_paths([str(word)], session["id"])["jobId"]
    assert run_once(service)
    imported = next(j for j in service.store.jobs() if j["id"] == job_id)
    assert not imported["result"]["failures"], imported
    current = service.session_metrics(session["id"])
    assert current["sessionId"] == session["id"] and current["id"] != legacy["id"]
    assert service.store.get("definition", legacy["id"]) == legacy
    assert run_once(service, DocumentGateway())
    assert service.session_metrics(session["id"])["status"] == "PUBLISHED"


def test_failed_parse_retry_is_scoped_and_becomes_ready(uploaded):
    service, session, _, _ = uploaded

    class FailingGateway:
        def call(self, *args):
            raise AppError("MODEL_TIMEOUT", "模拟网络超时")

    assert run_once(service, FailingGateway())
    retry = service.dispatch("session.metrics.retry", {"session_id": session["id"]})
    assert retry == service.retry_session_metrics(session["id"])
    other = service.create_session()
    with pytest.raises(AppError):
        service.attach_session_definition(other["id"], session["definitionId"])
    assert run_once(service, DocumentGateway())
    assert service.session_metrics(session["id"])["status"] == "PUBLISHED"


@pytest.mark.parametrize("gateway", [DocumentGateway(skip=True), DocumentGateway(invalid=True)])
def test_unclear_or_invalid_evidence_never_becomes_executable(uploaded, gateway):
    service, session, _, _ = uploaded
    assert run_once(service, gateway)
    definition = service.session_metrics(session["id"])
    assert definition["status"] == "DRAFT"
    assert all(c["status"] == "SKIP" and c["reason"] for c in definition["candidates"])
    job = analyze(service, service.store.get("session", session["id"]))
    assert job["status"] == "FAILED"
    assert job["error"]["details"]["nextAction"] == "VIEW_METRICS"
    assert not service.store.list("run")


def test_manual_mapping_opt_out_is_not_overridden(uploaded):
    service, session, _, _ = uploaded
    assert run_once(service, DocumentGateway())
    session = service.store.get("session", session["id"])
    session = service.update_session(session["id"], session["revision"], {"mappings": {}}, {"actor": "测试人员", "reason": "暂不映射此指标", "source": "USER_RULE"})
    job = analyze(service, session)
    run = service.store.get("run", job["result"]["runId"])
    assert run["contract"]["mappings"] == {}
    assert run["results"][0]["outcome"] == "SKIP"


def test_multiple_uploaded_tables_with_exact_fields_are_combined(uploaded, tmp_path):
    service, session, _, excel = uploaded
    from openpyxl import load_workbook
    book = load_workbook(excel)
    sheet = book.active
    sheet.delete_rows(2, 3)
    sheet.append(["synthetic-d", "2026-10-02", "2026-10-02T09:00:00+08:00", "2026-10-02T09:30:00+08:00"])
    extra = tmp_path / "补充数据.xlsx"
    book.save(extra)
    # Extraction is already queued, so finish it before importing extra rows.
    assert run_once(service, DocumentGateway())
    service.import_paths([str(extra)], session["id"])
    assert run_once(service)
    job = analyze(service, service.store.get("session", session["id"]))
    run = service.store.get("run", job["result"]["runId"])
    assert run["results"][0]["groups"][0]["displayValue"] == "3"
    assert len(next(iter(run["contract"]["mappings"].values()))["sources"]) == 2
