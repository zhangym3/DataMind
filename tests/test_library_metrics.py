"""Metric browsing chooses real version snapshots and keeps private data isolated."""
import copy
import json

import pytest

from insight_agent.core import AppError
from insight_agent.service import AppService


def manual(service, name="手术总量", draft=None):
    spec = {"name": name, "description": "按唯一手术事件编号统计全部手术量", "actor": "合成维护人",
            "domain": "surgery", "entity": "surgery_event", "method": "count_distinct",
            "dateField": "operation_date", "keyField": "surgery_event_id", "unit": "case", "scale": 0}
    return service.dispatch("definition.manual.save", {"spec": spec, **(
        {"definition_id": draft["id"], "revision": draft["revision"]} if draft else {"name": "合成指标集"})})


def clone(service, draft):
    return service.dispatch("definition.clone", {"definition_id": draft["id"], "revision": draft["revision"]})


def browse(service, **args):
    return service.dispatch("library.metrics", args)


def test_latest_per_family_uses_creation_order_even_when_old_draft_is_edited(tmp_path):
    service = AppService(tmp_path)
    old = manual(service)
    latest = manual(service, "新版本新增指标", clone(service, old))
    other = manual(service, "另一指标集的指标")
    # Editing the older draft changes its updated time but cannot make it latest.
    old = manual(service, "仅在旧版的指标", old)
    result = browse(service)
    assert {v["id"] for v in result["versions"]} == {latest["id"], other["id"]}
    assert {r["name"] for r in result["items"]} == {"手术总量", "新版本新增指标", "另一指标集的指标"}
    assert all(r["definitionId"] != old["id"] for r in result["items"])
    historical = browse(service, version=old["id"])
    assert {r["name"] for r in historical["items"]} == {"手术总量", "仅在旧版的指标"}
    assert [v["id"] for v in historical["versions"]] == [old["id"]]


def test_latest_ties_keep_newer_row_and_empty_latest_does_not_fall_back(tmp_path):
    service = AppService(tmp_path)
    old = manual(service)
    latest = clone(service, old)
    with service.store.transaction() as db:
        latest = service.store.put("definition", latest | {"candidates": []}, db)
        db.execute("UPDATE objects SET created='2026-10-30T00:00:00Z' WHERE kind='definition'")
    result = browse(service)
    assert result["items"] == []
    assert result["versions"][0]["id"] == latest["id"]
    assert result["versions"][0]["metricCount"] == 0
    assert browse(service, version=old["id"])["items"]


@pytest.mark.parametrize(("changes", "code"), [({"sessionId": "private_session"}, "SCOPE_FORBIDDEN"),
                                               ({"deletedAt": "2026-10-30T00:00:00Z"}, "DEFINITION_DELETED")])
def test_private_and_deleted_versions_never_expose_metric_content(tmp_path, changes, code):
    service = AppService(tmp_path)
    shared = manual(service)
    hidden = clone(service, shared)
    hidden["candidates"][0]["name"] = "不可泄露的内容"
    with service.store.transaction() as db:
        service.store.put("definition", hidden | changes, db)
    result = browse(service)
    assert [v["id"] for v in result["versions"]] == [shared["id"]]
    assert "不可泄露" not in json.dumps(result, ensure_ascii=False)
    with pytest.raises(AppError) as error:
        browse(service, version=hidden["id"])
    assert error.value.code == code


def test_metric_projection_preserves_same_name_slots_units_and_pending_reasons(tmp_path):
    service = AppService(tmp_path)
    draft = manual(service)
    count = draft["candidates"][0]
    candidates = [count]
    slots, nodes = [], []
    for index, method in enumerate(("SURGERY_COUNT", "READ_SUMMARY", "UNRESOLVED")):
        key = f"slot_{index}"
        context = "总手术量X台。"
        slots.append({"id": key, "kind": "NUMBER", "context": context, "startOffset": 4, "endOffset": 5})
        nodes.append({"locator": key, "text": context})
        candidates.append({"id": key, "name": "总手术量", "status": "REPORT", "locators": [key],
                           "plan": {"method": method, "reason": "缺少明确取数规则" if method == "UNRESOLVED" else ""}})
    candidates.append({"id": "skipped", "name": "跳过项", "status": "SKIP", "reason": "原文缺少统计条件", "locators": []})
    with service.store.transaction() as db:
        service.store.put("definition", draft | {"nodes": draft["nodes"] + nodes, "candidates": candidates,
                                                  "reportPlan": {"slots": slots}}, db)
    result = browse(service)
    rows = result["items"]
    assert len(rows) == 5 and len({r["id"] for r in rows}) == 5
    assert rows[0]["description"] == "按唯一手术事件编号统计全部手术量"
    assert rows[0]["method"] == "去重计数" and rows[0]["unit"] == "台次" and rows[0]["editable"]
    assert [r["name"] for r in rows[1:4]] == ["总手术量"] * 3
    assert [r["method"] for r in rows[1:4]] == ["总手术量计数", "读取已有汇总", "待明确取数方式"]
    assert [r["unit"] for r in rows[1:4]] == ["台"] * 3
    assert [r["status"] for r in rows[1:4]] == ["REPORT", "REPORT", "SKIP"]
    assert rows[3]["reason"] == "缺少明确取数规则" and rows[4]["reason"] == "原文缺少统计条件"
    assert all(r["description"] == "总手术量X台。" for r in rows[1:4])


def test_published_disabled_and_legacy_versions_remain_traceable_and_read_only(tmp_path):
    service = AppService(tmp_path)
    draft = manual(service)
    version = service.dispatch("definition.publish", {"definition_id": draft["id"], "revision": draft["revision"],
                                                        "actor": "审核人", "reviewed": True})
    service.dispatch("definition.disable", {"definition_id": version["id"], "disabled": True})
    with service.store.transaction() as db:
        legacy = copy.deepcopy(draft)
        legacy.pop("familyId")
        legacy["id"] = "legacy"
        service.store.put("definition", legacy, db)
    before = service.store.list("definition")
    result = browse(service)
    published = next(r for r in result["items"] if r["definitionId"] == version["id"])
    assert published["disabled"] and published["definitionStatus"] == "PUBLISHED" and published["editable"]
    assert {v["id"] for v in result["versions"]} == {version["id"], "legacy"}
    assert service.store.list("definition") == before
    assert not service.store.jobs() and not service.store.list("model_call")


def test_bootstrap_revisions_change_after_edit_so_metric_list_can_refresh(tmp_path):
    service = AppService(tmp_path)
    draft = manual(service)
    before = service.bootstrap()["definitions"][0]
    manual(service, "新增第二个指标", draft)
    after = service.bootstrap()["definitions"][0]
    assert before["created"] == after["created"]
    assert before["revision"] < after["revision"]
    assert before["metricCount"] == 1 and after["metricCount"] == 2
    assert len(browse(service)["items"]) == 2


@pytest.mark.parametrize("version", [None, 1, [], {}, "", "a" * 201])
def test_invalid_version_is_rejected(tmp_path, version):
    service = AppService(tmp_path)
    with pytest.raises(AppError) as error:
        browse(service, version=version)
    assert error.value.code == "INVALID_VERSION"
