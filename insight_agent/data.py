"""Explicit dataset-to-standard-field mappings with grain and coverage checks."""
from __future__ import annotations

from .core import LIMITS, checkpoint, digest, require
from .files import snapshot_for


def catalog(store, session):
    tables = []
    for id in session["attachmentIds"]:
        attachment = store.get("attachment", id)
        if attachment["route"]["role"] != "DATA_FILE" or attachment["status"] != "READY":
            continue
        snapshot = snapshot_for(store, attachment)
        for sheet in snapshot.get("sheets", []):
            tables.append({"attachmentId": id, "fileName": attachment["name"], "sheet": sheet["name"], "headers": sheet["headers"], "rowCount": sheet["rowCount"], "executionBlocked": snapshot.get("executionBlocked")})
    return tables


def suggest_mappings(store, session, definition):
    tables = catalog(store, session)
    mappings = {}
    for candidate in definition["candidates"]:
        if candidate["status"] != "CLEAR" or candidate["id"] in definition.get("metricExclusions", {}):
            continue
        fields = [f["id"] for f in candidate["rule"]["requiredFields"]]
        eligible = [t for t in tables if set(fields) <= set(t["headers"]) and not t["executionBlocked"]]
        if len(eligible) == 1:
            table = eligible[0]
            mappings[candidate["id"]] = {"sources": [{"attachmentId": table["attachmentId"], "sheet": table["sheet"], "fields": {f: f for f in fields}}], "mode": "union", "coverage": None}
    return mappings


def document_mappings(store, session, definition):
    """Use exact field names over uploaded records; make no date coverage claim."""
    tables = catalog(store, session)
    mappings = {}
    for candidate in definition["candidates"]:
        if candidate["status"] != "CLEAR" or candidate["id"] in definition.get("metricExclusions", {}):
            continue
        fields = [f["id"] for f in candidate["rule"]["requiredFields"]]
        sources = [{"attachmentId": t["attachmentId"], "sheet": t["sheet"], "fields": {f: f for f in fields}}
                   for t in tables if set(fields) <= set(t["headers"]) and not t["executionBlocked"]]
        if sources:
            mappings[candidate["id"]] = {"sources": sources, "mode": "union", "coverage": {
                "complete": True, "evidence": "当前对话上传文件中匹配字段的工作表的全部记录，仅表示本次输入范围，不声明期间数据完整性"}}
    return mappings


