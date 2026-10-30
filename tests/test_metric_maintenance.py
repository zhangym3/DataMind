"""Metric maintenance uses real version snapshots, contracts and local runs."""
import copy

import pytest
from docx import Document
from openpyxl import Workbook

from insight_agent import definitions
from insight_agent.core import AppError
from insight_agent.files import import_file
from insight_agent.report_plan import calculate_slot, render_report
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_metric_management import publish, save, spec
from test_integration import prepared  # noqa: F401
from test_report_plan import FillGateway


def selected(version, *ids):
    return [{"definition_id": version["id"], "revision": version["revision"], "metric_id": id} for id in ids]


def delete(service, items, **changes):
    return service.dispatch("definition.metrics.delete", {"selections": items, "confirmation": "确认删除", "actor": "合成维护人", **changes})


def edit(service, version, id, changes):
    return service.dispatch("definition.metric.edit", {"definition_id": version["id"], "revision": version["revision"], "metric_id": id, "changes": changes})


def report(service, tmp_path):
    word = Document()
    word.add_paragraph("统计口径：手术编号唯一标识一次手术，按手术日期统计。")
    word.add_paragraph("总手术量X台，急诊手术量X台。")
    path = tmp_path / "合成指标说明.docx"
    word.save(path)
    attachment = import_file(service.store, path, "library")
    service.route(attachment["id"], "METRIC_DEFINITION")
    draft = service.create_definition(attachment["id"])
    return definitions.extract(service.store, draft["id"], FillGateway(), "synthetic-fill")


def test_published_manual_edit_creates_version_only_on_success(tmp_path):
    service = AppService(tmp_path)
    version = publish(service, save(service))
    id = version["candidates"][0]["id"]
    with pytest.raises(AppError):
        edit(service, version, id, spec() | {"method": "invalid"})
    assert service.store.list("definition") == [version]
    result = edit(service, version, id, spec() | {"name": "修订总量", "description": "修订口径"})
    assert result["versionLabel"] == "v2" and result["parentId"] == version["id"] and result["status"] == "DRAFT"
    assert service.managed_definition(version["id"]) == version
    assert service.dispatch("library.metrics", {})["items"][0]["name"] == "修订总量"
    assert publish(service, result)["manifestHash"] != version["manifestHash"]


def test_word_rule_edit_and_skip_edit_support_published_versions(prepared):  # noqa: F811
    service, _, version, _ = prepared
    id = version["candidates"][0]["id"]
    updated = edit(service, version, id, {"name": "修订延迟台次", "description": "人工维护说明", "actor": "维护人"})
    assert updated["candidates"][0]["rule"]["name"] == "修订延迟台次"
    assert service.dispatch("library.metrics", {})["items"][0]["description"] == "人工维护说明"
    assert publish(service, updated)["manifest"]["metricNotes"][id]["description"] == "人工维护说明"
    updated = service.managed_definition(updated["id"])
    skipped = edit(service, updated, id, {"name": "待补充指标", "description": "需要更多字段", "actor": "维护人", "status": "SKIP", "reason": "缺少业务口径"})
    assert skipped["candidates"][0]["status"] == "SKIP" and "rule" not in skipped["candidates"][0]
    assert service.managed_definition(version["id"]) == version


def test_report_edit_updates_candidate_and_slot_and_preserves_semantic_checks(tmp_path):
    service = AppService(tmp_path / "home")
    version = publish(service, report(service, tmp_path))
    candidate = version["candidates"][0]
    changes = {"name": "实际总手术量", "description": "按原文统计实际手术量", "actor": "维护人", "plan": candidate["plan"]}
    before = service.store.list("definition")
    with pytest.raises(AppError) as exc:
        edit(service, version, candidate["id"], changes | {"plan": candidate["plan"] | {"method": "EMERGENCY_COUNT"}})
    assert exc.value.code == "MODEL_SEMANTIC_INVALID"
    assert service.store.list("definition") == before
    result = edit(service, version, candidate["id"], changes)
    assert result["candidates"][0]["plan"] == result["reportPlan"]["slots"][0]["plan"]
    assert result["amendments"] == []
    assert publish(service, result)["status"] == "PUBLISHED"
    assert service.managed_definition(version["id"]) == version


