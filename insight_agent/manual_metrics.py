"""Offline form compilation; manual statements remain immutable evidence."""
from __future__ import annotations

import copy
import re
from contextlib import nullcontext

from .core import LIMITS, dumps, now, require, sha, uid
from .definitions import _next_version_label, evidence_documents, require_not_deleted, save_candidates
from .skills import runtime_manifest


def create(store, name, db=None):
    require(isinstance(name, str) and 0 < len(name.strip()) <= 120, "INVALID_DEFINITION", "请填写 1 至 120 字的指标集名称")
    text = "手工维护指标集：" + name.strip()
    source = store.blob(text.encode())
    with store.transaction() if db is None else nullcontext(db) as db:
        value = store.put("definition", {"name": name.strip(), "familyId": uid("family_"), "versionLabel": "v1",
            "parentId": None, "notes": "", "default": False, "status": "DRAFT", "sourceKind": "MANUAL",
            "attachmentId": None, "documentHash": source, "manualSourceHash": source,
            "parser": {"parserVersion": "manual-form/1.0"}, "convertedSha256": None,
            "nodes": [{"locator": "manual:source", "text": text, "quoteSha256": sha(text.encode())}],
            "amendments": [], "candidates": [], "coverage": [{"locator": "manual:source", "disposition": "CONTEXT", "candidateIds": [], "reason": "手工指标集名称"}],
            "reviewPolicy": "DETERMINISTIC_APPROVED", "manifestHash": None, "disabled": False}, db)
        store.event(db, value["id"], "definition.created", {"sourceKind": "MANUAL"})
        return value


def clone(store, definition_id, revision, *, db=None):
    with store.transaction() if db is None else nullcontext(db) as db:
        source = store.get("definition", definition_id, db)
        require_not_deleted(source)
        require(not source.get("sessionId"), "SCOPE_FORBIDDEN", "对话内指标说明请在所属对话维护")
        require(source["revision"] == revision, "REVISION_CONFLICT", "指标版本已变化，请刷新")
        require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('QUEUED','RUNNING')", (definition_id,)).fetchone(), "DEFINITION_BUSY", "请等待当前解析完成")
        draft = copy.deepcopy(source)
        for key in ("id", "revision", "created", "updated", "manifest", "extractionProgress", "reportPlanProgress"):
            draft.pop(key, None)
        family = source.get("familyId", source["id"])
        draft.update(familyId=family, parentId=source["id"], versionLabel=_next_version_label(store, family, db),
                     status="DRAFT", default=False, disabled=False, manifestHash=None)
        result = store.put("definition", draft, db)
        store.event(db, result["id"], "definition.cloned", {"parentId": source["id"]})
        return result