def validate_mappings(store, session, definition, mappings):
    """Validate saved structure without executing rows or asserting coverage."""
    require(isinstance(mappings, dict), "MAPPING_UNRESOLVED", "映射必须是对象")
    rules = {c["id"]: c["rule"] for c in definition["candidates"] if c["status"] == "CLEAR" and c["id"] not in definition.get("metricExclusions", {})}
    require(set(mappings) <= set(rules), "MAPPING_UNRESOLVED", "映射包含未知指标")
    tables = {(t["attachmentId"], t["sheet"]): t for t in catalog(store, session)}
    for metric_id, mapping in mappings.items():
        require(isinstance(mapping, dict) and set(mapping) <= {"mode", "sources", "coverage", "relationship"}, "MAPPING_UNRESOLVED", "映射结构无效")
        sources, mode = mapping.get("sources"), mapping.get("mode", "union")
        require(mode in ("union", "join") and isinstance(sources, list) and 1 <= len(sources) <= 50 and (mode != "join" or len(sources) == 2), "MAPPING_UNRESOLVED", "来源数量或准备模式无效")
        standard = {f["id"] for f in rules[metric_id]["requiredFields"]}
        pairs, mapped = set(), set()
        for source in sources:
            require(isinstance(source, dict) and set(source) == {"attachmentId", "sheet", "fields"}, "MAPPING_UNRESOLVED", "来源结构无效")
            pair = (source["attachmentId"], source["sheet"])
            require(pair in tables and pair not in pairs, "MAPPING_UNRESOLVED", "来源不存在或重复")
            pairs.add(pair)
            fields = source["fields"]
            require(isinstance(fields, dict) and set(fields) <= standard and all(isinstance(v, str) and v in tables[pair]["headers"] for v in fields.values()) and len(set(fields.values())) == len(fields), "MAPPING_UNRESOLVED", "字段映射不唯一或表头不存在")
            require(mode != "union" or set(fields) == standard, "MAPPING_UNRESOLVED", "合并来源需各自覆盖全部标准字段")
            mapped.update(fields)
        require(mapped == standard, "MAPPING_UNRESOLVED", "标准字段映射不完整")
        if mode == "join":
            rel = mapping.get("relationship", {})
            require(isinstance(rel, dict) and set(rel) == {"keys", "cardinality", "unmatchedPolicy", "evidence"}, "MAPPING_UNRESOLVED", "关联政策结构无效")
            keys = rel["keys"]
            require(isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys) and len(set(keys)) == len(keys) and all(set(keys) <= set(s["fields"]) for s in sources), "MAPPING_UNRESOLVED", "左右表需要共同关联键")
            require(rel["cardinality"] in ("1:1", "1:n", "n:1") and rel["unmatchedPolicy"] in ("reject", "exclude") and isinstance(rel["evidence"], str) and rel["evidence"].strip(), "MAPPING_UNRESOLVED", "需明确基数、覆盖和关联依据")
        coverage = mapping.get("coverage")
        require(coverage is None or isinstance(coverage, dict) and set(coverage) <= {"start", "end", "complete", "evidence"}, "MAPPING_UNRESOLVED", "完整范围结构无效")
    return mappings


def record_mapping_revision(store, session, definition, db, review=None):
    """Immutable preparation snapshot. Structure checks never imply row validation."""
    review = review or {'actor': '本机使用者', 'reason': '通过会话接口保存映射', 'source': 'USER_RULE'}
    require(isinstance(review, dict) and set(review) <= {'actor', 'reason', 'source'} and
            all(isinstance(review.get(k), str) and 0 < len(review[k].strip()) <= 500 for k in ('actor', 'reason')) and
            review.get('source', 'USER_RULE') in ('USER_RULE', 'SYSTEM_DICTIONARY'), 'MAPPING_REVIEW_REQUIRED', '请填写映射维护人和修改依据')
    validate_mappings(store, session, definition, session['mappings'])
    fields, relationships = [], []
    rules = {c['id']: c['rule'] for c in definition['candidates'] if c['status'] == 'CLEAR' and c['id'] not in definition.get('metricExclusions', {})}
    for metric_id, mapping in session['mappings'].items():
        rule = rules[metric_id]
        types = {f['id']: f['valueType'] for f in rule['requiredFields']}
        for source in mapping['sources']:
            attachment = store.get('attachment', source['attachmentId'], db)
            for standard, column in source['fields'].items():
                fields.append({'metricId': metric_id, 'standardField': standard, 'attachmentId': attachment['id'],
                               'sha256': attachment['sha256'], 'snapshotHash': attachment['snapshotHash'],
                               'sheet': source['sheet'], 'column': column, 'expectedType': types[standard],
                               'normalization': 'ENGINE_TYPED_VALUES', 'candidateSource': review.get('source', 'USER_RULE'),
                               'checks': {'headerExists': True, 'uniqueColumnBinding': True},
                               'valueValidation': 'DEFERRED_UNTIL_RUN'})
        if mapping.get('mode') == 'join':
            content = {'sessionId': session['id'], 'metricId': metric_id, 'definitionId': definition['id'],
                       'sources': mapping['sources'], 'policy': mapping['relationship'],
                       'coverage': mapping.get('coverage'), 'review': review,
                       'status': 'STRUCTURE_VALIDATED', 'dataChecks': 'DEFERRED_UNTIL_RUN'}
            relationships.append(store.put('relationship_revision', content | {'digest': digest(content)}, db)['id'])
    content = {'schemaVersion': 'mapping-revision/1.0', 'sessionId': session['id'], 'definitionId': definition['id'],
               'definitionManifestHash': definition['manifestHash'], 'parentId': session.get('mappingRevisionId'),
               'mappings': session['mappings'], 'mappingHash': digest(session['mappings']), 'fields': fields,
               'relationshipRevisionIds': relationships, 'review': review, 'status': 'STRUCTURE_VALIDATED',
               'checksDeferred': ['ROW_TYPES', 'ENTITY_KEYS', 'JOIN_CARDINALITY', 'DATA_COVERAGE']}
    revision = store.put('mapping_revision', content | {'digest': digest(content)}, db)
    session['mappingRevisionId'] = revision['id']
    return revision


