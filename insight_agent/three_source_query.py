"""Compile closed knowledge-base plans to scoped, parameterized read-only SQL."""
from __future__ import annotations

import json
import re
from datetime import date

from .core import AppError, digest, require
from .three_source_kb import active_release

PLAN_KEYS = {"entity", "aggregate", "groupBy", "filters", "orderBy", "limit"}
FILTER_KEYS = {"field", "op", "value"}
ENTITY_TERMS = {"first_surgery": ("首台",), "cancellation": ("退单",), "surgery_detail": ("明细", "手术明细")}
UNIT_TERMS = {"OR_1": ("一号手术室", "第一手术室", "一手"),
              "OR_2": ("二号手术室", "第二手术室", "二手"),
              "OR_3": ("三号手术室", "第三手术室", "三手")}


def validate_question_plan(question, plan):
    require(isinstance(question, str) and isinstance(plan, dict), "PLAN_SHAPE", "问题或计划无效")
    mentioned_entities = {entity for entity, terms in ENTITY_TERMS.items()
                          if any(term in question for term in terms)}
    require(len(mentioned_entities) <= 1, "ENTITY_AMBIGUOUS", "问题同时涉及多类数据，请明确查询对象")
    require(not mentioned_entities or plan.get("entity") in mentioned_entities,
            "PLAN_ENTITY_MISMATCH", "指标或查询计划的数据类别与问题不一致")
    if any(term in question for term in ("台次", "台数", "例数", "人数", "已实施", "实际手术",
                                         "去重", "跨表", "连接三表", "住院号连接")):
        raise AppError("SEMANTIC_GAP", "该问题缺少已批准的台次、去重、实施状态或跨表口径")
    if "退单日期" in question:
        raise AppError("DATE_AMBIGUOUS", "请明确来源日期或退单发生时间")
    if any(term in question for term in ("本周", "上周", "本月", "上月", "最近", "今天", "昨日")):
        raise AppError("AMBIGUOUS_TIME_WINDOW", "请明确输入起止日期，避免相对时间窗口被忽略")
    mentioned_units = [code for code, aliases in UNIT_TERMS.items() if any(alias in question for alias in aliases)]
    require(len(mentioned_units) <= 1, "UNIT_AMBIGUOUS", "问题同时涉及多个手术室，请分别查询")
    if mentioned_units:
        expected_field = "origin_operating_unit" if plan.get("entity") == "cancellation" else "operating_unit"
        matching = [item for item in plan.get("filters", []) if item.get("field") == expected_field]
        if plan.get("entity") == "cancellation":
            require(any(item.get("op") == "eq" and item.get("value") == mentioned_units[0] for item in matching),
                    "PLAN_CONDITION_MISSING", "计划未包含问题指定的手术室")
        else:
            require(matching, "PLAN_CONDITION_MISSING", "计划未包含问题指定的手术室")
    if any(term in question for term in ("按日期", "每天", "逐日")):
        date_field = "record_date" if plan.get("entity") == "cancellation" else "operation_date"
        require(date_field in plan.get("groupBy", []), "PLAN_CONDITION_MISSING", "计划未包含按日期分组")


