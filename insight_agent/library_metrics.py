"""Metric rows from shared versions; document snapshots remain the authority."""
from __future__ import annotations

from .core import require
from .definitions import require_not_deleted


METHODS = {"count": "记录数", "count_distinct": "去重计数", "sum": "求和", "mean": "平均值",
           "min": "最小值", "max": "最大值", "ratio": "占比",
           "SURGERY_COUNT": "总手术量计数", "EMERGENCY_COUNT": "急诊手术量计数",
           "DURATION_PATIENTS": "时差条件人数", "READ_SUMMARY": "读取已有汇总", "UNRESOLVED": "待明确取数方式"}
UNITS = {"case": "台次", "person": "人", "item": "项", "minute": "分钟", "day": "天",
         "bed_day": "床日", "currency": "金额", "percent": "%", "ratio": "比例"}
VERSION_FIELDS = ("id", "name", "familyId", "versionLabel", "status", "disabled", "sourceKind", "created", "revision")


def metric_row(definition, candidate):
    rule, plan = candidate.get("rule") or {}, candidate.get("plan") or {}
    manual = definition.get("manualMetrics", {}).get(candidate["id"], {})
    locators = set(candidate.get("locators", []))
    sources = [n["text"] for n in [*definition["nodes"], *definition.get("amendments", [])] if n["locator"] in locators]
    method = plan.get("method") or rule.get("expression", {}).get("operator")
    slot = next((s for s in definition.get("reportPlan", {}).get("slots", []) if s["id"] == candidate["id"]), {})
    unit = UNITS.get(rule.get("unit"), rule.get("unit", ""))
    if plan:
        from .report_plan import expected_unit
        unit = "不适用" if slot.get("kind") in ("DATE", "TEXT") else expected_unit(slot) if slot else ""
    note = definition.get("metricNotes", {}).get(candidate["id"], {})
    return {"id": candidate["id"], "definitionId": definition["id"], "definitionRevision": definition["revision"], "definitionName": definition["name"],
            "versionLabel": definition.get("versionLabel", "v1"), "definitionStatus": definition["status"],
            "disabled": bool(definition.get("disabled")), "name": candidate["name"],
            "description": manual.get("description") or note.get("description") or "\n".join(sources),
            "method": METHODS.get(method, method or "未定义"), "unit": unit or "未明确",
            "status": "SKIP" if candidate["status"] == "SKIP" or method == "UNRESOLVED" else candidate["status"],
            "reason": candidate.get("reason") or plan.get("reason", ""), "kind": slot.get("kind"),
            "editable": True}


def list_metrics(store, version="latest"):
    require(isinstance(version, str) and 0 < len(version) <= 200, "INVALID_VERSION", "指标版本筛选无效")
    if version == "latest":
        # Store order uses creation time, so revisiting v1 cannot displace v2.
        selected, families = [], set()
        for definition in store.list("definition"):
            if definition.get("sessionId") or definition.get("deletedAt"):
                continue
            family = definition.get("familyId") or definition["id"]
            if family not in families:
                selected.append(definition)
                families.add(family)
    else:
        definition = store.get("definition", version)
        require_not_deleted(definition)
        require(not definition.get("sessionId"), "SCOPE_FORBIDDEN", "对话内指标说明请在所属对话查看和维护")
        selected = [definition]
    rows = [metric_row(d, c) for d in selected for c in d["candidates"] if c["id"] not in d.get("metricExclusions", {})]
    return {"items": rows, "versions": [{k: d.get(k) for k in VERSION_FIELDS} | {"metricCount": sum(c["id"] not in d.get("metricExclusions", {}) for c in d["candidates"])} for d in selected]}