def materialize(store, session, rule, mapping, parameters, metadata_only=False):
    require(mapping and mapping.get("sources"), "MAPPING_UNRESOLVED", "指标尚未配置唯一字段映射")
    mode = mapping.get("mode", "union")
    require(mode in ("union", "join"), "UNSUPPORTED_PREPARATION", "该准备算子尚未注册")
    require(mode != "join" or len(mapping["sources"]) == 2, "MAPPING_UNRESOLVED", "关联需要明确左右两个来源")
    coverage = mapping.get("coverage")
    require(isinstance(coverage, dict) and coverage.get("complete") is True and coverage.get("evidence"), "COVERAGE_UNCONFIRMED", "需声明并记录数据范围完整性依据")
    tw = rule["timeWindow"]
    if parameters.get("scope") != "ALL_DATA":
        require(coverage.get("start", "9999") <= parameters[tw["startParameter"]] and coverage.get("end", "") >= parameters[tw["endParameter"]], "DATA_INSUFFICIENT", "输入数据声明的完整范围不足以覆盖查询窗口")
    rows, provenance, used, source_rows, mapped = [], [], set(), [], set()
    standard = {f["id"] for f in rule["requiredFields"]}
    for source in mapping["sources"]:
        require(source["attachmentId"] in session["attachmentIds"], "SCOPE_FORBIDDEN", "不能引用其他会话的数据文件")
        attachment = store.get("attachment", source["attachmentId"])
        require(attachment["route"]["role"] == "DATA_FILE" and attachment["status"] == "READY", "ROUTE_UNRESOLVED", "数据文件用途尚未确定")
        snapshot = snapshot_for(store, attachment)
        require(not snapshot.get("executionBlocked"), "FORMAT_UNSUPPORTED", "当前文件解析版本仅支持预览，请重新导入文件")
        sheet = next((s for s in snapshot["sheets"] if s["name"] == source["sheet"]), None)
        require(sheet is not None, "MAPPING_UNRESOLVED", "映射中的工作表不存在")
        pair = (attachment["sha256"], source["sheet"])
        require(pair not in used, "DATA_CONFLICT", "同一来源不能重复合并")
        used.add(pair)
        fields = source["fields"]
        require((set(fields) == standard if mode == "union" else set(fields) <= standard) and set(fields.values()) <= set(sheet["headers"]) and len(set(fields.values())) == len(fields), "MAPPING_UNRESOLVED", "标准字段与表头映射必须完整且唯一")
        mapped.update(fields)
        current = []
        if not metadata_only:
            for index, row in enumerate(sheet["rows"]):
                if index % 256 == 0:
                    checkpoint()
                current.append({**{f: row["values"].get(column) for f, column in fields.items()}, "_source": {"attachmentId": attachment["id"], "sha256": attachment["sha256"], "sheet": sheet["name"], "row": row["row"], "columns": fields}})
        source_rows.append(current)
        provenance.append({"sha256": attachment["sha256"], "snapshotHash": attachment["snapshotHash"], "sheet": sheet["name"], "rowCount": len(sheet["rows"]), "fields": fields})
        require(sum(p["rowCount"] for p in provenance) <= LIMITS["session_rows"], "FILE_LIMIT", "会话总数据行数超限")
    require(mapped == standard, "MAPPING_UNRESOLVED", "关联后的标准字段不完整")
    relationship = None
    if mode == "join":
        relationship = mapping.get("relationship", {})
        keys = relationship.get("keys", [])
        require(keys and len(set(keys)) == len(keys) and all(set(keys) <= set(s["fields"]) for s in mapping["sources"]), "MAPPING_UNRESOLVED", "左右来源必须映射相同的非空关联键")
        require(relationship.get("cardinality") in ("1:1", "1:n", "n:1"), "UNSUPPORTED_CARDINALITY", "需明确 1:1、1:n 或 n:1，禁止多对多")
        require(relationship.get("unmatchedPolicy") in ("reject", "exclude"), "COVERAGE_UNCONFIRMED", "需显式声明未匹配记录的处理政策")
        require(relationship.get("evidence"), "COVERAGE_UNCONFIRMED", "需记录关联依据")
    audit = {"sources": provenance, "mappingHash": digest(mapping), "coverage": coverage, "mode": mode, "inputRows": sum(p["rowCount"] for p in provenance), "analysisScope": parameters.get("scope", "DATE_WINDOW")}
    if metadata_only:
        return [], audit | {"rowCount": None, "checksDeferred": ["ROW_TYPES", "UNIQUE_KEYS", "JOIN_COVERAGE"], "metadataOnly": True}
    if mode == "union":
        rows = [r for source in source_rows for r in source]
    else:
        right_fields = {f: f for f in mapping["sources"][1]["fields"]}
        rows, join_audit = join_rows(*source_rows, relationship["keys"], relationship["cardinality"], right_fields, relationship["unmatchedPolicy"] == "reject")
        audit["relationship"] = join_audit
    return rows, audit | {"rowCount": len(rows)}


