"""Manual natural-language metrics with immutable published SQL versions."""
from __future__ import annotations

import json
import math
import re

from .core import AppError, now, require, uid
from .three_source_kb import active_release
from .three_source_query import compile_plan, execute_plan, validate_question_plan

PLAN_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["plan", "sqlDraft"],
               "properties": {"plan": {"type": "object"}, "sqlDraft": {"type": "string", "maxLength": 12000}}}
MATCH_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["decision", "metricId", "reason"],
                "properties": {"decision": {"enum": ["MATCH", "NO_MATCH", "AMBIGUOUS"]},
                               "metricId": {"type": ["string", "null"]}, "reason": {"type": "string"}}}


def _current(db, metric_id):
    metric = db.execute("SELECT * FROM manual_metric WHERE id=? AND deleted_at IS NULL", (metric_id,)).fetchone()
    require(metric, "METRIC_NOT_FOUND", "指标不存在或已删除")
    version = db.execute("SELECT * FROM manual_metric_version WHERE metric_id=? ORDER BY version_no DESC LIMIT 1", (metric_id,)).fetchone()
    return metric, version


def _public(metric, version):
    value = dict(version)
    for key in ("aliases_json", "plan_json", "parameter_schema_json", "validation_json"):
        if value.get(key):
            value[key.removesuffix("_json")] = json.loads(value[key])
        value.pop(key, None)
    return {"id": metric["id"], "activeVersionId": metric["active_version_id"],
            "deletedAt": metric["deleted_at"], "version": value}


def list_metrics(store, include_deleted=False):
    with store.connect() as db:
        rows = db.execute("SELECT * FROM manual_metric ORDER BY created_at DESC").fetchall()
        return [_public(row, db.execute("SELECT * FROM manual_metric_version WHERE metric_id=? ORDER BY version_no DESC LIMIT 1", (row["id"],)).fetchone())
                for row in rows if include_deleted or not row["deleted_at"]]


def get_metric(store, metric_id):
    with store.connect() as db:
        metric, version = _current(db, metric_id)
        versions = [_public(metric, r)["version"] for r in db.execute("SELECT * FROM manual_metric_version WHERE metric_id=? ORDER BY version_no DESC", (metric_id,))]
        return _public(metric, version) | {"versions": versions}


def create_metric(store, name, description, aliases=None):
    require(isinstance(name, str) and 1 <= len(name.strip()) <= 120 and
            isinstance(description, str) and 1 <= len(description.strip()) <= 10000,
            "METRIC_INPUT", "请填写指标名称和自然语言描述")
    aliases = aliases or []
    require(isinstance(aliases, list) and len(aliases) <= 20 and all(isinstance(a, str) and 1 <= len(a.strip()) <= 120 for a in aliases),
            "METRIC_INPUT", "指标别名无效")
    metric_id, version_id, timestamp = uid("metric_"), uid("metricv_"), now()
    with store.transaction() as db:
        db.execute("INSERT INTO manual_metric(id,created_at) VALUES (?,?)", (metric_id, timestamp))
        db.execute("""INSERT INTO manual_metric_version
            (id,metric_id,version_no,name,aliases_json,description,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'DRAFT',?,?)""",
            (version_id, metric_id, 1, name.strip(), json.dumps(aliases, ensure_ascii=False), description.strip(), timestamp, timestamp))
    return get_metric(store, metric_id)