def test_batch_delete_multiple_versions_and_families_is_durable(tmp_path):
    service = AppService(tmp_path)
    first = save(service)
    first = save(service, spec() | {"name": "保留项"}, first)
    first = publish(service, first)
    second = save(service, spec() | {"name": "另一个指标集"})
    original = copy.deepcopy(first)
    items = selected(first, first["candidates"][0]["id"]) + selected(second, second["candidates"][0]["id"])
    result = delete(service, items)
    assert result["deletedCount"] == 2 and len(result["versions"]) == 2
    rows = service.dispatch("library.metrics", {})["items"]
    assert [r["name"] for r in rows] == ["保留项"]
    assert service.managed_definition(first["id"]) == original
    assert len(service.dispatch("library.metrics", {"version": first["id"]})["items"]) == 2
    assert sum(d["metricCount"] for d in service.bootstrap()["definitions"] if d["id"] != first["id"]) == 1
    assert [r["name"] for r in AppService(tmp_path).dispatch("library.metrics", {})["items"]] == ["保留项"]
    assert not service.store.jobs() and not service.store.list("model_call")


@pytest.mark.parametrize("failure", ["confirmation", "actor", "stale", "missing", "duplicate", "busy", "private", "deleted"])
def test_batch_failure_leaves_no_partial_deletes_or_clones(tmp_path, failure):
    service = AppService(tmp_path)
    first = publish(service, save(service))
    second = save(service, spec() | {"name": "另一项"})
    if failure in ("private", "deleted"):
        with service.store.transaction() as db:
            second = service.store.put("definition", second | ({"sessionId": "private"} if failure == "private" else {"deletedAt": "now"}), db)
    if failure == "busy":
        with service.store.transaction() as db:
            service.store.enqueue(db, "extract", second["id"], {})
    items = selected(first, first["candidates"][0]["id"]) + selected(second, second["candidates"][0]["id"])
    changes = {}
    if failure == "confirmation":
        changes["confirmation"] = first["id"]
    if failure == "actor":
        changes["actor"] = ""
    if failure == "stale":
        items[-1]["revision"] -= 1
    if failure == "missing":
        items[-1]["metric_id"] = "missing"
    if failure == "duplicate":
        items.append(items[0])
    before = service.store.list("definition")
    with service.store.connect() as db:
        count = db.execute("SELECT count(*) FROM events").fetchone()[0]
    with pytest.raises(AppError):
        delete(service, items, **changes)
    assert service.store.list("definition") == before
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == count


def test_report_deletion_preserves_all_positions_and_prevents_value_reads(tmp_path):
    service = AppService(tmp_path / "home")
    draft = report(service, tmp_path)
    id = draft["candidates"][0]["id"]
    delete(service, selected(draft, id))
    draft = service.managed_definition(draft["id"])
    version = publish(service, draft)
    contract = {"reportPlan": version["reportPlan"], "reportDocumentHash": version["documentHash"], "metricExclusions": version["manifest"]["metricExclusions"]}
    # A valid, matching summary must never restore a deleted slot's value.
    results = [calculate_slot(s, [], contract) for s in version["reportPlan"]["slots"]]
    assert results[0]["reasonCode"] == "METRIC_DELETED" and "groups" not in results[0]
    assert len(results) == 2 and len(service.dispatch("library.metrics", {})["items"]) == 1
    assert "指标已删除，不参与计算" in render_report(version["reportPlan"], results)["paragraphs"][1]["text"]


def test_deleted_metric_cannot_be_edited_deleted_again_or_publish_empty_set(tmp_path):
    service = AppService(tmp_path)
    draft = save(service)
    id = draft["candidates"][0]["id"]
    delete(service, selected(draft, id))
    draft = service.managed_definition(draft["id"])
    with pytest.raises(AppError, match="已删除"):
        edit(service, draft, id, spec())
    with pytest.raises(AppError, match="已删除"):
        delete(service, selected(draft, id))
    with pytest.raises(AppError, match="空指标版本"):
        publish(service, draft)


