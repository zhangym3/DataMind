"""Library management and offline manual metrics exercise the real calculation path."""
import copy

import pytest
from openpyxl import Workbook

from insight_agent.core import AppError
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_document_workflow import uploaded, DocumentGateway  # noqa: F401
from test_integration import prepared  # noqa: F401


def spec(method="count_distinct"):
    return {"name": "合成手工指标", "description": "对当前窗口内手术记录按指定统计方法计算，flag=是为符合条件记录", "actor": "合成维护人",
            "domain": "surgery", "entity": "surgery_event", "method": method, "dateField": "operation_date", "keyField": "surgery_event_id",
            "valueField": "duration", "unit": "case" if method in ("count", "count_distinct") else "percent" if method == "ratio" else "minute", "scale": 2 if method == "ratio" else 0,
            **({"conditionField": "flag", "conditionType": "string", "conditionOperator": "eq", "conditionValue": "是"} if method == "ratio" else {})}


def save(service, value=None, draft=None, metric_id=None):
    args = {"spec": value or spec()}
    if draft:
        args.update(definition_id=draft["id"], revision=draft["revision"], metric_id=metric_id)
    return service.dispatch("definition.manual.save", args)


def publish(service, draft):
    return service.dispatch("definition.publish", {"definition_id": draft["id"], "revision": draft["revision"], "actor": "审核人", "reviewed": True})


@pytest.mark.parametrize(("method", "expected"), [("count", "3"), ("count_distinct", "3"), ("sum", "60"), ("mean", "20"), ("min", "10"), ("max", "30"), ("ratio", "66.67")])
def test_manual_create_publish_bind_and_calculate_offline(tmp_path, method, expected):
    service = AppService(tmp_path / "home")
    draft = save(service, spec(method))
    assert not service.store.list("attachment") and not service.store.jobs()
    version = publish(service, draft)
    assert version["manifest"]["evidenceDocuments"][0]["kind"] == "MANUAL"
    session = service.dispatch("session.create", {"definition_id": version["id"]})
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["surgery_event_id", "operation_date", "duration", "flag"])
    for key, minutes, flag in (("a", 10, "是"), ("b", 20, "否"), ("c", 30, "是")):
        sheet.append([key, "2026-10-01", minutes, flag])
    path = tmp_path / "合成手术数据.xlsx"
    workbook.save(path)
    service.import_paths([str(path)], session["id"])
    assert run_once(service)
    assert service.store.jobs()[0]["status"] == "SUCCEEDED"
    session = service.session_detail(session["id"])["session"]
    mapping = service.suggest_mapping(session["id"])
    for value in mapping.values():
        value["coverage"] = {"complete": True, "evidence": "合成测试的全部手术记录"}
    session = service.update_session(session["id"], session["revision"], {"mappings": mapping})
    service.submit_user_turn(session["id"], session["revision"], "manual-test", "", "ANALYZE", {"scope": "ALL_DATA"})
    assert run_once(service)
    job = service.store.jobs()[0]
    assert job["status"] == "SUCCEEDED", job
    result = service.store.get("run", job["result"]["runId"])
    assert result["committed"] and result["results"][0]["groups"][0]["displayValue"] == expected
    assert not service.store.list("model_call")
    assert AppService(service.store.home).session_metrics(session["id"])["manifestHash"] == version["manifestHash"]


def test_manual_edit_preserves_evidence_and_published_versions(tmp_path):
    service = AppService(tmp_path)
    draft = save(service)
    metric_id = draft["candidates"][0]["id"]
    old_amendment = copy.deepcopy(draft["amendments"][0])
    draft = save(service, spec() | {"name": "修改后的指标"}, draft, metric_id)
    assert len(draft["candidates"]) == 1 and draft["candidates"][0]["id"] == metric_id
    assert draft["amendments"][0] == old_amendment
    version = publish(service, draft)
    with pytest.raises(AppError, match="已发布版本不可修改"):
        save(service, draft=version, metric_id=metric_id)
    cloned = service.dispatch("definition.clone", {"definition_id": version["id"], "revision": version["revision"]})
    assert cloned["versionLabel"] == "v2" and cloned["parentId"] == version["id"]
    cloned = save(service, spec() | {"name": "第二项"}, cloned)
    assert len(publish(service, cloned)["candidates"]) == 2
    assert service.managed_definition(version["id"])["manifestHash"] == version["manifestHash"]


@pytest.mark.parametrize("changes", [{"name": ""}, {"actor": ""}, {"dateField": "非法字段"}, {"method": "eval"}, {"method": "ratio", "unit": "percent"}, {"unit": "currency"}, {"scale": 9}])
def test_invalid_manual_form_leaves_no_partial_definition(tmp_path, changes):
    service = AppService(tmp_path)
    with pytest.raises(AppError):
        save(service, spec() | changes)
    assert not service.store.list("definition")
    assert not service.store.jobs()


