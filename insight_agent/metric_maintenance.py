"""Atomic edits and removal of shared metrics, preserving published evidence."""
from __future__ import annotations

import copy

from . import definitions, manual_metrics
from .core import DELETE_CONFIRMATION_TEXT, LIMITS, now, require


def _source(store, definition_id, revision, db):
    value = store.get("definition", definition_id, db)
    definitions.require_not_deleted(value)
    require(not value.get("sessionId"), "SCOPE_FORBIDDEN", "对话内指标请在所属对话维护")
    require(type(revision) is int and value["revision"] == revision, "REVISION_CONFLICT", "指标版本已变化，请刷新后重新选择")
    definitions._require_editable(db, definition_id)
    return value


def _candidate(value, metric_id):
    item = next((c for c in value["candidates"] if c["id"] == metric_id), None)
    require(item is not None and metric_id not in value.get("metricExclusions", {}), "METRIC_UNAVAILABLE", "指标不存在或已删除，请刷新")
    return item


def _draft(store, source, db):
    return manual_metrics.clone(store, source["id"], source["revision"], db=db) if source["status"] == "PUBLISHED" else source


def edit(store, definition_id, revision, metric_id, changes):
    require(isinstance(changes, dict), "INVALID_METRIC", "指标修改参数无效")
    with store.transaction() as db:
        source = _source(store, definition_id, revision, db)
        candidate = _candidate(source, metric_id)
        draft = _draft(store, source, db)
        if metric_id in source.get("manualMetrics", {}):
            result = manual_metrics.save(store, changes, draft["id"], draft["revision"], metric_id, db=db)
        else:
            require(set(changes) <= {"name", "description", "actor", "plan", "status", "reason", "rule"}, "INVALID_METRIC", "指标修改包含未知字段")
            for key, limit in (("name", 200), ("description", 10000), ("actor", 200)):
                require(isinstance(changes.get(key), str) and 0 < len(changes[key].strip()) <= limit, "INVALID_METRIC", "请填写指标名称、维护说明和维护人")
            updated = copy.deepcopy(candidate)
            updated["name"] = changes["name"].strip()
            if draft.get("reportPlan"):
                require(set(changes) <= {"name", "description", "actor", "plan"} and isinstance(changes.get("plan"), dict), "INVALID_METRIC", "请填写完整取数计划")
                slot = next(s for s in draft["reportPlan"]["slots"] if s["id"] == metric_id)
                plan = copy.deepcopy(changes["plan"])
                plan.update(slot=metric_id, name=updated["name"])
                from .report_plan import validate_intent
                validate_intent(slot, plan)
                slot["plan"] = plan
                updated["plan"] = plan
            else:
                require("plan" not in changes, "INVALID_METRIC", "此指标使用计算规则")
                updated["status"] = changes.get("status", candidate["status"])
                if updated["status"] == "CLEAR":
                    updated.pop("reason", None)
                    updated["rule"] = copy.deepcopy(changes.get("rule", candidate.get("rule")))
                    require(isinstance(updated["rule"], dict), "INVALID_METRIC", "可计算指标需有完整规则")
                    updated["rule"].update(name=updated["name"], metricId=metric_id)
                else:
                    updated.pop("rule", None)
                    updated["reason"] = changes.get("reason", "").strip()
            draft["candidates"] = [updated if c["id"] == metric_id else c for c in draft["candidates"]]
            draft.setdefault("metricNotes", {})[metric_id] = {k: changes[k].strip() for k in ("description", "actor")} | {"at": now()}
            draft = store.put("definition", draft, db, expected=draft["revision"])
            result = draft if draft.get("reportPlan") else definitions.save_candidates(
                store, draft["id"], draft["candidates"], draft["coverage"], draft["revision"], draft["reviewPolicy"], db=db)
        store.event(db, result["id"], "definition.metric_edited", {"metricId": metric_id, "sourceId": source["id"]})
        return result


def delete_many(store, selections, confirmation, actor):
    """All selected versions succeed together, including any necessary clones.

    Deletion is a durable exclusion: the source positions remain accountable in
    Word output, and frozen runs can continue to reproduce their old contracts.
    """
    require(confirmation == DELETE_CONFIRMATION_TEXT, "DELETE_CONFIRMATION", f"请输入“{DELETE_CONFIRMATION_TEXT}”确认删除")
    require(isinstance(actor, str) and 0 < len(actor.strip()) <= 200, "INVALID_METRIC", "请填写维护人")
    require(isinstance(selections, list) and 0 < len(selections) <= LIMITS["candidates"], "INVALID_SELECTION", "请选择需要删除的指标，单次不能超过指标数量上限")
    grouped, seen = {}, set()
    with store.transaction() as db:
        for item in selections:
            require(isinstance(item, dict) and set(item) == {"definition_id", "revision", "metric_id"}
                    and isinstance(item["definition_id"], str) and isinstance(item["metric_id"], str), "INVALID_SELECTION", "指标选择参数无效")
            key = (item["definition_id"], item["metric_id"])
            require(key not in seen, "INVALID_SELECTION", "不能重复选择同一指标")
            seen.add(key)
            source = _source(store, item["definition_id"], item["revision"], db)
            _candidate(source, item["metric_id"])
            group = grouped.setdefault(source["id"], {"source": source, "ids": []})
            group["ids"].append(item["metric_id"])
        versions = []
        for group in grouped.values():
            source, ids = group["source"], group["ids"]
            draft = _draft(store, source, db)
            excluded = draft.setdefault("metricExclusions", {})
            for metric_id in ids:
                candidate = _candidate(source, metric_id)
                excluded[metric_id] = {"name": candidate["name"], "actor": actor.strip(), "at": now(), "reason": "指标已删除，不参与计算"}
            result = store.put("definition", draft, db, expected=draft["revision"])
            store.event(db, result["id"], "definition.metrics_deleted", {"metricIds": ids, "sourceId": source["id"], "actor": actor.strip()})
            versions.append({"sourceId": source["id"], "id": result["id"], "versionLabel": result["versionLabel"], "deletedCount": len(ids)})
        return {"deletedCount": len(seen), "versions": versions}