def test_deleted_metric_references_survive_replacement_and_version_comparison(tmp_path):
    service = AppService(tmp_path)
    version = publish(service, save(service))
    id = version["candidates"][0]["id"]
    result = delete(service, selected(version, id))
    draft = service.managed_definition(result["versions"][0]["id"])
    assert definitions.compare_versions(version, draft)["removed"] == [id]
    replacement = copy.deepcopy(draft["candidates"])
    replacement[0]["id"] = replacement[0]["rule"]["metricId"] = "replacement_id"
    coverage = copy.deepcopy(draft["coverage"])
    for row in coverage:
        row["candidateIds"] = ["replacement_id" if x == id else x for x in row["candidateIds"]]
    with pytest.raises(AppError) as exc:
        definitions.save_candidates(service.store, draft["id"], replacement, coverage, draft["revision"])
    assert exc.value.code == "DELETED_METRIC_REFERENCE"
    assert service.managed_definition(draft["id"]) == draft


def test_report_all_deleted_cannot_publish_or_revive_after_reextract(tmp_path):
    service = AppService(tmp_path / "home")
    draft = report(service, tmp_path)
    ids = [c["id"] for c in draft["candidates"]]
    delete(service, selected(draft, *ids))
    draft = definitions.extract(service.store, draft["id"], FillGateway(), "synthetic-reextract")
    assert set(draft["metricExclusions"]) == set(ids)
    assert not service.dispatch("library.metrics", {"version": draft["id"]})["items"]
    with pytest.raises(AppError, match="空指标版本"):
        publish(service, draft)


def test_real_run_skips_deleted_metric_and_keeps_old_run_unchanged(tmp_path):
    service = AppService(tmp_path / "home")
    draft = save(service)
    draft = save(service, spec() | {"name": "保留项"}, draft)
    version = publish(service, draft)
    ids = [c["id"] for c in version["candidates"]]
    delete(service, selected(version, ids[0]))
    new = publish(service, service.store.list("definition")[0])
    book = Workbook()
    sheet = book.active
    sheet.append(["surgery_event_id", "operation_date"])
    sheet.append(["S1", "2026-10-01"])
    path = tmp_path / "合成数据.xlsx"
    book.save(path)

    def run(value, request):
        session = service.create_session(definition_id=value["id"])
        service.import_paths([str(path)], session["id"])
        assert run_once(service)
        session = service.store.get("session", session["id"])
        mapping = service.suggest_mapping(session["id"])
        assert set(mapping) == {c["id"] for c in value["candidates"] if c["id"] not in value.get("metricExclusions", {})}
        detail = service.session_detail(session["id"])
        assert detail["metricDefinition"]["metricCount"] == len(mapping)
        for m in mapping.values():
            m["coverage"] = {"complete": True, "evidence": "合成测试全量数据"}
        session = service.update_session(session["id"], session["revision"], {"mappings": mapping})
        result = service.submit_user_turn(session["id"], session["revision"], request, "", "ANALYZE", {"scope": "ALL_DATA"})
        assert run_once(service)
        job = next(j for j in service.store.jobs() if j["id"] == result["jobId"])
        assert job["status"] == "SUCCEEDED", job
        return service.store.get("run", job["result"]["runId"])

    old_run = run(version, "old")
    new_run = run(new, "new")
    assert [r["outcome"] for r in old_run["results"]] == ["VALUE", "VALUE"]
    assert new_run["results"][0]["reasonCode"] == "METRIC_DELETED"
    assert new_run["results"][1]["groups"][0]["displayValue"] == "1"
    assert new_run["contract"]["metricExclusions"] == new["manifest"]["metricExclusions"]
    assert service.store.get("run", old_run["id"]) == old_run
    assert not service.store.list("model_call")


def test_published_description_delete_preserves_bound_history(tmp_path):
    service = AppService(tmp_path)
    version = publish(service, save(service))
    session = service.create_session(definition_id=version["id"])
    result = service.dispatch("definition.delete", {"definition_id": version["id"], "revision": version["revision"], "confirmation": "确认删除"})
    assert result["retainedForHistory"]
    retained = service.session_metrics(session["id"])
    assert retained["manifestHash"] == version["manifestHash"]
    assert not service.dispatch("library.metrics", {})["items"]
    with pytest.raises(AppError):
        service.create_session(definition_id=version["id"])