def join_rows(left, right, keys, cardinality, fields, require_full=True):
    """Registered equality join. No many-to-many or silent dictionary overwrites."""
    require(cardinality in ("1:1", "1:n", "n:1"), "UNSUPPORTED_CARDINALITY", "不支持多对多关联")
    left_index, right_index = {}, {}
    for rows, index in ((left, left_index), (right, right_index)):
        for index_value, row in enumerate(rows):
            if index_value % 256 == 0:
                checkpoint()
            key = tuple(row.get(k) for k in keys)
            require(all(isinstance(k, (str, int, bool)) and k != "" for k in key), "DATA_INSUFFICIENT", "关联键缺失或类型无效")
            index.setdefault(key, []).append(row)
    require(cardinality == "n:1" or all(len(v) == 1 for v in left_index.values()), "DATA_CONFLICT", "左表关联键不唯一")
    require(cardinality == "1:n" or all(len(v) == 1 for v in right_index.values()), "DATA_CONFLICT", "右表关联键不唯一")
    require(not require_full or left_index.keys() <= right_index.keys(), "DATA_INSUFFICIENT", "关联覆盖不足")
    result = []
    for key, members in left_index.items():
        checkpoint()
        for lrow in members:
            for rrow in right_index.get(key, []):
                if len(result) % 256 == 0:
                    checkpoint()
                require(not any(name in lrow and lrow[name] != rrow.get(column) for name, column in fields.items()), "DATA_CONFLICT", "关联字段冲突")
                require(len(result) < LIMITS["session_rows"], "FILE_LIMIT", "关联后行数超限")
                result.append(lrow | {name: rrow.get(column) for name, column in fields.items()} | {"_source": {"left": lrow.get("_source"), "right": rrow.get("_source")}})
    return result, {"leftRows": len(left), "rightRows": len(right), "outputRows": len(result), "unmatchedLeftKeys": len(left_index.keys() - right_index.keys()), "unmatchedRightKeys": len(right_index.keys() - left_index.keys()), "cardinality": cardinality}
