"""Acceptance checks for the revisioned three-source runtime."""
from pathlib import Path
import sqlite3
import uuid

import pytest

from insight_agent.core import AppError
from insight_agent.maintenance import verify_references
from insight_agent.service import AppService
from insight_agent.skills import invoke, registry
from insight_agent.store import SCHEMA, Store
from insight_agent.worker import run_once
from insight_agent.three_source_ingest import (_filename_unit, confirm_unit, correct_unit,
                                               _semantic_unit, detect_source_type,
                                               import_workbook, list_sources)
from insight_agent.three_source_kb import bundled_catalog
from insight_agent import three_source_kb
from insight_agent.three_source_metrics import (create_metric, edit_metric, generate_sql,
                                                publish_metric, route_metric)
from insight_agent.three_source_query import (basic_plan, compile_plan, execute_plan,
                                              validate_question_plan)

SAMPLES = Path(__file__).resolve().parents[1] / "templet"


def _service(tmp_path):
    service = AppService(tmp_path)
    session = service.create_session_v2()
    return service, session


def test_schema_one_migration_preserves_history(tmp_path):
    path = tmp_path / "datamind.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO meta VALUES ('schema','1')")
        db.execute("INSERT INTO objects VALUES ('session','old',1,'{}','before','before')")
    store = Store(tmp_path)
    with store.connect() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "2"
        assert db.execute("SELECT id FROM objects WHERE id='old'").fetchone()[0] == "old"
        assert db.execute("SELECT count(*) FROM kb_field").fetchone()[0] >= 59
    assert (tmp_path / "datamind-before-schema-2.sqlite3").is_file()


def test_no_fts5_keeps_exact_matching_and_uncertain_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(three_source_kb, "_install_fts", lambda db: False)
    service, _ = _service(tmp_path)
    metric = create_metric(service.store, "退单来源记录行数", "统计退单来源记录行数")
    plan = {"entity": "cancellation", "aggregate": "COUNT_SOURCE_ROWS", "groupBy": [],
            "filters": [], "orderBy": [], "limit": 1000}
    gateway = type("GatewayStub", (), {"call": lambda self, *args: {
        "plan": plan, "sqlDraft": "SELECT count(*) FROM cancellation_row"}})()
    generate_sql(service.store, metric["id"], 1, gateway)
    publish_metric(service.store, metric["id"], 2, "reviewer", True)
    assert route_metric(service.store, "退单来源记录行数")["decision"] == "MATCH"
    assert route_metric(service.store, "请分析退单导出资料")["decision"] == "RETRIEVAL_UNCERTAIN"


def test_draft_metric_does_not_fall_through_to_dynamic_sql(tmp_path):
    service, _ = _service(tmp_path)
    draft = create_metric(service.store, "退单来源记录行数", "统计退单来源记录行数")
    decision = route_metric(service.store, "退单来源记录行数")
    assert decision == {"decision": "MATCH_UNAVAILABLE", "candidates": [draft["id"]],
                        "reason": "DRAFT_NOT_PUBLISHED"}


def test_worker_graphs_import_query_and_generate_sql(tmp_path):
    service, session = _service(tmp_path)
    sample = next(SAMPLES.glob("*一手退单*"))
    import_job = service.import_paths([str(sample)], session["id"])["jobId"]
    assert run_once(service)
    assert next(job for job in service.store.jobs() if job["id"] == import_job)["status"] == "SUCCEEDED"
    request = service.submit_user_turn(session["id"], session["revision"], str(uuid.uuid4()),
                                       "一号手术室退单来源记录行数")
    assert run_once(service)
    run = service.store.get("run_v2", request["runId"])
    assert run["status"] == "COMPLETED"
    assert run["result"]["rows"] == [{"source_row_count": 134}]
    metric = create_metric(service.store, "退单来源记录行数", "统计退单来源记录行数")
    plan = {"entity": "cancellation", "aggregate": "COUNT_SOURCE_ROWS", "groupBy": [],
            "filters": [], "orderBy": [], "limit": 1000}
    gateway = type("GatewayStub", (), {"call": lambda self, *args: {
        "plan": plan, "sqlDraft": "SELECT count(*) FROM cancellation_row"}})()
    job_id = service.queue_v2_sql(metric["id"], 1)["jobId"]
    assert run_once(service, gateway)
    assert next(job for job in service.store.jobs() if job["id"] == job_id)["status"] == "SUCCEEDED"
    assert service.store.get("run_v2", request["runId"])["status"] == "COMPLETED"
    assert service.preview_v2_metric(session["id"], metric["id"])["rows"] == [{"source_row_count": 134}]