def compile_rule(spec, metric_id, evidence):
    allowed = {"name", "description", "actor", "domain", "entity", "method", "dateField", "keyField", "valueField", "unit", "scale", "conditionField", "conditionType", "conditionOperator", "conditionValue"}
    require(isinstance(spec, dict) and set(spec) <= allowed, "INVALID_METRIC", "手工指标包含未知字段")
    for key in ("name", "description", "actor"):
        require(isinstance(spec.get(key), str) and 0 < len(spec[key].strip()) <= (10000 if key == "description" else 200), "INVALID_METRIC", "请填写指标名称、统计口径和维护人")
    method = spec.get("method")
    require(method in ("count", "count_distinct", "sum", "mean", "min", "max", "ratio"), "UNSUPPORTED_OPERATOR", "请选择支持的统计方法")
    domain, entity = spec.get("domain"), spec.get("entity")
    date_field, key_field, value_field = spec.get("dateField", ""), spec.get("keyField", ""), spec.get("valueField", "")
    fields = {}

    def field(name, value_type, unit, role):
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name), "INVALID_METRIC", "字段标识需以英文字母开头，仅包含字母、数字和下划线（最多 64 位）；实际列名可在字段映射中选择")
        item = {"id": name, "valueType": value_type, "unit": unit, "role": role}
        require(name not in fields or (fields[name]["valueType"], fields[name]["unit"]) == (value_type, unit), "INVALID_METRIC", "同一字段不能声明为不同类型")
        fields.setdefault(name, item)

    field(date_field, "date", "date", "time")
    distinct = method == "count_distinct"
    if distinct:
        field(key_field, "string", "none", "key")
    numeric = method in ("sum", "mean", "min", "max")
    if numeric:
        field(value_field, "decimal", spec.get("unit"), "measure")
    where = {"operator": "all_records"}
    condition = spec.get("conditionField", "")
    require(method != "ratio" or condition, "INVALID_METRIC", "比例指标请填写分子筛选条件；分母为统计窗口内全部记录")
    if condition:
        kind, op, value = spec.get("conditionType"), spec.get("conditionOperator"), spec.get("conditionValue")
        require(kind in ("string", "decimal") and op in ("eq", "ne", "gt", "gte", "lt", "lte") and isinstance(value, str) and value != "", "INVALID_METRIC", "筛选条件不完整")
        if kind == "decimal":
            require(re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?", value) and len(value) <= 80, "INVALID_METRIC", "数值筛选值需为有效十进制数字")
        unit = fields[condition]["unit"] if condition in fields else "none"
        field(condition, kind, unit, "measure" if kind == "decimal" else "dimension")
        literal = {"node": "literal", "valueType": kind, "value": value}
        if kind == "decimal":
            literal["unit"] = unit
        where = {"operator": op, "left": {"node": "field", "field": condition}, "right": literal}
    expression = {"operator": method, "where": where}
    if distinct:
        expression["fields"] = [key_field]
    if numeric:
        expression["value"] = {"node": "field", "field": value_field}
    if method == "ratio":
        expression = {"operator": "ratio", "numerator": {"operator": "count", "where": where}, "denominator": {"operator": "count", "where": {"operator": "all_records"}}, "scaleFactor": "100" if spec.get("unit") == "percent" else "1", "subsetRequired": True}
    scale = spec.get("scale", 0 if method in ("count", "count_distinct") else 2)
    require(type(scale) is int and 0 <= scale <= 8, "INVALID_METRIC", "小数位数须为 0 至 8")
    return {"schemaVersion": "metric-definition/1.0", "metricId": metric_id, "name": spec["name"].strip(),
        "domainProfile": {"id": domain, "version": "1.0.0"}, "unit": spec.get("unit"), "entityType": entity,
        "countBasis": {"kind": "distinct_entity" if distinct else "records", "keys": [key_field] if distinct else []},
        "population": {"operator": "all_records"}, "timeWindow": {"anchorField": date_field, "startParameter": "date_start", "endParameter": "date_end", "boundary": "[)", "timezone": "Asia/Shanghai", "dateAttribution": "event_date"},
        "requiredFields": list(fields.values()), "filters": [], "exclusions": [], "expression": expression, "groupBy": [],
        "missingPolicy": {"action": "skip_metric", "maxExcludedFraction": "0"}, "invalidPolicy": {"action": "skip_metric", "maxExcludedFraction": "0"},
        "emptySetPolicy": "zero_for_count" if method in ("count", "count_distinct") else "skip_metric", "zeroDenominatorPolicy": "skip_metric" if method == "ratio" else "not_applicable",
        "precision": {"scale": scale, "roundingMode": "half_up"}, "allowedQueryParameters": [{"name": key, "kind": key, "required": True, "allowedValues": []} for key in ("date_start", "date_end")],
        "sourceEvidence": [evidence], "semanticStatus": "CLEAR", "operatorVersion": "operator-catalog/1.0"}


def save(store, spec, definition_id=None, revision=None, metric_id=None, name=None, *, db=None):
    # All object writes, including initial creation, roll back if compilation fails.
    with store.transaction() if db is None else nullcontext(db) as db:
        require(isinstance(spec, dict), "INVALID_METRIC", "手工指标参数无效")
        draft = store.get("definition", definition_id, db) if definition_id else create(store, name or spec.get("name", ""), db)
        require_not_deleted(draft)
        require(not draft.get("sessionId"), "SCOPE_FORBIDDEN", "对话内指标说明请在所属对话维护")
        require(draft["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可修改，请先创建新版本")
        require(not draft.get("reportPlan"), "INVALID_METRIC", "回填文档的待填位置请通过原文取数计划维护；手工计算指标请另建指标集")
        require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('QUEUED','RUNNING')", (draft["id"],)).fetchone(), "DEFINITION_BUSY", "解析期间不能手工修改，请先完成或取消任务")
        require(not definition_id or draft["revision"] == revision, "REVISION_CONFLICT", "指标草稿已变化，请重新打开")
        if metric_id:
            require(metric_id in draft.get("manualMetrics", {}), "INVALID_METRIC", "仅手工指标可通过此表单编辑")
        else:
            metric_id = uid("metric_")
        require(len(draft["candidates"]) < LIMITS["candidates"] or metric_id in {c["id"] for c in draft["candidates"]}, "FILE_LIMIT", "指标数量超限")
        locator = "amendment:" + uid()
        quote = dumps({"kind": "MANUAL_METRIC", "metricId": metric_id, "form": spec})
        evidence = {"evidenceId": uid("ev_"), "documentSha256": sha(quote.encode()), "locator": locator, "quote": quote, "quoteSha256": sha(quote.encode())}
        rule = compile_rule(spec, metric_id, evidence)
        domains = {c["rule"]["domainProfile"]["id"] for c in draft["candidates"] if c["status"] == "CLEAR" and c["id"] != metric_id}
        require(not domains or domains == {spec["domain"]}, "DOMAIN_MISMATCH", "同一指标集仅支持一个领域，请另建指标集")
        amendment = {"sha256": store.blob(quote.encode()), "locator": locator, "text": quote, "actor": spec["actor"].strip(), "scope": rule["name"], "replaces": [], "created": now(), "kind": "USER_AMENDMENT"}
        draft["amendments"].append(amendment)
        candidate = {"id": metric_id, "name": rule["name"], "status": "CLEAR", "rule": rule, "locators": [locator]}
        candidates = [candidate if c["id"] == metric_id else c for c in draft["candidates"]]
        if not any(c["id"] == metric_id for c in candidates):
            candidates.append(candidate)
        coverage = copy.deepcopy(draft["coverage"])
        for item in coverage:
            if metric_id in item.get("candidateIds", []):
                item["candidateIds"].remove(metric_id)
                if not item["candidateIds"]:
                    item.update(disposition="CONTEXT", reason="已被新的手工口径取代，保留审计依据")
        coverage.append({"locator": locator, "disposition": "CANDIDATE", "candidateIds": [metric_id], "reason": "维护人通过受控表单明确口径"})
        draft.setdefault("manualMetrics", {})[metric_id] = copy.deepcopy(spec)
        # Validate before persisting the amended evidence as part of this transaction.
        from .engine import validate_rule
        validate_rule(rule, evidence_documents(draft))
        draft = store.put("definition", draft, db, expected=draft["revision"])
        result = save_candidates(store, draft["id"], candidates, coverage, draft["revision"], draft["reviewPolicy"],
                                 provenance={"kind": "MANUAL_FORM", "runtime": runtime_manifest()}, db=db)
        store.event(db, draft["id"], "definition.manual_saved", {"metricId": metric_id, "actor": spec["actor"]})
        return result