def test_review_disable_default_delete_and_mapping_reset(tmp_path):
    service = AppService(tmp_path)
    draft = save(service)
    with pytest.raises(AppError, match="显式发布"):
        service.dispatch("definition.publish", {"definition_id": draft["id"], "revision": draft["revision"], "actor": "复核人", "reviewed": False})
    version = publish(service, draft)
    service.dispatch("definition.default", {"definition_id": version["id"]})
    assert service.dispatch("session.create", {})["definitionId"] is None
    session = service.create_session(definition_id=version["id"])
    with service.store.transaction() as db:
        session = service.store.put("session", session | {"mappings": {"old": {"fields": "stale"}}, "mappingRevisionId": "old"}, db)
    other = publish(service, save(service, spec() | {"name": "另一指标集"}))
    session = service.dispatch("session.update", {"session_id": session["id"], "revision": session["revision"], "changes": {"definitionId": other["id"]}})
    assert not session["mappings"] and session["mappingRevisionId"] is None
    service.dispatch("definition.disable", {"definition_id": version["id"], "disabled": True})
    assert not service.managed_definition(version["id"])["default"]
    with pytest.raises(AppError):
        service.create_session(definition_id=version["id"])
    draft = save(service)
    service.dispatch("definition.delete", {"definition_id": draft["id"], "revision": draft["revision"], "confirmation": "确认删除"})
    assert draft["id"] not in {d["id"] for d in service.bootstrap()["definitions"]}


def test_private_upload_stays_in_its_session(uploaded):  # noqa: F811
    service, session, _, _ = uploaded
    assert run_once(service, DocumentGateway())
    private = service.session_metrics(session["id"])
    assert private["id"] not in {d["id"] for d in service.bootstrap()["definitions"]}
    for method, args in (("definition.get", {"definition_id": private["id"]}),
                         ("definition.clone", {"definition_id": private["id"], "revision": private["revision"]}),
                         ("session.create", {"definition_id": private["id"]})):
        with pytest.raises(AppError) as error:
            service.dispatch(method, args)
        assert error.value.code == "SCOPE_FORBIDDEN"
    other = service.create_session()
    with pytest.raises(AppError) as error:
        service.update_session(other["id"], other["revision"], {"definitionId": private["id"]})
    assert error.value.code == "SCOPE_FORBIDDEN"


def test_manual_append_to_word_draft_and_busy_guard(prepared):  # noqa: F811
    service, _, old, _ = prepared
    draft = service.dispatch("definition.clone", {"definition_id": old["id"], "revision": old["revision"]})
    draft = save(service, draft=draft)
    assert len(publish(service, draft)["candidates"]) == 2
    draft = service.dispatch("definition.clone", {"definition_id": old["id"], "revision": old["revision"]})
    service.queue("extract", draft["id"], {"definitionId": draft["id"]})
    with pytest.raises(AppError, match="解析期间"):
        save(service, draft=draft)


def test_invalid_edit_and_stale_revision_do_not_change_existing_draft(tmp_path):
    service = AppService(tmp_path)
    draft = save(service)
    with pytest.raises(AppError):
        save(service, spec() | {"unit": "currency"}, draft)
    assert service.managed_definition(draft["id"]) == draft
    updated = save(service, spec() | {"name": "第二项"}, draft)
    with pytest.raises(AppError) as error:
        save(service, draft=draft)
    assert error.value.code == "REVISION_CONFLICT"
    assert service.managed_definition(updated["id"]) == updated


def test_advanced_edit_keeps_only_unchanged_manual_forms(tmp_path):
    service = AppService(tmp_path)
    draft = save(service)
    draft = save(service, spec() | {"name": "另一项"}, draft)
    ids = [candidate["id"] for candidate in draft["candidates"]]
    args = {"definition_id": draft["id"], "revision": draft["revision"], "candidates": draft["candidates"], "coverage": draft["coverage"]}
    draft = service.dispatch("definition.save", args)
    assert set(draft["manualMetrics"]) == set(ids)
    changed = copy.deepcopy(draft["candidates"])
    changed[0]["name"] = changed[0]["rule"]["name"] = "高级维护名称"
    draft = service.dispatch("definition.save", args | {"revision": draft["revision"], "candidates": changed})
    assert set(draft["manualMetrics"]) == {ids[1]}
    with pytest.raises(AppError, match="仅手工指标"):
        save(service, draft=draft, metric_id=ids[0])
    assert len(publish(service, draft)["candidates"]) == 2


@pytest.mark.parametrize("action", ["save", "amend", "metadata"])
def test_word_library_edits_are_blocked_during_extraction(prepared, action):  # noqa: F811
    service, _, old, _ = prepared
    draft = service.dispatch("definition.clone", {"definition_id": old["id"], "revision": old["revision"]})
    service.dispatch("job.queue", {"kind": "extract", "scope": draft["id"], "payload": {"definitionId": draft["id"]}})
    values = {"save": {"candidates": draft["candidates"], "coverage": draft["coverage"]},
              "amend": {"text": "补充政策", "actor": "维护人", "scope": "全部", "replaces": []},
              "metadata": {"version_label": "next", "notes": "说明"}}[action]
    with pytest.raises(AppError) as error:
        service.dispatch("definition." + action, {"definition_id": draft["id"], "revision": draft["revision"], **values})
    assert error.value.code == "DEFINITION_BUSY"
    assert service.managed_definition(draft["id"]) == draft