def edit_metric(store, metric_id, name, description, aliases=None, expected_revision=None):
    require(isinstance(name, str) and 1 <= len(name.strip()) <= 120 and
            isinstance(description, str) and 1 <= len(description.strip()) <= 10000,
            "METRIC_INPUT", "请填写指标名称和自然语言描述")
    aliases = aliases or []
    require(isinstance(aliases, list) and len(aliases) <= 20 and all(isinstance(a, str) and 1 <= len(a.strip()) <= 120 for a in aliases),
            "METRIC_INPUT", "指标别名无效")
    with store.transaction() as db:
        metric, latest = _current(db, metric_id)
        require(expected_revision is None or latest["draft_revision"] == expected_revision,
                "REVISION_CONFLICT", "指标草稿已变化，请刷新")
        if latest["status"] == "PUBLISHED":
            timestamp = now()
            db.execute("""INSERT INTO manual_metric_version
                (id,metric_id,version_no,name,aliases_json,description,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'DRAFT',?,?)""",
                (uid("metricv_"), metric_id, latest["version_no"] + 1, name.strip(),
                 json.dumps(aliases, ensure_ascii=False), description.strip(), timestamp, timestamp))
        else:
            db.execute("""UPDATE manual_metric_version SET name=?,aliases_json=?,description=?,
                draft_revision=draft_revision+1,status='DRAFT',plan_json=NULL,ai_sql_draft=NULL,
                sql_template=NULL,sql_sha256=NULL,validation_json=NULL,updated_at=? WHERE id=?""",
                (name.strip(), json.dumps(aliases, ensure_ascii=False), description.strip(), now(), latest["id"]))
    return get_metric(store, metric_id)


def delete_metric(store, metric_id):
    with store.transaction() as db:
        _current(db, metric_id)
        db.execute("UPDATE manual_metric SET deleted_at=? WHERE id=?", (now(), metric_id))
        try:
            db.execute("DELETE FROM manual_metric_fts WHERE metric_id=?", (metric_id,))
        except Exception as exc:
            if "no such table" not in str(exc):
                raise
    return {"id": metric_id, "deleted": True}


def _catalog(db, question=""):
    release = active_release(db)
    entities = [dict(row) for row in db.execute("SELECT entity_id,grain,description FROM kb_entity WHERE release_id=?", (release["id"],))]
    fields = [dict(row) for row in db.execute("SELECT entity_id,field_id,description,filter_ops_json,groupable FROM kb_field WHERE release_id=? AND sensitivity!='PERSONAL'", (release["id"],))]
    capabilities = [dict(row) for row in db.execute("SELECT capability_id,label,grain,deduplication FROM kb_query_capability WHERE release_id=? AND allowed=1", (release["id"],))]
    terms = [dict(row) for row in db.execute("SELECT term,target_kind,entity_id,target_id FROM kb_term WHERE release_id=?", (release["id"],))]
    ranked = []
    mode = "IN_MEMORY_BM25"
    if question.strip() and len(question.strip()) >= 3:
        try:
            phrase = '"' + question.strip().replace('"', '""') + '"'
            ranked = [f"{row[0]}.{row[1]}" for row in db.execute("""SELECT entity_id,target_id
                FROM kb_search_fts WHERE kb_search_fts MATCH ? AND target_kind='FIELD'
                AND release_id=? ORDER BY bm25(kb_search_fts) LIMIT 12""",
                (phrase, release["id"]))]
            mode = "FTS5_BM25"
        except Exception as exc:
            if "no such table" not in str(exc) and "no such tokenizer" not in str(exc):
                raise
    if not ranked and question.strip():
        mode = "IN_MEMORY_BM25"
        query_tokens = _tokens(question)
        documents = [_tokens(field["field_id"] + " " + field["description"] + " " +
                             " ".join(term["term"] for term in terms if term["entity_id"] == field["entity_id"]
                                      and term["target_id"] == field["field_id"])) for field in fields]
        average_length = sum(len(document) for document in documents) / max(1, len(documents))
        scores = []
        for field, document in zip(fields, documents):
            score = 0.0
            for token in query_tokens & document:
                frequency = sum(token in candidate for candidate in documents)
                idf = math.log(1 + (len(documents) - frequency + .5) / (frequency + .5))
                score += idf * 2.2 / (1 + 1.2 * (.25 + .75 * len(document) / max(1, average_length)))
            scores.append((score, f"{field['entity_id']}.{field['field_id']}"))
        ranked = [name for score, name in sorted(scores, reverse=True) if score > 0][:12]
    return {"entities": entities, "fields": fields, "capabilities": capabilities,
            "terms": terms, "relations": [], "retrieval": {"mode": mode, "rankedFields": ranked,
            "coverage": "FULL_CATALOG_INCLUDED"}}