def compile_plan(db, plan, release_id=None):
    require(isinstance(plan, dict) and set(plan) <= PLAN_KEYS and plan.get("aggregate") == "COUNT_SOURCE_ROWS",
            "PLAN_UNSUPPORTED", "仅支持来源记录行数的闭合查询计划")
    release = active_release(db) if release_id is None else db.execute(
        "SELECT * FROM kb_release WHERE id=? AND state='PUBLISHED'", (release_id,)).fetchone()
    require(release, "KB_VERSION", "查询计划知识库版本不可用")
    release_id = release["id"]
    entity = db.execute("SELECT * FROM kb_entity WHERE release_id=? AND entity_id=?",
                        (release_id, plan.get("entity"))).fetchone()
    require(entity, "PLAN_ENTITY", "查询实体不在知识库中")
    capability = db.execute("SELECT allowed,label,unit FROM kb_query_capability WHERE release_id=? AND capability_id=?",
                            (release_id, plan["aggregate"])).fetchone()
    require(capability and capability["allowed"], "PLAN_CAPABILITY", "查询能力未获批准")
    fields = {r["field_id"]: dict(r) for r in db.execute("SELECT * FROM kb_field WHERE release_id=? AND entity_id=?", (release_id, entity["entity_id"]))}
    groups = plan.get("groupBy", [])
    filters = plan.get("filters", [])
    order = plan.get("orderBy", [])
    limit = plan.get("limit", 1000)
    require(isinstance(groups, list) and len(groups) <= 3 and len(set(groups)) == len(groups)
            and isinstance(filters, list) and len(filters) <= 10 and isinstance(order, list)
            and type(limit) is int and 1 <= limit <= 1000, "PLAN_SHAPE", "查询分组、筛选或输出上限无效")
    joins = []
    def expression(field_id):
        field = fields.get(field_id)
        require(field, "PLAN_FIELD", "字段不在知识库中")
        require(re.fullmatch(r"[a-z][a-z0-9_]*", field["physical_column"]), "KB_INVALID", "知识库字段标识无效")
        if field["physical_table"] == "data_source":
            return "src." + field["physical_column"]
        if field["physical_table"] == "cancellation_tag":
            require(entity["entity_id"] == "cancellation", "PLAN_FIELD", "退单类别只属于退单来源")
            if "JOIN cancellation_tag tag ON tag.cancellation_row_id=r.id" not in joins:
                joins.append("JOIN cancellation_tag tag ON tag.cancellation_row_id=r.id")
            return "tag." + field["physical_column"]
        require(field["physical_table"] is None, "PLAN_FIELD", "字段访问路径未批准")
        return "r." + field["physical_column"]
    selections = []
    for field_id in groups:
        field = fields.get(field_id)
        require(field and field["groupable"] and field["sensitivity"] != "PERSONAL", "PLAN_GROUP", "字段不允许分组")
        selections.append(f'{expression(field_id)} AS "{field_id}"')
    params = {}
    conditions = []
    def valid_value(field, value):
        require(isinstance(value, (str, int)) and len(str(value)) <= 120, "PLAN_FILTER", "筛选值无效")
        if field["value_type"] == "DATE":
            try:
                require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value,
                        "PLAN_FILTER", "日期必须是 YYYY-MM-DD")
            except ValueError as exc:
                raise AppError("PLAN_FILTER", "日期必须是 YYYY-MM-DD") from exc
        if field["field_id"] == "origin_operating_unit":
            allowed_values = {entry["code"] for entry in json.loads(field["value_dictionary_json"] or "[]")}
            require(value in allowed_values, "PLAN_FILTER", "手术室不在批准的取值表中")
    for index, item in enumerate(filters):
        require(isinstance(item, dict) and set(item) == FILTER_KEYS and isinstance(item["field"], str),
                "PLAN_FILTER", "筛选条件结构无效")
        field = fields.get(item["field"])
        require(field and field["sensitivity"] != "PERSONAL", "PLAN_FILTER", "字段不允许筛选")
        allowed = json.loads(field["filter_ops_json"])
        op = item["op"]
        require(op in allowed and op in ("eq", "range", "in"), "PLAN_FILTER", "筛选算子未获批准")
        expr = expression(item["field"])
        value = item["value"]
        if op == "eq":
            valid_value(field, value)
            params[f"p{index}"] = value
            conditions.append(f"{expr} = :p{index}")
        elif op == "range":
            require(isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) and len(v) <= 30 for v in value),
                    "PLAN_FILTER", "日期范围无效")
            for boundary in value:
                valid_value(field, boundary)
            require(value[0] < value[1], "PLAN_FILTER", "日期范围起点必须早于终点")
            params[f"p{index}a"], params[f"p{index}b"] = value
            conditions.append(f"{expr} >= :p{index}a AND {expr} < :p{index}b")
        else:
            require(isinstance(value, list) and 1 <= len(value) <= 20 and all(isinstance(v, (str, int)) and len(str(v)) <= 120 for v in value),
                    "PLAN_FILTER", "筛选列表无效")
            names = []
            for position, member in enumerate(value):
                valid_value(field, member)
                key = f"p{index}_{position}"
                params[key] = member
                names.append(":" + key)
            conditions.append(f"{expr} IN ({','.join(names)})")
    selections.append(('COUNT(DISTINCT r.id)' if joins else 'COUNT(*)') + ' AS "source_row_count"')
    for item in order:
        require(isinstance(item, dict) and set(item) == {"field", "direction"}
                and item["field"] in groups and item["direction"] in ("asc", "desc"),
                "PLAN_ORDER", "排序只能使用分组字段")
    table = entity["physical_table"]
    require(table in ("first_surgery_row", "cancellation_row", "surgery_detail_row"), "KB_INVALID", "来源表不在白名单")
    sql = (f"SELECT {', '.join(selections)} FROM {table} r "
           "JOIN data_sheet sh ON sh.id=r.sheet_id "
           "JOIN data_source src ON src.id=sh.source_id "
           "JOIN data_revision_source drs ON drs.source_id=src.id "
           "JOIN data_revision rev ON rev.id=drs.revision_id "
           + " ".join(joins) + " WHERE rev.session_id=:session_id AND rev.id=:revision_id "
           "AND drs.session_id=:session_id AND src.session_id=:session_id "
           "AND src.source_type=:source_type")
    if conditions:
        sql += " AND " + " AND ".join(f"({condition})" for condition in conditions)
    if groups:
        sql += " GROUP BY " + ", ".join(expression(field_id) for field_id in groups)
    if order:
        sql += " ORDER BY " + ", ".join(f'{expression(item["field"])} {item["direction"].upper()}' for item in order)
    sql += " LIMIT :limit"
    params["source_type"] = entity["source_type"]
    params["limit"] = limit
    return {"sql": sql, "parameters": params, "sqlSha256": digest(sql),
            "releaseId": release_id, "releaseHash": release["content_sha256"],
            "label": capability["label"], "unit": capability["unit"]}


