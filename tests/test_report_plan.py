"""Document positions, conservative intent compilation and local fill operators."""
import io
import json
from copy import deepcopy

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook

from insight_agent.core import AppError, sha
from insight_agent.report_plan import (OUTPUT_SCHEMA, calculate_slot, clause, extract_plan,
                                      render_report, template, validate_intent)
from insight_agent.service import AppService
from insight_agent.worker import run_once


def nodes(*texts):
    return [{"locator": f"p:{i}", "text": text} for i, text in enumerate(texts, 1)]


def item(slot, method="SURGERY_COUNT", **changes):
    return {"slot": slot["id"], "name": "总手术量", "method": method, "room": "", "start": "", "end": "",
            "minutes": 0, "reason": "", **changes}


def planned(text="总手术量X台", **changes):
    plan = template(nodes(text))
    slot = plan["slots"][0]
    slot["plan"] = item(slot, **changes)
    return plan, slot, {"reportPlan": plan, "reportDocumentHash": sha(b"synthetic document")}


def source(rows, headers=None, role="DETAIL", id="a"):
    headers = headers or list(rows[0])
    return {"attachmentId": id, "fileName": "合成材料.xlsx", "sheet": "明细", "snapshotHash": sha(id.encode()),
            "role": role, "headers": headers, "rowCount": len(rows),
            "rows": [{"row": i, "values": v} for i, v in enumerate(rows, 2)]}


def surgery(id="S1", date="2026-10-01", **changes):
    return {"手术编号": id, "手术日期": date, "患者ID": "P1", "手术类型": "择期", "手术室": "第一手术室", "手术状态": "已完成",
            "入室时间": date + "T09:00:00", "麻醉开始时间": date + "T09:20:00", **changes}


def test_template_does_not_confuse_formula_threshold_or_inline_x_with_slots():
    plan = template(nodes("手术量X台，急诊X台", "XX年XX月XX日", "XXX科，XXX台", "XX点，科室xxx",
                          "延迟率=延迟台数/同期总数x100%，阈值20分钟；Xray不属于占位"))
    assert [s["kind"] for s in plan["slots"]] == ["NUMBER", "NUMBER", "DATE", "TEXT", "NUMBER", "TEXT", "TEXT"]
    assert not plan["paragraphs"][-1]["slots"]
    assert len({s["id"] for s in plan["slots"]}) == 7


def test_context_inherits_planned_and_timepoint_room_groups():
    plan = template(nodes("X年X月X日当日拟做手术量X台", "第一手术室台次数X台", "质量指标", "16:00接台数XXX台", "第一手术室X台"))
    room_slots = [s for s in plan["slots"] if "第一手术室" in s["context"]]
    assert "拟做" in room_slots[0]["semanticContext"]
    assert "接台" in room_slots[1]["semanticContext"]
    for s in room_slots:
        with pytest.raises(AppError, match="复合口径"):
            validate_intent(s, item(s, room="第一手术室"))


@pytest.mark.parametrize("method,offset", [("EMERGENCY_COUNT", 0), ("SURGERY_COUNT", 1)])
def test_same_paragraph_different_slots_cannot_swap_total_and_emergency(method, offset):
    plan = template(nodes("总手术量X台，急诊手术量X台"))
    slot = plan["slots"][offset]
    with pytest.raises(AppError) as exc:
        validate_intent(slot, item(slot, method))
    assert exc.value.code == "MODEL_SEMANTIC_INVALID"


def test_global_surgery_identity_and_overlapping_files():
    plan, slot, contract = planned()
    records = [surgery(), surgery("S2"), surgery("S3", "2026-10-02")]
    sources = [source(records), source(records[:1], id="subset"), source([{"退单时间": "2026-10-01"}], role="CANCELLATION", id="cancel")]
    result = calculate_slot(slot, sources, contract)
    assert result["outcome"] == "VALUE"
    assert [g["displayValue"] for g in result["groups"]] == ["2", "1"]
    assert result["audit"]["duplicateRows"] == 1
    assert result["provenance"]["sourceAssessment"][-1]["decision"] == "EXCLUDED"
    sources.append(source([surgery("S1", "2026-10-02")], id="conflict"))
    result = calculate_slot(slot, sources, contract)
    assert result["reasonCode"] == "DATA_CONFLICT" and result["outcome"] == "SKIP"


