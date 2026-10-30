"""Closed typed rule compiler and exact rational arithmetic; never eval/exec."""
from __future__ import annotations

import json
import operator
import re
from collections import defaultdict
from datetime import date, datetime, time, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator

from .core import AppError, ENGINE_VERSION, LIMITS, ROOT, digest, require, sha
from .domains import FIELDS

SCHEMA = json.loads((ROOT / "resources" / "metric-definition.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
NUMERIC = {"integer", "decimal"}
DOMAIN_ENTITIES = {"surgery": {"surgery_event"}, "hospital_ops": {"department_day", "discharge_event", "revenue_event"}}


def scalar_type(node, fields, params):
    kind = node["node"]
    if kind == "field":
        require(node["field"] in fields, "INVALID_REFERENCE", "规则引用了未声明字段")
        field = fields[node["field"]]
        return field["valueType"], field["unit"]
    if kind == "literal":
        return node["valueType"], node.get("unit", "none")
    if kind == "parameter":
        require(node["parameter"] in params, "INVALID_REFERENCE", "规则引用了未声明参数")
        param = params[node["parameter"]]
        require(param["kind"] in ("date_start", "date_end"), "PARAMETER_NOT_ALLOWED", "不支持将列表参数作为标量")
        return "date", "date"
    if kind == "duration":
        for name in (node["startField"], node["endField"]):
            require(name in fields and fields[name]["valueType"] == "datetime", "OPERATOR_TYPE_MISMATCH", "时长算子必须引用完整日期时间字段")
        return "decimal", "minute"
    raise AppError("UNSUPPORTED_OPERATOR", "未知标量算子")


def compatible(left, right):
    return (left[0] == right[0] or left[0] in NUMERIC and right[0] in NUMERIC) and left[1] == right[1]


def validate_rule(rule, evidence_documents=None):
    errors = list(VALIDATOR.iter_errors(rule))
    require(not errors, "INVALID_RULE_SCHEMA", "指标结构不符合受控规则规范", {"paths": ["/".join(map(str, e.absolute_path)) for e in errors[:10]]})
    fields = {x["id"]: x for x in rule["requiredFields"]}
    params = {x["name"]: x for x in rule["allowedQueryParameters"]}
    require(len(fields) == len(rule["requiredFields"]) and len(params) == len(rule["allowedQueryParameters"]), "INVALID_REFERENCE", "字段或参数 ID 重复")
    require(len({e["evidenceId"] for e in rule["sourceEvidence"]}) == len(rule["sourceEvidence"]), "INVALID_REFERENCE", "证据 ID 重复")
    domain = rule["domainProfile"]
    require(domain["version"] == "1.0.0" and rule["entityType"] in DOMAIN_ENTITIES.get(domain["id"], set()), "VERSION_MISMATCH", "领域包或统计实体不受支持")
    for field in fields.values():
        standard = FIELDS[domain["id"]].get(field["id"])
        if standard:
            require((field["valueType"], field["unit"]) == standard[:2], "OPERATOR_TYPE_MISMATCH", "字段类型或单位与领域字典不一致")
            require(rule["entityType"] in standard[2], "GRAIN_MISMATCH", "标准字段不属于当前统计实体")
    tw = rule["timeWindow"]
    require(tw["anchorField"] in fields and fields[tw["anchorField"]]["valueType"] in ("date", "datetime"), "INVALID_REFERENCE", "时间轴字段无效")
    for key, kind in (("startParameter", "date_start"), ("endParameter", "date_end")):
        require(tw[key] in params and params[tw[key]]["kind"] == kind and params[tw[key]]["required"] and not params[tw[key]]["allowedValues"], "INVALID_WINDOW", "日期参数声明无效")
        require(sum(p["kind"] == kind for p in params.values()) == 1, "INVALID_WINDOW", "日期参数必须唯一")
    try:
        ZoneInfo(tw["timezone"])
    except ZoneInfoNotFoundError:
        raise AppError("INVALID_WINDOW", "不支持该时区") from None
    for name in rule["groupBy"] + rule["countBasis"]["keys"]:
        require(name in fields, "INVALID_REFERENCE", "分组或实体键未声明")
    for name in rule["groupBy"]:
        require(fields[name]["role"] == "dimension", "GRAIN_MISMATCH", "分组字段必须为维度")
    for p in params.values():
        if p["kind"] == "group_by":
            require(all(x in fields and fields[x]["role"] == "dimension" for x in p["allowedValues"]), "PARAMETER_NOT_ALLOWED", "分组白名单无效")
        require(p["kind"] != "dimension_filter", "PARAMETER_NOT_ALLOWED", "当前领域版本未注册动态维度筛选参数，请在规则 filters 中明确")
    for key in ("missingPolicy", "invalidPolicy"):
        policy = rule[key]
        threshold = Fraction(policy["maxExcludedFraction"])
        require(0 <= threshold <= 1 and (policy["action"] != "skip_metric" or threshold == 0), "DEFINITION_INCOMPLETE", "缺失和非法值政策阈值不合法")
    visited, used = 0, set(rule["groupBy"] + rule["countBasis"]["keys"] + [tw["anchorField"]])

    def walk(node, depth=0):
        nonlocal visited
        if isinstance(node, dict):
            visited += 1
            require(depth <= 12 and visited <= 256, "PLAN_LIMIT_EXCEEDED", "规则表达式过于复杂")
            if "node" in node:
                scalar_type(node, fields, params)
            if node.get("node") == "field":
                used.add(node["field"])
            if node.get("node") == "duration":
                used.update((node["startField"], node["endField"]))
            op = node.get("operator")
            if op in ("eq", "ne", "gt", "gte", "lt", "lte"):
                require(compatible(scalar_type(node["left"], fields, params), scalar_type(node["right"], fields, params)), "OPERATOR_TYPE_MISMATCH", "比较两侧类型或单位不同")
            if op in ("in", "not_in"):
                require(all(compatible(scalar_type(node["value"], fields, params), scalar_type(v, fields, params)) for v in node["values"]), "OPERATOR_TYPE_MISMATCH", "集合元素类型不一致")
            if op in ("sum", "mean", "min", "max"):
                require(scalar_type(node["value"], fields, params)[0] in NUMERIC, "OPERATOR_TYPE_MISMATCH", "数值聚合不能用于文本或日期")
            if op == "count":
                require(rule["countBasis"]["kind"] == "records", "GRAIN_MISMATCH", "记录计数与实体计数声明不符")
            if op == "count_distinct":
                require(node["fields"] == rule["countBasis"]["keys"] and rule["countBasis"]["kind"] == "distinct_entity", "GRAIN_MISMATCH", "去重计数键与实体键不符")
                used.update(node["fields"])
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth)

    for part in (rule["population"], rule["filters"], rule["exclusions"], rule["expression"]):
        walk(part)
    require(set(fields) == used, "INVALID_REFERENCE", "规则存在未使用的必需字段")
    expression = rule["expression"]
    ratio = expression["operator"] == "ratio"
    require(rule["zeroDenominatorPolicy"] == ("skip_metric" if ratio else "not_applicable"), "INVALID_EMPTY_POLICY", "零分母政策不匹配")
    require(rule["emptySetPolicy"] != "zero_for_count" or expression["operator"] in ("count", "count_distinct"), "INVALID_EMPTY_POLICY", "仅计数允许空集为零")
    if ratio:
        require(rule["unit"] in ("percent", "ratio") and expression["scaleFactor"] == ("100" if rule["unit"] == "percent" else "1"), "INVALID_RATIO_CONTRACT", "比率单位与缩放不符")
        def aggregate_unit(agg):
            return "count" if agg["operator"] in ("count", "count_distinct") else scalar_type(agg["value"], fields, params)[1]
        require(aggregate_unit(expression["numerator"]) == aggregate_unit(expression["denominator"]), "INVALID_RATIO_CONTRACT", "分子分母单位不兼容")
        require(not expression["subsetRequired"] or all(expression[k]["operator"] in ("count", "count_distinct") for k in ("numerator", "denominator")), "INVALID_RATIO_CONTRACT", "成员子集校验仅适用于计数比率")
    elif expression["operator"] in ("count", "count_distinct"):
        require(rule["unit"] in ("case", "person", "item"), "OPERATOR_TYPE_MISMATCH", "计数结果必须使用计数单位")
    else:
        require(rule["unit"] == scalar_type(expression["value"], fields, params)[1], "OPERATOR_TYPE_MISMATCH", "聚合结果单位必须与输入度量一致")
    if evidence_documents is not None:
        for evidence in rule["sourceEvidence"]:
            doc = evidence_documents.get(evidence["documentSha256"])
            require(doc is not None and evidence["documentSha256"] != "0" * 64, "EVIDENCE_INVALID", "原文证据不在版本清单中")
            quote = doc.get(evidence["locator"])
            require(quote == evidence["quote"] and sha(quote.encode()) == evidence["quoteSha256"], "EVIDENCE_INVALID", "证据定位或原文哈希不匹配")
    return {"ruleDigest": digest(rule), "engineVersion": ENGINE_VERSION, "checks": ["V01", "V02" if evidence_documents is not None else "V02_DEFERRED", "V03", "V04", "V05", "V06", "V07", "V08", "V09", "V10"]}


def typed(value, type, tz):
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        raise ValueError("invalid cell")
    if type == "string":
        if not isinstance(value, str):
            raise ValueError("not string")
        return value
    if type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("not boolean")
        return value
    if type in NUMERIC:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?", str(value)) or len(str(value)) > 80:
            raise ValueError("invalid decimal")
        result = Fraction(str(value))
        if type == "integer" and result.denominator != 1:
            raise ValueError("not integer")
        return result
    if type == "date":
        return date.fromisoformat(value)
    if type == "datetime":
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    raise ValueError("unknown type")


def scalar(node, row, parameters, tz):
    kind = node["node"]
    if kind == "field":
        return row[node["field"]]
    if kind == "parameter":
        return date.fromisoformat(parameters[node["parameter"]])
    if kind == "literal":
        return typed(node["value"], node["valueType"], tz)
    start, end = row[node["startField"]], row[node["endField"]]
    if start is None or end is None:
        return None
    delta = end - start
    if delta.days < 0:
        raise ValueError("negative duration")
    return Fraction((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds, 60000000)


def predicate(node, row, parameters, tz):
    op = node["operator"]
    if op == "all_records":
        return True
    if op in ("and", "or"):
        values = [predicate(a, row, parameters, tz) for a in node["args"]]
        if op == "and":
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if op == "not":
        value = predicate(node["arg"], row, parameters, tz)
        return None if value is None else not value
    if op in ("is_null", "is_not_null"):
        result = scalar(node["value"], row, parameters, tz) is None
        return result if op == "is_null" else not result
    if op in ("in", "not_in"):
        value = scalar(node["value"], row, parameters, tz)
        values = [scalar(v, row, parameters, tz) for v in node["values"]]
        if value is None:
            return None
        result = True if value in values else None if None in values else False
        return result if op == "in" or result is None else not result
    left, right = scalar(node["left"], row, parameters, tz), scalar(node["right"], row, parameters, tz)
    if left is None or right is None:
        return None
    return {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}[op](left, right)


def display(value: Fraction, precision):
    scale = precision["scale"]
    scaled = abs(value) * 10**scale
    integer, remainder = divmod(scaled.numerator, scaled.denominator)
    mode = precision["roundingMode"]
    if mode == "half_up" and remainder * 2 >= scaled.denominator or mode == "half_even" and (remainder * 2 > scaled.denominator or remainder * 2 == scaled.denominator and integer % 2):
        integer += 1
    digits = str(integer).zfill(scale + 1)
    result = digits[:-scale] + "." + digits[-scale:] if scale else digits
    return ("-" if value < 0 and integer else "") + result


def exact(value):
    return {"exactNumerator": str(value.numerator), "exactDenominator": str(value.denominator)}


def validate_parameters(rule, parameters):
    declarations = {x["name"]: x for x in rule["allowedQueryParameters"]}
    # ``scope`` is an execution contract owned by the application rather than
    # a metric-specific query parameter. It is deliberately accepted for every
    # published rule so ALL_DATA can reuse the same immutable definitions.
    require(set(parameters) - {"scope"} <= set(declarations), "PARAMETER_NOT_ALLOWED", "查询包含未批准的参数")
    scope = parameters.get("scope")
    require(scope in (None, "ALL_DATA"), "INVALID_WINDOW", "统计范围仅支持当前会话全部数据")
    full_scope = scope == "ALL_DATA"
    # Rules retain their date declarations for auditability and backwards
    # compatibility. ALL_DATA is the UI-facing execution mode and intentionally
    # does not require callers to provide a date window.
    if not full_scope:
        require(all(not p["required"] or p["name"] in parameters for p in declarations.values()), "INVALID_WINDOW", "请明确统计开始日期与结束日期")
    tw = rule["timeWindow"]
    if full_scope:
        start = end = None
    else:
        try:
            start, end = date.fromisoformat(parameters[tw["startParameter"]]), date.fromisoformat(parameters[tw["endParameter"]])
        except (ValueError, TypeError, KeyError):
            raise AppError("INVALID_WINDOW", "日期格式应为 YYYY-MM-DD") from None
        require(start < end, "INVALID_WINDOW", "结束日期必须晚于开始日期")
    group = parameters.get("group_by", rule["groupBy"])
    require(isinstance(group, list) and len(group) <= 4 and len(set(group)) == len(group), "PARAMETER_NOT_ALLOWED", "分组参数无效")
    if "group_by" in parameters:
        require("group_by" in declarations and set(group) <= set(declarations["group_by"]["allowedValues"]), "PARAMETER_NOT_ALLOWED", "分组不在批准范围中")
    return start, end, group


def calculate(rule, source_rows, parameters):
    from .core import checkpoint
    """Return either auditable exact results or a nonnumeric skip record."""
    validate_rule(rule)
    start, end, group_by = validate_parameters(rule, parameters)
    full_scope = parameters.get("scope") == "ALL_DATA"
    tz = ZoneInfo(rule["timeWindow"]["timezone"])
    audit = {"inputRows": len(source_rows), "inWindowRows": 0, "outsideWindowRows": 0, "missingRows": 0, "invalidRows": 0, "excludedRows": 0, "includedRows": 0, "analysisScope": "ALL_DATA" if full_scope else "DATE_WINDOW"}
    base = {"metricId": rule["metricId"], "name": rule["name"], "unit": rule["unit"], "ruleDigest": digest(rule), "parameters": parameters, "audit": audit}

    def skip(code):
        return base | {"outcome": "SKIP", "reasonCode": code}

    rows, seen = [], set()
    keys = rule["countBasis"]["keys"]
    for index, source in enumerate(source_rows):
        if index % 256 == 0:
            checkpoint()
        try:
            row = {f["id"]: typed(source.get(f["id"]), f["valueType"], tz) for f in rule["requiredFields"]}
            anchor = row[rule["timeWindow"]["anchorField"]]
            if anchor is None:
                audit["missingRows"] += 1
                continue
            if not full_scope:
                lower, upper = start, end
                if isinstance(anchor, datetime):
                    lower = datetime.combine(start, time(), tz).astimezone(timezone.utc)
                    upper = datetime.combine(end, time(), tz).astimezone(timezone.utc)
                bounds = rule["timeWindow"]["boundary"]
                if not ((anchor >= lower if bounds[0] == "[" else anchor > lower) and (anchor <= upper if bounds[1] == "]" else anchor < upper)):
                    audit["outsideWindowRows"] += 1
                    continue
            audit["inWindowRows"] += 1
            if keys:
                key = tuple(row[k] for k in keys)
                if None in key:
                    return skip("DATA_INSUFFICIENT")
                if key in seen:
                    return skip("DATA_CONFLICT")
                seen.add(key)
            if any(v is None for v in row.values()):
                audit["missingRows"] += 1
                continue
            if predicate(rule["population"], row, parameters, tz) is not True or any(predicate(p, row, parameters, tz) is not True for p in rule["filters"]) or any(predicate(p, row, parameters, tz) is True for p in rule["exclusions"]):
                audit["excludedRows"] += 1
                continue
            # Evaluate every expression now; row-level errors follow explicit policy.
            expression = rule["expression"]
            aggs = [expression[k] for k in ("numerator", "denominator")] if expression["operator"] == "ratio" else [expression]
            selections = []
            for agg in aggs:
                selected = predicate(agg["where"], row, parameters, tz)
                if selected is None:
                    raise ValueError("unknown predicate")
                value = scalar(agg["value"], row, parameters, tz) if "value" in agg and selected else None
                selections.append((selected, value))
            rows.append((row, source.get("_source", {"row": index + 1}), selections))
        except (ValueError, TypeError, OverflowError):
            audit["invalidRows"] += 1
    for counter, policy_name in (("missingRows", "missingPolicy"), ("invalidRows", "invalidPolicy")):
        if audit[counter]:
            policy = rule[policy_name]
            if policy["action"] == "skip_metric" or Fraction(audit[counter], max(len(source_rows) - audit["outsideWindowRows"], 1)) > Fraction(policy["maxExcludedFraction"]):
                return skip("DATA_INSUFFICIENT")
    audit["includedRows"] = len(rows)
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[0][g] for g in group_by)].append(row)
    if not rows:
        if rule["emptySetPolicy"] == "zero_for_count" and not group_by:
            groups[()] = []
        else:
            return skip("EMPTY_SET")
    require(len(groups) <= LIMITS["groups"], "PLAN_LIMIT_EXCEEDED", "分组数量超限")
    results = []

    def aggregate(agg, members, slot):
        checkpoint()
        selected = [r for r in members if r[2][slot][0]]
        op = agg["operator"]
        if op in ("count", "count_distinct"):
            return Fraction(len(selected)), selected
        require(selected, "EMPTY_SET", "聚合没有符合条件的数据")
        values = [r[2][slot][1] for r in selected]
        return {"sum": lambda: sum(values, Fraction()), "mean": lambda: sum(values, Fraction()) / len(values), "min": lambda: min(values), "max": lambda: max(values)}[op](), selected

    for group, members in sorted(groups.items(), key=lambda item: tuple(str(x) for x in item[0])):
        checkpoint()
        result = {"group": dict(zip(group_by, group))}
        try:
            expr = rule["expression"]
            if expr["operator"] == "ratio":
                numerator, numerator_rows = aggregate(expr["numerator"], members, 0)
                denominator, denominator_rows = aggregate(expr["denominator"], members, 1)
                require(denominator != 0, "ZERO_DENOMINATOR", "分母为零")
                if expr["subsetRequired"]:
                    require({digest(r[1]) for r in numerator_rows} <= {digest(r[1]) for r in denominator_rows}, "DATA_CONFLICT", "分子成员不属于分母集合")
                value = numerator / denominator * Fraction(expr["scaleFactor"])
                result.update(numerator=exact(numerator), denominator=exact(denominator), scaleFactor=expr["scaleFactor"])
            else:
                value, _ = aggregate(expr, members, 0)
            result.update(outcome="VALUE", **exact(value), displayValue=display(value, rule["precision"]), sources=[r[1] for r in members])
        except AppError as exc:
            if exc.code not in ("EMPTY_SET", "ZERO_DENOMINATOR", "DATA_CONFLICT"):
                raise
            result.update(outcome="SKIP", reasonCode=exc.code)
        results.append(result)
    if all(r["outcome"] == "SKIP" for r in results):
        return skip(results[0]["reasonCode"]) | {"groups": results}
    return base | {"outcome": "VALUE", "groups": results}