def test_registered_ingest_skill(tmp_path):
    service, session = _service(tmp_path)
    path = next(SAMPLES.glob("*首台*"))
    assert detect_source_type(path) == "FIRST_SURGERY"
    assert "ingest" in registry()["surgery-first-ingest"]["tools"]
    result = invoke("surgery-first-ingest", "ingest", service.store, session["id"], path,
                    expected_source_type="FIRST_SURGERY")
    assert result["rowCount"] == 474
    with pytest.raises(AppError) as exc:
        import_workbook(service.store, session["id"], next(SAMPLES.glob("*一手退单*")),
                        expected_source_type="FIRST_SURGERY")
    assert exc.value.code == "SKILL_TYPE_MISMATCH"


def test_local_info_route_without_data_or_model(tmp_path):
    service, session = _service(tmp_path)
    response = service.submit_user_turn(session["id"], session["revision"], str(uuid.uuid4()), "有哪些文件")
    assert response["local"]
    assert "还没有上传" in service.session_detail(session["id"])["messages"][-1]["text"]
    response = service.submit_user_turn(session["id"], session["revision"], str(uuid.uuid4()), "查看表结构")
    assert response["local"]
    assert "cancellation" in service.session_detail(session["id"])["messages"][-1]["text"]


def test_submitted_query_freezes_data_and_catalog(tmp_path):
    service, session = _service(tmp_path)
    import_workbook(service.store, session["id"], next(SAMPLES.glob("*首台*")))
    request_id = str(uuid.uuid4())
    first = service.submit_user_turn(session["id"], session["revision"], request_id, "首台来源记录行数")
    assert service.submit_user_turn(session["id"], session["revision"], request_id, "首台来源记录行数") == first
    run = service.store.get("run_v2", first["runId"])
    assert run["kbContentSha256"] and run["metricCatalogSha256"]
    import_workbook(service.store, session["id"], next(SAMPLES.glob("*一手退单*")))
    current = list_sources(service.store, session["id"])["revision"]
    assert current["id"] != run["dataRevisionId"]
    assert service.store.get("run_v2", first["runId"])["dataRevisionId"] == run["dataRevisionId"]


def test_all_samples_and_revision_scope(tmp_path):
    service, session = _service(tmp_path)
    imported = [import_workbook(service.store, session["id"], path) for path in SAMPLES.glob("*.xlsx")]
    by_type = {}
    for item in imported:
        by_type.setdefault(item["sourceType"], []).append(item["rowCount"])
    assert {key: sum(values) for key, values in by_type.items()} == {
        "FIRST_SURGERY": 474, "CANCELLATION": 150, "SURGERY_DETAIL": 30740}
    assert [len(by_type[key]) for key in ("FIRST_SURGERY", "CANCELLATION", "SURGERY_DETAIL")] == [1, 3, 3]
    assert execute_plan(service.store, session["id"], basic_plan("退单来源记录行数"))["rows"] == [{"source_row_count": 150}]
    assert execute_plan(service.store, session["id"], basic_plan("一号手术室退单来源记录行数"))["rows"] == [{"source_row_count": 134}]
    assert len(execute_plan(service.store, session["id"], basic_plan("按日期统计明细来源记录行数"))["rows"]) > 1
    other = service.create_session_v2()
    with pytest.raises(AppError) as exc:
        execute_plan(service.store, other["id"], basic_plan("退单来源记录行数"),
                     list_sources(service.store, session["id"])["revision"]["id"])
    assert exc.value.code == "DATA_REQUIRED"
    repeated = import_workbook(service.store, session["id"], next(SAMPLES.glob("*首台*")))
    assert repeated["deduplicated"]
    assert len(list_sources(service.store, session["id"])["sources"]) == 7