def test_inpatient_number_is_not_surgery_identity():
    _, slot, contract = planned()
    row = surgery()
    row["住院号"] = row.pop("手术编号")
    result = calculate_slot(slot, [source([row])], contract)
    assert result["reasonCode"] == "FIELD_UNRESOLVED"
    assert result["provenance"]["sourceAssessment"][0]["decision"] == "BLOCKED"


def test_unknown_source_prevents_partial_total():
    _, slot, contract = planned()
    result = calculate_slot(slot, [source([surgery()]), source([{"未知": "合成"}], role="UNRESOLVED", id="unknown")], contract)
    assert result["reasonCode"] == "SOURCE_ROLE_UNRESOLVED"


def test_actual_count_needs_occurrence_evidence_not_just_a_unique_identifier():
    _, slot, contract = planned()
    row = surgery()
    row.pop("手术状态")
    assert calculate_slot(slot, [source([row])], contract)["reasonCode"] == "ACTUAL_STATUS_UNCONFIRMED"
    row["手术开始时间"] = "2026-10-01T09:30:00"
    assert calculate_slot(slot, [source([row])], contract)["groups"][0]["displayValue"] == "1"
    assert calculate_slot(slot, [source([surgery(手术状态="计划")])], contract)["groups"][0]["displayValue"] == "0"
    assert calculate_slot(slot, [source([row | {"手术状态": "已取消"}])], contract)["reasonCode"] == "DATA_CONFLICT"


def test_daily_patient_distinct_strict_threshold_multiple_surgeries_and_midnight():
    _, slot, contract = planned("入室至麻醉开始超过20分钟的患者人数X人", method="DURATION_PATIENTS", start="entry", end="anesthesia", minutes=20)
    records = [surgery(), surgery("S2", 麻醉开始时间="2026-10-01T09:21:00"),
               surgery("S3", 麻醉开始时间="2026-10-01T09:30:00"),
               surgery("S4", 患者ID="P2", 入室时间="2026-10-01T23:50:00", 麻醉开始时间="2026-10-02T00:11:00"),
               surgery("S5", "2026-10-02")]
    result = calculate_slot(slot, [source(records), source(records[:1], id="duplicate")], contract)
    assert [g["displayValue"] for g in result["groups"]] == ["2", "0"]
    assert result["unit"] == "人"


@pytest.mark.parametrize("changes,code", [({"麻醉开始时间": "09:21:00"}, "INVALID_TIME"),
                                         ({"麻醉开始时间": "2026-10-01T08:00:00"}, "INVALID_TIME"),
                                         ({"手术日期": "invalid"}, "INVALID_DATE"),
                                         ({"患者ID": ""}, "ENTITY_KEY_MISSING")])
def test_bad_patient_data_is_a_visible_gap(changes, code):
    _, slot, contract = planned("入室至麻醉开始超过20分钟的患者人数X人", method="DURATION_PATIENTS", start="entry", end="anesthesia", minutes=20)
    assert calculate_slot(slot, [source([surgery(**changes)])], contract)["reasonCode"] == code


def test_wrong_threshold_is_a_technical_failure_not_business_skip():
    _, slot, contract = planned("入室至麻醉开始超过20分钟的患者人数X人", method="DURATION_PATIENTS", start="entry", end="anesthesia", minutes=30)
    with pytest.raises(AppError) as exc:
        calculate_slot(slot, [source([surgery()])], contract)
    assert exc.value.code == "MODEL_SEMANTIC_INVALID"


