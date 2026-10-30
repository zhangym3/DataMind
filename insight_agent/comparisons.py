"""Audited result arithmetic; no LLM, interpolation, or raw patient records."""
from __future__ import annotations

import calendar
from datetime import date
from fractions import Fraction

from .core import digest, dumps, require


def shifted(value, months):
    current = date.fromisoformat(value)
    number = current.year * 12 + current.month - 1 + months
    year, month = divmod(number, 12)
    return date(year, month + 1, min(current.day, calendar.monthrange(year, month + 1)[1])).isoformat()


def exact(value):
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def compare_runs(store, session_id, run_ids, kind, job=None):
    require(kind in ("year", "period", "trend") and 2 <= len(run_ids) <= 24 and len(set(run_ids)) == len(run_ids), "COMPARISON_INVALID", "请选择 2 至 24 个不同的分析运行")
    runs = [store.get("run", id) for id in run_ids]
    require(all(r["sessionId"] == session_id and r.get("committed") and r["mode"] != "PLAN" and r.get("review", {}).get("status") == "PASS" for r in runs), "COMPARISON_INVALID", "仅可比较同会话中已提交并复核通过的分析")
    signature = lambda r: digest({k: r["contract"].get(k) for k in ("baselineHash", "definitionManifestHash", "domainPack", "candidates", "engineVersion", "mappings")})
    require(len({signature(r) for r in runs}) == 1, "COMPARISON_INCOMPARABLE", "数据基线、规则、分组或单位不一致")
    def window(run):
        candidates = [c["rule"] for c in run["contract"]["candidates"] if c["status"] == "CLEAR"]
        starts = {run["contract"]["parameters"][r["timeWindow"]["startParameter"]] for r in candidates}
        ends = {run["contract"]["parameters"][r["timeWindow"]["endParameter"]] for r in candidates}
        require(len(starts) == len(ends) == 1, "COMPARISON_INCOMPARABLE", "指标窗口参数不一致")
        return next(iter(starts)), next(iter(ends))
    runs.sort(key=lambda r: window(r)[0])
    windows = [window(r) for r in runs]
    require(all(a[1] <= b[0] for a, b in zip(windows, windows[1:])), "COMPARISON_INCOMPARABLE", "比较窗口不能重叠")
    groupings = [{k: v for k, v in r["contract"]["parameters"].items() if k not in {p for c in r["contract"]["candidates"] if c["status"] == "CLEAR" for p in (c["rule"]["timeWindow"]["startParameter"], c["rule"]["timeWindow"]["endParameter"] )}} for r in runs]
    require(len({digest(g) for g in groupings}) == 1, "COMPARISON_INCOMPARABLE", "比较的查询分组必须一致")
    if kind == "year":
        require(len(runs) == 2 and tuple(shifted(v, 12) for v in windows[0]) == windows[1], "COMPARISON_INCOMPARABLE", "同比需要对齐上一年相同窗口")
    else:
        lengths = [(date.fromisoformat(e) - date.fromisoformat(s)).days for s, e in windows]
        monthly = all(s.endswith("-01") and shifted(s, 1) == e for s, e in windows)
        require(len(set(lengths)) == 1 or monthly, "COMPARISON_INCOMPARABLE", "比较窗口需等长或为完整自然月")
        if kind == "period":
            require(len(runs) == 2 and windows[0][1] == windows[1][0], "COMPARISON_INCOMPARABLE", "环比需要相邻且可比的窗口")
    by_point = []
    labels = {}
    for run in runs:
        point = {}
        for metric in run["results"]:
            for group in metric.get("groups", []):
                key = (metric["metricId"], dumps(group.get("group", {})))
                labels[key] = (metric["name"], metric.get("unit"), group.get("group", {}))
                point[key] = group
        by_point.append(point)
    series = []
    for key, (name, unit, group) in sorted(labels.items()):
        points = []
        for run, window_value, point in zip(runs, windows, by_point):
            source = point.get(key)
            points.append({"runId": run["id"], "window": list(window_value), "outcome": source["outcome"] if source else "SKIP", "reasonCode": source.get("reasonCode") if source else "GROUP_NOT_AVAILABLE", "displayValue": source.get("displayValue") if source else None, "exact": {"numerator": source["exactNumerator"], "denominator": source["exactDenominator"]} if source and source["outcome"] == "VALUE" else None})
        item = {"metricId": key[0], "name": name, "unit": unit, "group": group, "points": points}
        if kind != "trend":
            if all(p["exact"] for p in points):
                base, current = [Fraction(int(p["exact"]["numerator"]), int(p["exact"]["denominator"])) for p in points]
                item.update(difference=exact(current - base), differenceUnit="percentage_point" if unit == "percent" else unit, relativeChangePercent=exact((current - base) / base * 100) if base else None, rateReason=None if base else "ZERO_BASE")
            else:
                item.update(reasonCode="POINT_NOT_AVAILABLE")
        series.append(item)
    require(series, "COMPARISON_EMPTY", "没有可比较的结果分组")
    value = {"sessionId": session_id, "kind": kind, "runIds": [r["id"] for r in runs], "sourceDigests": [r["resultDigest"] for r in runs], "windows": windows, "series": series}
    with store.transaction() as db:
        if job:
            active = db.execute("SELECT lease,status,cancel FROM jobs WHERE id=?", (job["id"],)).fetchone()
            require(active and active[0] == job["lease"] and active[1] == "RUNNING" and not active[2], "CANCELLED", "比较任务已取消或租约失效")
        existing = next((c for c in store.list("comparison", db) if c["comparisonHash"] == digest(value)), None)
        return existing or store.put("comparison", value | {"comparisonHash": digest(value), "jobId": job["id"] if job else None}, db)


def export_comparison(value):
    """Redact display labels only; exact numbers and source hashes must survive."""
    from copy import deepcopy
    from .models import redact_text
    result = deepcopy(value)
    for series in result["series"]:
        series["name"] = redact_text(series["name"])
        series["group"] = {k: redact_text(str(v)) for k, v in series["group"].items()}
    return result