def test_cancellation_unit_confirmation(tmp_path):
    routing = bundled_catalog()["routing"]
    assert _filename_unit("二十一手退单.xlsx", routing)[0] is None
    assert _filename_unit("一手二手退单.xlsx", routing)[0] is None
    judge = type("Judge", (), {"call": lambda self, *args: {
        "candidateUnitCode": "OR_1", "fileNameEvidence": "possible", "conflictingEvidence": "",
        "decision": "CANDIDATE"}})()
    assert _semantic_unit("手术退单汇总.xlsx", routing, judge)[0] is None
    service, session = _service(tmp_path)
    ambiguous = tmp_path / "手术退单汇总.xlsx"
    ambiguous.write_bytes(next(SAMPLES.glob("*一手退单*")) .read_bytes())
    result = import_workbook(service.store, session["id"], ambiguous)
    assert result["state"] == "NEEDS_UNIT_CONFIRMATION"
    repeated = import_workbook(service.store, session["id"], ambiguous)
    assert repeated["sourceId"] == result["sourceId"] and repeated["deduplicated"]
    assert list_sources(service.store, session["id"])["revision"] is None
    confirmed = confirm_unit(service.store, session["id"], result["sourceId"], "OR_2")
    assert confirmed["state"] == "READY"
    assert execute_plan(service.store, session["id"], basic_plan("二号手术室退单来源记录行数"))["rows"] == [{"source_row_count": 134}]
    prior_revision = confirmed["revisionId"]
    corrected = correct_unit(service.store, session["id"], result["sourceId"], "OR_3")
    assert corrected["revisionId"] != prior_revision
    assert execute_plan(service.store, session["id"], basic_plan("三号手术室退单来源记录行数"))["rows"] == [{"source_row_count": 134}]
    assert execute_plan(service.store, session["id"], basic_plan("二号手术室退单来源记录行数"), prior_revision)["rows"] == [{"source_row_count": 134}]
    repeated = import_workbook(service.store, session["id"], ambiguous)
    assert repeated["deduplicated"] and repeated["sourceId"] == corrected["sourceId"]


def test_same_blob_separate_conversations(tmp_path):
    service, first = _service(tmp_path)
    second = service.create_session_v2()
    sample = next(SAMPLES.glob("*首台*"))
    a = import_workbook(service.store, first["id"], sample)
    b = import_workbook(service.store, second["id"], sample)
    assert a["sourceId"] != b["sourceId"]
    with service.store.connect() as db:
        rows = db.execute("SELECT file_sha256 FROM data_source WHERE id IN (?,?)", (a["sourceId"], b["sourceId"])).fetchall()
        assert rows[0][0] == rows[1][0]
    assert execute_plan(service.store, first["id"], basic_plan("首台来源记录行数"))["rows"] == [{"source_row_count": 474}]
    assert execute_plan(service.store, second["id"], basic_plan("首台来源记录行数"))["rows"] == [{"source_row_count": 474}]
    deleted = service.delete_session(first["id"], first["revision"], "确认删除")
    assert deleted["deletedSources"] == 1
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM data_source WHERE session_id=?", (first["id"],)).fetchone()[0] == 0
    assert execute_plan(service.store, second["id"], basic_plan("首台来源记录行数"))["rows"] == [{"source_row_count": 474}]


def test_backup_reference_includes_three_source_blob(tmp_path):
    service, session = _service(tmp_path)
    import_workbook(service.store, session["id"], next(SAMPLES.glob("*首台*")))
    verify_references(tmp_path)
    with service.store.connect() as db:
        file_hash = db.execute("SELECT file_sha256 FROM data_source LIMIT 1").fetchone()[0]
    blob = tmp_path / "blobs" / file_hash
    backup = tmp_path / "blob-copy"
    blob.rename(backup)
    try:
        with pytest.raises(AppError) as exc:
            verify_references(tmp_path)
        assert exc.value.code == "BACKUP_CORRUPT"
    finally:
        backup.rename(blob)