def _semantic_gap(name, description, plan):
    text = name + " " + description
    unsupported = ("台次", "台数", "例数", "人数", "唯一手术", "实际手术", "已实施", "已完成",
                   "去重", "跨表", "关联三表", "住院号关联", "比率", "比例", "平均", "总时长")
    if any(term in text for term in unsupported):
        return "SEMANTIC_GAP"
    mentioned = [entity for term, entity in (("首台", "first_surgery"), ("退单", "cancellation"),
                                            ("明细", "surgery_detail")) if term in text]
    if len(set(mentioned)) != 1 or plan.get("entity") != mentioned[0]:
        return "ENTITY_MISMATCH"
    if not any(term in text for term in ("来源行", "来源记录", "导出行", "原始行", "导出来源行")):
        return "GRAIN_UNCONFIRMED"
    return None


def generate_sql(store, metric_id, expected_revision, gateway=None, generation_id=None):
    from .models import Gateway
    with store.connect() as db:
        _, version = _current(db, metric_id)
        if generation_id and version["last_generation_id"] == generation_id and version["draft_revision"] == expected_revision + 1:
            return get_metric(store, metric_id)
        require(version["status"] != "PUBLISHED" and version["draft_revision"] == expected_revision,
                "REVISION_CONFLICT", "指标草稿已变化，请刷新")
        catalog = _catalog(db, version["name"] + " " + version["description"])
        release = active_release(db)
    gateway = gateway or Gateway(store)
    response = gateway.call("metric_sql_draft", {"name": version["name"],
        "description": version["description"], "catalog": catalog,
        "instruction": "仅提出知识库批准的来源记录行数闭合计划和供人复核的 SQL 草稿；不准推断手术台次、实施状态或跨表连接。"},
        PLAN_SCHEMA, generation_id or uid("metric_generation_"))
    require(isinstance(response["sqlDraft"], str), "MODEL_SCHEMA_INVALID", "SQL 草稿无效")
    with store.transaction() as db:
        _, current = _current(db, metric_id)
        require(current["id"] == version["id"] and current["draft_revision"] == expected_revision,
                "REVISION_CONFLICT", "生成期间指标描述已变化，请重新生成")
        validation = {"status": "BLOCKED", "reason": None}
        compiled = None
        try:
            gap = _semantic_gap(current["name"], current["description"], response["plan"])
            require(not gap, gap or "SEMANTIC_GAP", "指标口径与批准的来源行数能力不一致")
            validate_question_plan(current["name"] + " " + current["description"], response["plan"])
            compiled = compile_plan(db, response["plan"], release["id"])
            validation = {"status": "PASS", "sqlSha256": compiled["sqlSha256"]}
        except AppError as exc:
            validation["reason"] = exc.code
        db.execute("""UPDATE manual_metric_version SET status=?,draft_revision=draft_revision+1,
            last_generation_id=?,ai_sql_draft=?,plan_json=?,sql_template=?,sql_sha256=?,
            parameter_schema_json=?,validation_json=?,kb_release_id=?,kb_content_sha256=?,
            output_unit=?,result_grain=?,updated_at=? WHERE id=?""",
            ("VALIDATED" if compiled else "SQL_DRAFT", generation_id or uid("generation_"), response["sqlDraft"],
             json.dumps(response["plan"], ensure_ascii=False), compiled["sql"] if compiled else None,
             compiled["sqlSha256"] if compiled else None, json.dumps({"session_id": "server", "revision_id": "server", "filters": "plan"}),
             json.dumps(validation), release["id"], release["content_sha256"],
             compiled["unit"] if compiled else None, "原始来源行" if compiled else None, now(), current["id"]))
    return get_metric(store, metric_id)