def test_summary_requires_basis_unit_dimension_and_conflict_checks():
    _, slot, contract = planned()
    row = {"指标": "总手术量", "数值": "3", "单位": "台", "统计日期": "2026-10-01", "口径": slot["context"]}
    result = calculate_slot(slot, [source([row], role="SUMMARY")], contract)
    assert result["method"] == "READ_SUMMARY" and result["groups"][0]["displayValue"] == "3"
    assert result["provenance"]["sources"][0]["row"] == 2
    for changes, code in [({"单位": "人"}, "SUMMARY_UNIT_CONFLICT"), ({"口径": "名字一样不代表口径一样"}, "SUMMARY_BASIS_UNCONFIRMED"),
                          ({"手术室": "第一手术室"}, "SUMMARY_DIMENSION_CONFLICT")]:
        assert calculate_slot(slot, [source([row | changes], role="SUMMARY")], contract)["reasonCode"] == code
    assert calculate_slot(slot, [source([row, row | {"数值": "4"}], role="SUMMARY")], contract)["reasonCode"] == "SUMMARY_VALUE_CONFLICT"


def test_unsupported_rate_summary_uses_percentage_unit_not_surgeries():
    _, slot, contract = planned("首台延误率X%", method="UNRESOLVED", name="首台延误率", reason="缺少首台分类与计算规则")
    row = {"指标": "首台延误率", "数值": "12.5", "单位": "%", "统计日期": "2026-10-01", "口径": slot["context"]}
    result = calculate_slot(slot, [source([row], role="SUMMARY")], contract)
    assert result["unit"] == "%" and result["groups"][0]["displayValue"] == "12.5"


def test_summary_multiple_positions_require_an_explicit_slot_binding():
    plan = template(nodes("总手术量X台，急诊手术量X台"))
    first, second = plan["slots"]
    for s in (first, second):
        s["plan"] = item(s, "READ_SUMMARY", name="同名汇总")
    contract = {"reportPlan": plan, "reportDocumentHash": sha(b"summary test")}
    row = {"指标": "同名汇总", "数值": "3", "单位": "台", "统计日期": "2026-10-01", "口径": first["semanticContext"]}
    assert calculate_slot(first, [source([row], role="SUMMARY")], contract)["reasonCode"] == "SUMMARY_POSITION_UNCONFIRMED"
    rows = [row | {"待填位置": first["id"]}, row | {"待填位置": second["id"], "数值": "1"}]
    assert [calculate_slot(s, [source(rows, role="SUMMARY")], contract)["groups"][0]["displayValue"] for s in (first, second)] == ["3", "1"]


def test_summary_inherits_timepoint_basis_and_cannot_reuse_another_room_group():
    plan = template(nodes("16:00接台数X台", "第一手术室X台", "18:00接台数X台", "第一手术室X台"))
    slots = [s for s in plan["slots"] if "第一手术室" in s["context"]]
    for s in slots:
        s["plan"] = item(s, "UNRESOLVED", name="手术室接台数", room="第一手术室", reason="需明确接台口径")
    contract = {"reportPlan": plan, "reportDocumentHash": sha(b"timepoint test")}
    first, second = slots
    row = {"指标": "手术室接台数", "数值": "3", "单位": "台", "手术室": "第一手术室", "统计日期": "2026-10-01", "口径": first["semanticContext"]}
    assert calculate_slot(first, [source([row], role="SUMMARY")], contract)["outcome"] == "VALUE"
    assert calculate_slot(second, [source([row], role="SUMMARY")], contract)["reasonCode"] == "SUMMARY_BASIS_UNCONFIRMED"


def test_render_preserves_order_rules_and_each_gap_and_does_not_fake_yesterday():
    plan, slot, contract = planned()
    result = calculate_slot(slot, [source([surgery()])], contract)
    report = render_report(plan, [result])
    assert report["paragraphs"][0]["text"] == "总手术量2026-10-01：1台"
    assert "未确认" in report["scopeNote"]
    with pytest.raises(AppError, match="完整对账"):
        render_report(plan, [])
    with pytest.raises(AppError, match="完整对账"):
        render_report(plan, [result, deepcopy(result)])