def test_metric_version_and_plan_boundary(tmp_path):
    service, session = _service(tmp_path)
    metric = create_metric(service.store, "退单来源记录行数", "统计退单导出的来源行数")
    plan = {"entity": "cancellation", "aggregate": "COUNT_SOURCE_ROWS", "groupBy": [],
            "filters": [], "orderBy": [], "limit": 1000}
    gateway = type("GatewayStub", (), {"call": lambda self, *args: {"plan": plan, "sqlDraft": "SELECT count(*) FROM cancellation_row"}})()
    generated = generate_sql(service.store, metric["id"], 1, gateway, generation_id="test-generation")
    assert generated["version"]["status"] == "VALIDATED"
    assert generate_sql(service.store, metric["id"], 1, gateway, generation_id="test-generation")["version"]["draft_revision"] == 2
    import_workbook(service.store, session["id"], next(SAMPLES.glob("*一手退单*")))
    preview = service.preview_v2_metric(session["id"], metric["id"])
    assert preview["rows"] == [{"source_row_count": 134}]
    assert preview["sql"] == generated["version"]["sql_template"]
    assert "rev.session_id=:session_id" in generated["version"]["sql_template"]
    published = publish_metric(service.store, metric["id"], 2, "reviewer", True)
    assert route_metric(service.store, "退单来源记录行数")["decision"] == "MATCH"
    edited = edit_metric(service.store, metric["id"], "退单来源记录行数", "新口径待审核", expected_revision=2)
    assert edited["version"]["status"] == "DRAFT"
    assert edited["activeVersionId"] == published["activeVersionId"]
    with service.store.connect() as db:
        for bad in ({**plan, "aggregate": "COUNT_OPERATIONS"},
                    {**plan, "groupBy": ["patient_name"]},
                    {**plan, "filters": [{"field": "origin_operating_unit", "op": "eq", "value": "OR_9"}]},
                    {**plan, "filters": [{"field": "record_date", "op": "range", "value": ["tomorrow", "later"]}]},
                    {**plan, "entity": "unknown"}):
            with pytest.raises(AppError):
                compile_plan(db, bad)
    with pytest.raises(AppError) as exc:
        basic_plan("本周退单来源记录行数")
    assert exc.value.code == "PLAN_NEEDS_MODEL"
    with pytest.raises(AppError) as exc:
        validate_question_plan("一号手术室退单来源记录行数", plan)
    assert exc.value.code == "PLAN_CONDITION_MISSING"
    with pytest.raises(AppError) as exc:
        validate_question_plan("按日期统计退单来源记录行数", plan)
    assert exc.value.code == "PLAN_CONDITION_MISSING"
    with pytest.raises(AppError) as exc:
        validate_question_plan("首台来源记录行数", plan)
    assert exc.value.code == "PLAN_ENTITY_MISMATCH"
    blocked = create_metric(service.store, "退单台次", "统计退单台次")
    blocked = generate_sql(service.store, blocked["id"], 1, gateway)
    assert blocked["version"]["status"] == "SQL_DRAFT"
    assert blocked["version"]["validation"]["reason"] == "SEMANTIC_GAP"
    duplicate = create_metric(service.store, "退单来源记录行数", "统计退单来源记录行数")
    duplicate = generate_sql(service.store, duplicate["id"], 1, gateway)
    publish_metric(service.store, duplicate["id"], 2, "reviewer", True)
    assert route_metric(service.store, "退单来源记录行数")["decision"] == "AMBIGUOUS"
    seen = []
    class NoMatchJudge:
        def call(self, stage, payload, *_args):
            seen.extend(item["id"] for item in payload["candidates"])
            return {"decision": "NO_MATCH", "metricId": None, "reason": "different source"}
    assert route_metric(service.store, "首台来源记录行数", NoMatchJudge())["decision"] == "NO_MATCH"
    assert set(seen) == {metric["id"], duplicate["id"]}