def publish_metric(store, metric_id, expected_revision, actor, reviewed):
    require(isinstance(actor, str) and actor.strip() and reviewed is True, "REVIEW_REQUIRED", "请确认 SQL 与业务口径并填写复核人")
    with store.transaction() as db:
        metric, version = _current(db, metric_id)
        require(version["status"] == "VALIDATED" and version["draft_revision"] == expected_revision,
                "REVISION_CONFLICT", "指标未通过校验或草稿已变化")
        release = active_release(db)
        require(version["kb_release_id"] == release["id"] and version["kb_content_sha256"] == release["content_sha256"],
                "KB_VERSION", "知识库版本已变化，请重新生成 SQL")
        compiled = compile_plan(db, json.loads(version["plan_json"]))
        require(compiled["sqlSha256"] == version["sql_sha256"] and compiled["sql"] == version["sql_template"],
                "SQL_CHANGED", "SQL 模板与本地编译结果不一致")
        validation = json.loads(version["validation_json"]) | {"reviewer": actor.strip(), "reviewedAt": now()}
        db.execute("UPDATE manual_metric_version SET status='PUBLISHED',published_at=?,validation_json=?,updated_at=? WHERE id=?",
                   (now(), json.dumps(validation, ensure_ascii=False), now(), version["id"]))
        db.execute("UPDATE manual_metric SET active_version_id=? WHERE id=?", (version["id"], metric_id))
        try:
            db.execute("DELETE FROM manual_metric_fts WHERE metric_id=?", (metric_id,))
            db.execute("INSERT INTO manual_metric_fts(metric_id,version_id,route_status,name,aliases,description) VALUES (?,?,?,?,?,?)",
                       (metric_id, version["id"], "PUBLISHED", version["name"], " ".join(json.loads(version["aliases_json"] or "[]")), version["description"]))
        except Exception as exc:
            if "no such table" not in str(exc):
                raise
    return get_metric(store, metric_id)


def _tokens(text):
    text = re.sub(r"\s+", "", text.lower())
    return set(text[i:i + 2] for i in range(max(0, len(text) - 1))) | set(re.findall(r"[a-z0-9_]+", text))


def _bm25_rank(question, rows):
    query_tokens = _tokens(question)
    documents = [_tokens(row["name"] + " " + (row["aliases_json"] or "") + " " + row["description"]) for row in rows]
    average_length = sum(len(doc) for doc in documents) / max(1, len(documents))
    ranking = {}
    for row, document in zip(rows, documents):
        score = 0.0
        for token in query_tokens & document:
            frequency = 1
            document_frequency = sum(token in candidate for candidate in documents)
            idf = math.log(1 + (len(rows) - document_frequency + .5) / (document_frequency + .5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * len(document) / max(1, average_length)))
        ranking[row["id"]] = score
    return ranking