def execute_plan(store, session_id, plan, revision_id=None, release_id=None, release_hash=None):
    store.get("session", session_id)
    with store.connect() as db:
        revision = db.execute("SELECT * FROM data_revision WHERE session_id=? ORDER BY revision_no DESC LIMIT 1", (session_id,)).fetchone() if revision_id is None else db.execute("SELECT * FROM data_revision WHERE id=? AND session_id=?", (revision_id, session_id)).fetchone()
        require(revision, "DATA_REQUIRED", "当前对话尚无已发布数据版本")
        compiled = compile_plan(db, plan, release_id)
        require(release_hash is None or compiled["releaseHash"] == release_hash,
                "KB_VERSION", "冻结知识库哈希与本地发布包不一致")
        parameters = compiled["parameters"] | {"session_id": session_id, "revision_id": revision["id"]}
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        rows = [dict(row) for row in db.execute(compiled["sql"], parameters).fetchall()]
        return {"rows": rows, "plan": plan, "sql": compiled["sql"],
                "sqlSha256": compiled["sqlSha256"], "revisionId": revision["id"],
                "manifestSha256": revision["manifest_sha256"], "releaseId": compiled["releaseId"],
                "releaseHash": compiled["releaseHash"], "label": compiled["label"], "unit": compiled["unit"]}


def basic_plan(question):
    """A narrow local fallback for explicit source-row questions without a model."""
    require(isinstance(question, str) and 1 <= len(question) <= 4000, "INVALID_QUESTION", "查询问题无效")
    if any(term in question for term in ("台次", "已实施", "实际手术", "去重", "住院号连接", "合并三表")):
        raise AppError("SEMANTIC_GAP", "该口径缺少已确认的去重键、实施状态或跨表关系")
    if "退单日期" in question:
        raise AppError("DATE_AMBIGUOUS", "退单日期可指来源日期或退单发生时间，请明确指定")
    remainder = re.sub(r"[\s，。？?、]", "", question)
    for phrase in ("按日期统计", "每天统计", "逐日统计", "当前对话的", "当前对话", "一号手术室",
                   "二号手术室", "三号手术室", "第一手术室", "第二手术室", "第三手术室",
                   "手术明细", "来源记录行数", "来源行数", "记录行数", "行数", "首台", "退单",
                   "明细", "一手", "二手", "三手", "统计"):
        remainder = remainder.replace(phrase, "")
    require(not remainder, "PLAN_NEEDS_MODEL", "问题包含离线解析未覆盖的条件，请由知识库生成闭合计划")
    entities = [name for name, terms in ENTITY_TERMS.items() if any(term in question for term in terms)]
    require(len(entities) == 1 and ("行" in question or "记录" in question),
            "PLAN_NEEDS_MODEL", "请明确三类来源之一及来源记录行数，或配置模型生成查询计划")
    entity = entities[0]
    group = []
    if "按日期" in question or "每天" in question or "逐日" in question:
        group = ["record_date" if entity == "cancellation" else "operation_date"]
    filters = []
    if entity == "cancellation":
        units = [("OR_1", ("一号手术室", "第一手术室", "一手")),
                 ("OR_2", ("二号手术室", "第二手术室", "二手")),
                 ("OR_3", ("三号手术室", "第三手术室", "三手"))]
        hits = [code for code, aliases in units if any(alias in question for alias in aliases)]
        require(len(hits) <= 1, "UNIT_AMBIGUOUS", "问题同时涉及多个手术室，请分别查询")
        if hits:
            filters.append({"field": "origin_operating_unit", "op": "eq", "value": hits[0]})
    plan = {"entity": entity, "aggregate": "COUNT_SOURCE_ROWS", "groupBy": group,
            "filters": filters, "orderBy": [{"field": group[0], "direction": "asc"}] if group else [], "limit": 1000}
    validate_question_plan(question, plan)
    return plan