class FillGateway:
    def __init__(self, fail_call=None):
        self.calls, self.fail_call = [], fail_call

    def call(self, stage, payload, schema, budget_id):
        assert stage == "fill_plan" and schema == OUTPUT_SCHEMA
        self.calls.append((deepcopy(payload), budget_id))
        if self.fail_call == len(self.calls):
            raise AppError("MODEL_UNAVAILABLE", "合成服务不可用")
        plan = template(payload["nodes"])
        items = []
        for requested in payload["slots"]:
            slot = next(s for s in plan["slots"] if s["id"] == requested["id"])
            local = clause(slot)
            if "拟做" in slot["semanticContext"]:
                values = item(slot, "UNRESOLVED", name="拟做手术量", reason="缺少16:00排程历史快照")
            elif "患者" in local:
                values = item(slot, "DURATION_PATIENTS", name="入室至麻醉超过20分钟患者人数", start="entry", end="anesthesia", minutes=20)
            else:
                values = item(slot, "EMERGENCY_COUNT" if "急诊" in local else "SURGERY_COUNT", name="急诊手术量" if "急诊" in local else "总手术量")
            items.append(values)
        return {"items": items}


@pytest.fixture
def uploaded_fill(tmp_path):
    service = AppService(tmp_path / "workspace")
    session = service.create_session()
    document = Document()
    for text in ("统计口径：手术编号唯一标识一次手术，患者ID唯一标识患者，按手术日期逐日统计。", "总手术量X台，急诊手术量X台。",
                 "入室至麻醉开始超过20分钟的患者人数X人。", "当日16:00拟做手术量X台。", "计算规则：严格大于20分钟，比例公式x100%。"):
        document.add_paragraph(text)
    path = tmp_path / "指标说明.docx"
    document.save(path)
    book = Workbook()
    sheet = book.active
    records = [surgery(), surgery("S2", 手术类型="急诊", 麻醉开始时间="2026-10-01T09:21:00")]
    sheet.append(list(records[0]))
    for row in records:
        sheet.append(list(row.values()))
    excel = tmp_path / "材料.xlsx"
    book.save(excel)
    service.import_paths([str(path), str(excel)], session["id"])
    assert run_once(service)
    return service, session["id"]


def test_end_to_end_registered_skills_full_coverage_fill_and_export(uploaded_fill):
    service, session_id = uploaded_fill
    assert run_once(service, FillGateway())
    definition = service.session_metrics(session_id)
    assert definition["status"] == "PUBLISHED"
    assert len(definition["coverage"]) == 5
    assert len(definition["candidates"]) == 4
    assert all(c["status"] == "REPORT" for c in definition["candidates"])
    session = service.store.get("session", session_id)
    submitted = service.submit_user_turn(session_id, session["revision"], "fill-e2e", "", "ANALYZE", {"scope": "ALL_DATA"})
    assert run_once(service)
    job = next(j for j in service.store.jobs() if j["id"] == submitted["jobId"])
    assert job["status"] == "SUCCEEDED", job
    run = service.store.get("run", job["result"]["runId"])
    assert run["committed"] and run["status"] == "SUCCEEDED_WITH_SKIPS"
    assert [r["groups"][0]["displayValue"] for r in run["results"] if r["outcome"] == "VALUE"] == ["2", "1", "1"]
    report = run["filledReport"]
    assert len(report["bindings"]) == 4 and len(report["gaps"]) == 1
    assert report["paragraphs"][-1]["text"] == "计算规则：严格大于20分钟，比例公式x100%。"
    assert "排程历史快照" in report["paragraphs"][-2]["text"]
    from insight_agent.skills import invoke
    for format in ("json", "docx", "xlsx", "csv", "zip"):
        artifact = invoke("surgery-word-report", "export", service.store, run["id"], format)
        assert artifact["contentQA"] == "PASS"
        data = service.store.read_blob(artifact["sha256"])
        if format == "json":
            assert len(json.loads(data)["filledReport"]["bindings"]) == 4
        if format == "docx":
            assert report["paragraphs"][-2]["text"] in "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
        if format == "xlsx":
            wb = load_workbook(io.BytesIO(data))
            assert "待补充清单" in wb.sheetnames and wb["待补充清单"].max_row == 2
        if format == "csv":
            assert "排程历史快照" in data.decode("utf-8-sig")