def route_metric(store, question, gateway=None, budget_id=None):
    require(isinstance(question, str) and 1 <= len(question) <= 4000, "INVALID_QUESTION", "问题无效")
    with store.connect() as db:
        rows = db.execute("""SELECT m.id,v.id AS version_id,v.name,v.aliases_json,v.description,v.plan_json
            FROM manual_metric m JOIN manual_metric_version v ON v.id=m.active_version_id
            WHERE m.deleted_at IS NULL AND v.status='PUBLISHED'""").fetchall()
        drafts = db.execute("""SELECT m.id,v.name,v.aliases_json FROM manual_metric m
            JOIN manual_metric_version v ON v.metric_id=m.id
            WHERE m.deleted_at IS NULL AND v.status!='PUBLISHED'
            AND v.version_no=(SELECT MAX(latest.version_no) FROM manual_metric_version latest
                              WHERE latest.metric_id=m.id)""").fetchall()
    exact = [r for r in rows if question.strip().casefold() in [r["name"].casefold(),
             *[a.casefold() for a in json.loads(r["aliases_json"] or "[]")]]]
    if len(exact) == 1:
        return {"decision": "MATCH", "metricId": exact[0]["id"], "versionId": exact[0]["version_id"], "reason": "EXACT_NAME"}
    if len(exact) > 1:
        return {"decision": "AMBIGUOUS", "candidates": [r["id"] for r in exact], "reason": "DUPLICATE_NAME"}
    draft_exact = [r for r in drafts if question.strip().casefold() in [r["name"].casefold(),
                   *[a.casefold() for a in json.loads(r["aliases_json"] or "[]")]]]
    if draft_exact:
        return {"decision": "MATCH_UNAVAILABLE", "candidates": [r["id"] for r in draft_exact],
                "reason": "DRAFT_NOT_PUBLISHED"}
    if not rows:
        return {"decision": "NO_MATCH", "reason": "EMPTY_CATALOG"}
    ranking = _bm25_rank(question, rows)
    if len(question.strip()) >= 3:
        try:
            phrase = '"' + question.strip().replace('"', '""') + '"'
            with store.connect() as db:
                for hit in db.execute("""SELECT metric_id,bm25(manual_metric_fts) AS score
                    FROM manual_metric_fts WHERE manual_metric_fts MATCH ? ORDER BY score LIMIT 20""", (phrase,)):
                    ranking[hit["metric_id"]] = ranking.get(hit["metric_id"], 0) + 1 / (1 + abs(hit["score"]))
        except Exception as exc:
            if "no such table" not in str(exc) and "no such tokenizer" not in str(exc):
                raise
    candidates = sorted(rows, key=lambda row: ranking.get(row["id"], 0), reverse=True)
    if gateway is None:
        return {"decision": "RETRIEVAL_UNCERTAIN", "candidates": [r["id"] for r in candidates], "reason": "MODEL_REQUIRED"}
    from .models import Gateway
    judge = gateway or Gateway(store)
    decisions = []
    for offset in range(0, len(candidates), 50):
        batch = candidates[offset:offset + 50]
        value = judge.call("metric_match", {"question": question,
            "candidates": [{"id": r["id"], "name": r["name"], "description": r["description"]} for r in batch],
            "instruction": "只判断问题是否明确指向某个已发布指标。歧义返回 AMBIGUOUS；不把来源行数当作手术台次。"},
            MATCH_SCHEMA, budget_id or uid("metric_route_"))
        require(value["metricId"] is None or value["metricId"] in [r["id"] for r in batch],
                "MODEL_SCHEMA_INVALID", "模型返回了目录外指标")
        require(value["decision"] != "MATCH" or value["metricId"] is not None,
                "MODEL_SCHEMA_INVALID", "模型未提供命中的指标 ID")
        decisions.append(value)
    hits = [d["metricId"] for d in decisions if d["decision"] == "MATCH"]
    if len(hits) == 1 and all(d["decision"] != "AMBIGUOUS" for d in decisions):
        row = next(r for r in candidates if r["id"] == hits[0])
        return {"decision": "MATCH", "metricId": row["id"], "versionId": row["version_id"], "reason": "MODEL_FULL_CATALOG"}
    if hits or any(d["decision"] == "AMBIGUOUS" for d in decisions):
        return {"decision": "AMBIGUOUS", "candidates": list(dict.fromkeys(hits or [r["id"] for r in candidates])),
                "reason": "MODEL_AMBIGUOUS"}
    return {"decision": "NO_MATCH", "reason": "MODEL_FULL_CATALOG"}


def execute_metric(store, session_id, metric_id, revision_id=None):
    with store.connect() as db:
        metric, _ = _current(db, metric_id)
        require(metric["active_version_id"], "METRIC_UNPUBLISHED", "指标尚未发布")
        version = db.execute("SELECT * FROM manual_metric_version WHERE id=?", (metric["active_version_id"],)).fetchone()
        require(version["status"] == "PUBLISHED", "METRIC_UNPUBLISHED", "指标尚未发布")
        plan = json.loads(version["plan_json"])
    result = execute_plan(store, session_id, plan, revision_id, version["kb_release_id"], version["kb_content_sha256"])
    require(result["sql"] == version["sql_template"] and result["releaseHash"] == version["kb_content_sha256"],
            "SQL_CHANGED", "指标 SQL 或知识库版本已变化")
    return result | {"metricId": metric_id, "metricVersionId": version["id"], "metricName": version["name"]}
