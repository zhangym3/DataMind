"""Synthetic Office fixtures and independently specified integration expectations."""
import copy
import io
import json
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook

from insight_agent.core import AppError, sha
from insight_agent.definitions import create_draft, publish, save_candidates
from insight_agent.files import import_file, parse_xlsx
from insight_agent.service import AppService
from insight_agent.worker import run_once

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    service = AppService(tmp_path / "workspace")
    session = service.create_session("合成手术数据")
    rule = json.loads((ROOT / "docs/contracts/examples/surgery-delay-count.json").read_text(encoding="utf-8"))
    quote = "手术事件以 surgery_id 唯一，按 surgery_date 统计。入室至麻醉开始严格超过20分钟计延迟台次。起止必须为完整日期时间，跨午夜以实际时差计算。缺失、非法值和重复键不计算。统计窗口左闭右开，空集为0。"
    word = Document()
    word.add_paragraph(quote)
    word_path = tmp_path / "指标说明.docx"
    word.save(word_path)
    attachment = import_file(service.store, word_path, "library")
    attachment = service.route(attachment["id"], "METRIC_DEFINITION")
    definition = create_draft(service.store, attachment["id"], "合成手术口径")
    node = definition["nodes"][0]
    for evidence in rule["sourceEvidence"]:
        evidence.update(documentSha256=attachment["sha256"], locator=node["locator"], quote=quote, quoteSha256=sha(quote.encode()))
    candidate = {"id":rule["metricId"],"name":rule["name"],"status":"CLEAR","rule":rule,"locators":[node["locator"]]}
    definition = save_candidates(service.store,definition["id"],[candidate],[{"locator":node["locator"],"disposition":"CANDIDATE","candidateIds":[rule["metricId"]],"reason":"人工逐条核对合成规范"}],definition["revision"])
    definition = publish(service.store,definition["id"],definition["revision"],"测试审核人",True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "手术事件"
    headers = [f["id"] for f in rule["requiredFields"]]
    sheet.append(headers)
    cases = json.loads((ROOT / "docs/contracts/golden-cases.json").read_text(encoding="utf-8"))["cases"][0]
    for row in cases["rows"]:
        sheet.append([row.get(h) for h in headers])
    excel_path = tmp_path / "测试数据.xlsx"
    workbook.save(excel_path)
    service.import_paths([str(excel_path)],session["id"])
    assert run_once(service)
    assert service.store.jobs()[0]["status"] == "SUCCEEDED", service.store.jobs()[0]
    detail = service.session_detail(session["id"])
    session = service.update_session(session["id"],detail["session"]["revision"],{"definitionId":definition["id"]})
    mapping = service.suggest_mapping(session["id"])
    mapping[rule["metricId"]]["coverage"]={"start":"2026-10-01","end":"2026-11-01","complete":True,"evidence":"独立合成全量事件"}
    session = service.update_session(session["id"],session["revision"],{"mappings":mapping})
    return service, session, definition, cases["parameters"]


def submit(service, session, parameters, action="ANALYZE", source=None, request_id="request-1"):
    return service.submit_user_turn(session["id"],session["revision"],request_id,"",action,parameters,source)


def test_real_office_langgraph_and_offline_recalculation(prepared):
    service, session, definition, parameters = prepared
    submit(service,session,parameters)
    assert run_once(service)
    run = service.store.list("run")[0]
    assert run["status"] == "SUCCEEDED", service.store.jobs()[0]
    assert run["committed"] and run["results"][0]["groups"][0]["displayValue"] == "2"
    assert service.store.get("session",session["id"])["state"] == "LOCKED"
    assert (service.store.home / "checkpoints.sqlite3").is_file()
    # No gateway is configured: any unintended online call would fail this test.
    session = service.store.get("session",session["id"])
    submit(service,session,{},"RECALCULATE_FROZEN",run["id"],"recalc-1")
    assert run_once(service)
    recalculated = service.store.list("run")[0]
    assert recalculated["status"] == "SUCCEEDED", service.store.jobs()[0]
    assert recalculated["contractHash"] == run["contractHash"]
    assert recalculated["resultDigest"] == run["resultDigest"]


def test_all_data_run_keeps_definition_order_and_phase_history(prepared):
    service, session, definition, _ = prepared
    submit(service, session, {"scope": "ALL_DATA"})
    assert run_once(service)
    run = service.store.list("run")[0]
    assert run["contract"]["parameters"] == {"scope": "ALL_DATA"}
    assert run["results"][0]["definitionOrder"] == 1
    assert run["results"][0]["definition"]["name"] == definition["candidates"][0]["name"]
    assert run["results"][0]["audit"]["analysisScope"] == "ALL_DATA"
    assert run["results"][0]["audit"]["outsideWindowRows"] == 0
    assert [item["phase"] for item in run["phaseHistory"]] == ["PREPARING", "CALCULATING", "AUDITING", "COMMITTING", "COMPLETED"]
    assert all(item["durationMs"] is not None for item in run["phaseHistory"])


def test_submit_idempotency_and_revision(prepared):
    service, session, _, parameters = prepared
    first=submit(service,session,parameters)
    assert submit(service,session,parameters)==first
    with pytest.raises(AppError) as error:
        submit(service,session,parameters|{"date_end":"2026-10-02"})
    assert error.value.code=="IDEMPOTENCY_CONFLICT"
    assert sum(j["kind"]=="turn" for j in service.store.jobs())==1


def test_help_does_not_lock_and_invalid_window_does_not_lock(prepared):
    service, session, _, _=prepared
    submit(service,session,{},"HELP")
    run_once(service)
    session=service.store.get("session",session["id"])
    assert session["state"]=="DRAFT"
    submit(service,session,{"date_start":"2026-11-01","date_end":"2026-10-01"},request_id="invalid")
    run_once(service)
    assert service.store.jobs()[0]["status"]=="FAILED"
    assert service.store.get("session",session["id"])["state"]=="DRAFT"
    assert not service.store.list("run")


def test_plan_locks_and_cancel_does_not_publish(prepared):
    service,session,_,parameters=prepared
    response=submit(service,session,parameters,"PLAN")
    run_once(service)
    assert service.store.list("run")[0]["results"][0]["outcome"]=="PLANNED"
    session=service.store.get("session",session["id"])
    assert session["state"]=="LOCKED"
    response=submit(service,session,parameters,request_id="cancel")
    service.cancel(response["jobId"])
    assert not run_once(service)
    assert len(service.store.list("run"))==1


def test_scope_and_locked_baseline(prepared):
    service,session,definition,parameters=prepared
    submit(service,session,parameters)
    run_once(service)
    session=service.store.get("session",session["id"])
    with pytest.raises(AppError) as error:
        service.update_session(session["id"],session["revision"],{"mappings":{}})
    assert error.value.code=="SESSION_LOCKED"
    fresh=service.create_session()
    assert fresh["id"]!=session["id"] and fresh["attachmentIds"]==[] and fresh["state"]=="DRAFT" and fresh["mappings"]=={}
    with pytest.raises(AppError) as error:
        service.dispatch("session.clone", {"session_id": session["id"]})
    assert error.value.code=="API_FORBIDDEN"
    with pytest.raises(AppError):
        service.preview(session["attachmentIds"][0],fresh["id"])
    service.disable_definition(definition["id"],True)
    with pytest.raises(AppError):
        service.create_session(definition_id=definition["id"])


def test_sparse_excel_preserves_exact_decimal():
    wb=Workbook()
    ws=wb.active
    ws.append(["a","b","c"])
    ws.append(["0.10000000000000000001",None,"文本"])
    stream=io.BytesIO()
    wb.save(stream)
    parsed=parse_xlsx(stream.getvalue())
    assert parsed["sheets"][0]["rows"][0]["values"]=={"a":"0.10000000000000000001","b":None,"c":"文本"}