def test_first_batch_failure_retains_budget_identity_and_no_partial_publish(uploaded_fill):
    service, session_id = uploaded_fill
    failing = FillGateway(fail_call=1)
    assert run_once(service, failing)
    assert service.session_metrics(session_id)["status"] == "DRAFT"
    attempt = service.store.list("fill_attempt")[0]
    assert not attempt["batches"]
    retry = service.retry_session_metrics(session_id)
    gateway = FillGateway()
    assert run_once(service, gateway)
    assert gateway.calls[0][1] == failing.calls[0][1] == attempt["budgetId"]
    assert retry["jobId"] != attempt["budgetId"]
    assert service.session_metrics(session_id)["status"] == "PUBLISHED"


@pytest.mark.parametrize("recover", [True, False])
def test_semantic_correction_is_bounded_and_never_publishes_bad_intent(uploaded_fill, recover):
    service, session_id = uploaded_fill
    class CorrectingGateway(FillGateway):
        def call(self, *args):
            value = super().call(*args)
            if len(self.calls) == 1 or not recover:
                value["items"][0]["start"] = "entry"
            return value
    gateway = CorrectingGateway()
    assert run_once(service, gateway)
    assert len(gateway.calls) == 2
    assert gateway.calls[0][1] == gateway.calls[1][1]
    assert "上一批未通过" in gateway.calls[1][0]["instruction"]
    definition = service.session_metrics(session_id)
    assert definition["status"] == ("PUBLISHED" if recover else "DRAFT")
    if not recover:
        assert not definition["candidates"]
        assert any(j["error"] and j["error"]["code"] == "MODEL_SEMANTIC_INVALID" for j in service.store.jobs())


@pytest.mark.parametrize('code', ['MODEL_OUTPUT_TRUNCATED', 'MODEL_TIMEOUT'])
def test_successful_batches_reused_and_failed_batch_only_bisected(tmp_path, code):
    service = AppService(tmp_path / "workspace")
    document_nodes = nodes(*(f"第{i}项总手术量X台" for i in range(9)))
    with service.store.transaction() as db:
        draft = service.store.put("definition", {"status": "DRAFT", "nodes": document_nodes, "documentHash": sha(b"doc"), "amendments": []}, db)
    failing = FillGateway(fail_call=2)
    with pytest.raises(AppError):
        extract_plan(service.store, draft, failing, "budget-first")
    assert len(service.store.list("fill_attempt")[0]["batches"]) == 1

    class Truncated(FillGateway):
        def call(self, stage, payload, schema, budget_id):
            if len(payload["slots"]) > 2:
                self.calls.append((deepcopy(payload), budget_id))
                raise AppError(code, "合成批次失败")
            return super().call(stage, payload, schema, budget_id)

    gateway = Truncated()
    result = extract_plan(service.store, draft, gateway, "new-job")
    assert [len(c[0]["slots"]) for c in gateway.calls] == [4, 2, 2, 1]
    assert all(c[1] == "budget-first" for c in gateway.calls)
    assert len(result["reportPlan"]["slots"]) == 9


def test_cancelled_extraction_never_saves_completed_plan(uploaded_fill):
    service, session_id = uploaded_fill

    class Removed(FillGateway):
        def call(self, *args):
            result = super().call(*args)
            session = service.store.get("session", session_id)
            definition = service.session_metrics(session_id)
            service.update_session(session_id, session["revision"], {"attachmentIds": [a for a in session["attachmentIds"] if a != definition["attachmentId"]]})
            return result

    assert run_once(service, Removed())
    assert service.store.get("session", session_id)["definitionId"] is None
    assert not any(d["status"] == "PUBLISHED" for d in service.store.list("definition"))
