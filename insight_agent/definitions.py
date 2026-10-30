"""Document rules and immutable snapshots, with legacy publication support."""
from __future__ import annotations


import re
import json
from contextlib import nullcontext

from .core import DELETE_CONFIRMATION_TEXT, AppError, LIMITS, digest, now, require, sha, uid
from .domains import manifest as domain_manifest
from .engine import SCHEMA, validate_rule
from .files import snapshot_for
from .skills import WORD_SPEC_EXTRACTION_INSTRUCTION, WORD_SPEC_EXTRACTION_VERSION, runtime_manifest


_VERSION_NUMBER = re.compile(r"^v(\d+)$", re.IGNORECASE)


def _next_version_label(store, family_id, db):
    """Return the next numeric version while the caller owns the write lock."""
    highest = 0
    for definition in store.list("definition", db):
        if definition.get("familyId") != family_id:
            continue
        match = _VERSION_NUMBER.fullmatch(str(definition.get("versionLabel", "")).strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"v{highest + 1}"


def create_draft(store, attachment_id, name, parent_id=None, *, reuse_existing=False):
    attachment = store.get("attachment", attachment_id)
    require(attachment["route"]["role"] == "METRIC_DEFINITION" and attachment["status"] == "READY", "ROUTE_UNRESOLVED", "请先确认文件用途为指标说明")
    snapshot = snapshot_for(store, attachment)
    require(snapshot["kind"] == "document", "FORMAT_UNSUPPORTED", "指标定义目前要求 Word 文档，请将指标说明另存为 DOCX")
    nodes = snapshot["nodes"]
    session_id = attachment["scope"] if attachment["scope"] != "library" else None
    with store.transaction() as db:
        # Replays of an import or route request must return the original draft.
        # Legacy snapshots stay unchanged; a session reimport gets scoped rules.
        existing = next((d for d in store.list("definition", db)
                         if d.get("attachmentId") == attachment_id
                         and not d.get("deletedAt")
                         and (not session_id or d.get("sessionId") == session_id)), None)
        if reuse_existing and existing:
            return existing
        definition_name = name or attachment["name"]
        parent = store.get("definition", parent_id, db) if parent_id else None
        if parent:
            require_not_deleted(parent)
        if parent is None and not session_id:
            # Automatic imports can continue a series only when the document
            # name identifies the same source. Different names start at v1.
            all_definitions = [d for d in store.list("definition", db)
                               if not d.get("sessionId") and not d.get("deletedAt")]
            same_name = [d for d in all_definitions if d.get("name") == definition_name]
            if not same_name:
                families = {d.get("familyId", d["id"]) for d in all_definitions if not d.get("disabled")}
                if len(families) == 1:
                    same_name = [d for d in all_definitions if d.get("familyId", d["id"]) in families]
            parent = max(same_name, key=lambda d: (d.get("updated", ""), d["id"])) if same_name else None
            parent_id = parent["id"] if parent else None
        family_id = parent.get("familyId", parent["id"]) if parent else uid("family_")
        version_label = _next_version_label(store, family_id, db)
        draft = store.put("definition", {"name": definition_name, "familyId": family_id, "versionLabel": version_label, "parentId": parent_id, "notes": "", "default": False, "parser": {k: snapshot.get(k) for k in ("parserVersion", "parts", "finalViewPolicy")}, "convertedSha256": attachment.get("convertedSha256"), "status": "DRAFT", "attachmentId": attachment_id, "documentHash": attachment["sha256"], "nodes": nodes, "amendments": [], "candidates": [], "coverage": [], "reviewPolicy": "DETERMINISTIC_APPROVED", "manifestHash": None, "disabled": False}, db)
        if session_id:
            draft = store.put("definition", draft | {"sessionId": session_id}, db)
        store.event(db, draft["id"], "definition.created", {"nodeCount": len(nodes)})
        return draft


def evidence_documents(draft):
    documents = {draft["documentHash"]: {n["locator"]: n["text"] for n in draft["nodes"]}}
    for amendment in draft["amendments"]:
        documents[amendment["sha256"]] = {amendment["locator"]: amendment["text"]}
    return documents


def require_not_deleted(definition):
    require(not definition.get("deletedAt"), "DEFINITION_DELETED", "指标说明已删除，请重新上传或选择其他指标版本")


def _require_editable(db, definition_id):
    require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('QUEUED','RUNNING')", (definition_id,)).fetchone(),
            "DEFINITION_BUSY", "解析期间不能修改，请先完成或取消任务")


def amend(store, definition_id, text, actor, scope, replaces, revision):
    require(text.strip() and actor.strip() and scope.strip(), "AMENDMENT_INCOMPLETE", "补充说明需填写条款、维护人和适用范围")
    require(len(text) <= 10000, "FILE_LIMIT", "补充说明过长")
    with store.transaction() as db:
        draft = store.get("definition", definition_id, db)
        require_not_deleted(draft)
        require(draft["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可修改")
        _require_editable(db, definition_id)
        known = {n["locator"] for n in draft["nodes"]}
        require(all(x in known for x in replaces), "EVIDENCE_INVALID", "替代条款定位不存在")
        blob = store.blob(text.encode())
        amendment = {"sha256": blob, "locator": "amendment:" + uid(), "text": text, "actor": actor, "scope": scope, "replaces": replaces, "created": now(), "kind": "USER_AMENDMENT"}
        draft["amendments"].append(amendment)
        return store.put("definition", draft, db, expected=revision)


def save_candidates(store, definition_id, candidates, coverage, revision, policy="DETERMINISTIC_APPROVED", *, provenance=None, db=None):
    require(len(candidates) <= LIMITS["candidates"], "FILE_LIMIT", "指标候选数量超限")
    require(policy in ("DETERMINISTIC_APPROVED", "AI_REQUIRED"), "INVALID_POLICY", "复核政策不受支持")
    with store.transaction() if db is None else nullcontext(db) as db:
        draft = store.get("definition", definition_id, db)
        require_not_deleted(draft)
        require(draft["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可修改")
        if not provenance or provenance.get("kind") != "MODEL_EXTRACTION":
            _require_editable(db, definition_id)
        evidence = evidence_documents(draft)
        ids = set()
        cleaned = []
        for candidate in candidates:
            require(set(candidate) <= {"id", "name", "status", "reason", "rule", "locators"}, "INVALID_CANDIDATE", "候选包含未知字段")
            require(candidate.get("id") and candidate["id"] not in ids, "INVALID_REFERENCE", "候选 ID 为空或重复")
            ids.add(candidate["id"])
            require(candidate.get("status") in ("CLEAR", "SKIP"), "DEFINITION_INCOMPLETE", "每个候选必须有明确执行或跳过结论")
            if candidate["status"] == "CLEAR":
                validate_rule(candidate["rule"], evidence)
                require(candidate["id"] == candidate["rule"]["metricId"], "INVALID_REFERENCE", "候选与规则指标 ID 不一致")
            else:
                require(bool(candidate.get("reason")), "DEFINITION_INCOMPLETE", "跳过候选需说明原因")
                require(not candidate.get("rule"), "DEFINITION_INCOMPLETE", "跳过候选不得携带可执行规则")
            known_locators = {locator for document in evidence.values() for locator in document}
            require(candidate.get("locators") and all(locator in known_locators for locator in candidate["locators"]), "EVIDENCE_INVALID", "候选必须定位到真实原文或人工补充")
            cleaned.append(candidate)
        require(set(draft.get("metricExclusions", {})) <= ids, "DELETED_METRIC_REFERENCE", "重新提取或高级编辑需保留已删除指标的原文引用；如需重新建立全部指标，请上传新版本说明")
        node_locators = {n["locator"] for n in draft["nodes"]} | {a["locator"] for a in draft["amendments"]}
        require(len(coverage) == len(node_locators) and {c.get("locator") for c in coverage} == node_locators, "DOCUMENT_COVERAGE_INCOMPLETE", "必须检查并登记全部文档节点")
        for item in coverage:
            require(set(item) <= {"locator", "disposition", "candidateIds", "reason"} and item.get("disposition") in ("CANDIDATE", "CONTEXT", "NON_METRIC"), "DOCUMENT_COVERAGE_INCOMPLETE", "文档节点处理结论无效")
            require(set(item.get("candidateIds", [])) <= ids, "INVALID_REFERENCE", "覆盖清单引用了不存在的指标")
            require(item.get("reason") and (item["disposition"] != "CANDIDATE" or item.get("candidateIds")), "DOCUMENT_COVERAGE_INCOMPLETE", "文档节点需有检查依据")
        # Advanced edits and model replacement must not reopen an outdated form.
        if not provenance or provenance.get("kind") != "MANUAL_FORM":
            previous = {candidate["id"]: candidate for candidate in draft["candidates"]}
            unchanged = {candidate["id"] for candidate in cleaned if candidate == previous.get(candidate["id"])}
            if "manualMetrics" in draft:
                draft["manualMetrics"] = {key: value for key, value in draft["manualMetrics"].items() if key in unchanged}
        draft.update(candidates=cleaned, coverage=coverage, reviewPolicy=policy)
        record = {"kind": "MANUAL_REVIEW", "runtime": runtime_manifest()} if provenance is None else dict(provenance)
        record.update(at=now(), candidatesHash=digest(cleaned), coverageHash=digest(coverage))
        draft.setdefault("provenanceHistory", []).append(record)
        return store.put("definition", draft, db, expected=revision)


def publish(store, definition_id, revision, actor, reviewed, session_id=None, session_revision=None, *, extraction_job=None):
    # Uploaded definitions bind privately; library publication freezes reusable versions.
    automatic = extraction_job is not None
    require(automatic or reviewed and actor.strip(), "REVIEW_REQUIRED", "请核对原文、全部候选和口径后显式发布")
    with store.transaction() as db:
        draft = store.get("definition", definition_id, db)
        require_not_deleted(draft)
        if automatic:
            store.check_job(extraction_job["id"], extraction_job["lease"])
            require(extraction_job["kind"] == "extract" and extraction_job["scope"] == definition_id and draft.get("sessionId") == session_id, "SCOPE_FORBIDDEN", "指标解析不属于当前对话")
        require(draft["status"] == "DRAFT", "IMMUTABLE_VERSION", "该版本已发布")
        require(draft["coverage"] and draft["candidates"], "DOCUMENT_COVERAGE_INCOMPLETE", "尚未完成全文指标提取与检查")
        excluded = draft.get("metricExclusions", {})
        require(isinstance(excluded, dict) and set(excluded) <= {c["id"] for c in draft["candidates"]}, "INVALID_REFERENCE", "删除记录引用了不存在的指标，请检查后发布")
        require(any(c["id"] not in excluded for c in draft["candidates"]), "NO_VALID_DEFINITION", "全部指标均已删除，不能发布空指标版本")
        required_locators = {n["locator"] for n in draft["nodes"]} | {a["locator"] for a in draft["amendments"]}
        require(len(draft["coverage"]) == len(required_locators) and {c["locator"] for c in draft["coverage"]} == required_locators, "DOCUMENT_COVERAGE_INCOMPLETE", "新增条款尚未完成全文覆盖复验，请重新校验保存")
        require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND kind='extract' AND status IN ('QUEUED','RUNNING') AND id<>?", (definition_id, extraction_job["id"] if automatic else "")).fetchone(), "DEFINITION_BUSY", "请等待指标提取完成")
        if draft.get("sourceKind") == "MANUAL":
            require(draft["manualSourceHash"] == draft["documentHash"] and draft["nodes"] == [{"locator": "manual:source", "text": store.read_blob(draft["manualSourceHash"]).decode(), "quoteSha256": draft["manualSourceHash"]}], "EVIDENCE_INVALID", "手工口径原件与证据不一致")
        else:
            attachment = store.get("attachment", draft["attachmentId"], db)
            store.read_blob(attachment["sha256"])
            store.read_blob(attachment["snapshotHash"])
            if attachment.get("convertedSha256"):
                store.read_blob(attachment["convertedSha256"])
        for amendment in draft["amendments"]:
            require(store.read_blob(amendment["sha256"]).decode("utf-8") == amendment["text"], "EVIDENCE_INVALID", "补充说明原件与证据不一致")
        fill_plan = draft.get("reportPlan")
        if fill_plan:
            from .report_plan import template, validate_intent
            skeleton = {**fill_plan, "slots": [{k: v for k, v in s.items() if k != "plan"} for s in fill_plan["slots"]]}
            require(skeleton == template(draft["nodes"]) and len(fill_plan["slots"]) == len(draft["candidates"]), "DOCUMENT_COVERAGE_INCOMPLETE", "待填位置与原文不一致")
            for slot, candidate in zip(fill_plan["slots"], draft["candidates"]):
                validate_intent(slot, slot["plan"])
                require(candidate == {"id": slot["id"], "name": slot["plan"]["name"], "status": "REPORT", "plan": slot["plan"], "locators": [slot["locator"]]},
                        "DOCUMENT_COVERAGE_INCOMPLETE", "候选清单与待填计划不一致")
        require(fill_plan or any(c["status"] == "CLEAR" and c["id"] not in excluded for c in draft["candidates"]), "NO_VALID_DEFINITION", "至少需要一项经校验可执行指标才能发布")
        evidence = evidence_documents(draft)
        for candidate in draft["candidates"]:
            if candidate["status"] == "CLEAR":
                validate_rule(candidate["rule"], evidence)
        superseded = {x for a in draft["amendments"] for x in a["replaces"]}
        for candidate in draft["candidates"]:
            if candidate["status"] == "CLEAR":
                require(not any(e["documentSha256"] == draft["documentHash"] and e["locator"] in superseded for e in candidate["rule"]["sourceEvidence"]), "EVIDENCE_CONFLICT", "规则仍在引用已被补充说明替代的条款")
        domains = {"surgery"} if fill_plan else {c["rule"]["domainProfile"]["id"] for c in draft["candidates"] if c["status"] == "CLEAR"}
        require(len(domains) == 1, "DOMAIN_MISMATCH", "同一版本只能绑定一个明确领域包，请拆分混合领域口径")
        domain = domain_manifest(next(iter(domains)))
        manifest = {"schemaVersion": "definition-manifest/1.1", "familyId": draft.get("familyId", draft["id"]), "versionLabel": draft.get("versionLabel", "v1"), "parentId": draft.get("parentId"), "notes": draft.get("notes", ""), "parser": draft.get("parser"), "convertedSha256": draft.get("convertedSha256"), "domainPack": domain, "documentHash": draft["documentHash"], "nodesHash": digest(draft["nodes"]), "amendments": draft["amendments"], "candidates": draft["candidates"], "coverage": draft["coverage"], "reviewPolicy": draft["reviewPolicy"], "actor": actor, "publishedAt": now()}
        manifest["provenanceHistory"] = draft.get("provenanceHistory", [])
        if excluded:
            manifest["metricExclusions"] = excluded
        if draft.get("metricNotes"):
            manifest["metricNotes"] = draft["metricNotes"]
        if fill_plan:
            manifest["reportPlan"] = fill_plan
            manifest["schemaVersion"] = "definition-manifest/1.2"
        if automatic:
            manifest.update(sessionId=session_id, validationMode="AUTOMATIC_DOCUMENT", extractionJobId=extraction_job["id"])
        manifest["ruleDigests"] = {c["id"]: digest(c["rule"]) for c in draft["candidates"] if c["status"] == "CLEAR"}
        manifest["evidenceDocuments"] = [{"kind": draft.get("sourceKind", "WORD"), "sha256": draft["documentHash"], "locators": [n["locator"] for n in draft["nodes"]]}] + [{"kind": "USER_AMENDMENT", "sha256": a["sha256"], "locators": [a["locator"]], "replaces": a["replaces"]} for a in draft["amendments"]]
        draft.update(status="PUBLISHED", manifest=manifest, manifestHash=digest(manifest))
        result = store.put("definition", draft, db, expected=revision)
        if session_id:
            session = store.get("session", session_id, db)
            require(not draft.get("sessionId") or draft["sessionId"] == session_id, "SCOPE_FORBIDDEN", "指标说明不属于目标对话")
            require(not draft.get("disabled"), "DEFINITION_UNAVAILABLE", "停用指标版本不能新绑定")
            require(session["state"] == "DRAFT" and session["revision"] == session_revision, "REVISION_CONFLICT", "目标会话已变化，发布与绑定均已回滚")
            require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('QUEUED','RUNNING') AND (?=0 OR status='RUNNING')", (session_id, int(automatic))).fetchone(), "SESSION_BUSY", "会话有活动任务")
            if automatic:
                attachment = store.get("attachment", draft["attachmentId"], db)
                require(session.get("definitionId") == definition_id and draft["attachmentId"] in session["attachmentIds"] and attachment["route"]["role"] == "METRIC_DEFINITION", "SCOPE_FORBIDDEN", "指标说明已移除或用途已变更")
            session["definitionId"] = definition_id
            session["domainPackId"] = domain["id"]
            session["mappingRevisionId"] = None
            session["mappings"] = {}
            session = store.put("session", session, db, expected=session_revision)
            if automatic:
                # Only this preparation's own binding revision may advance a
                # waiting turn. Other user changes still fail revision checks.
                waiting = db.execute("""SELECT id,payload FROM jobs WHERE kind='turn' AND scope=?
                    AND status='QUEUED' AND cancel=0
                    AND json_extract(payload,'$.waitForJobId')=?""", (session_id, extraction_job["id"])).fetchall()
                for pending in waiting:
                    turn = store.get("turn", json.loads(pending["payload"])["turnId"], db)
                    if (turn["state"] == "PENDING" and turn["sessionRevision"] == session_revision
                            and turn.get("preparationJobId") == extraction_job["id"]
                            and turn.get("preparationDefinitionId") == definition_id):
                        turn["sessionRevision"] = session["revision"]
                        store.put("turn", turn, db)
                        store.event(db, session_id, "turn.prepared", {"turnId": turn["id"], "jobId": pending["id"],
                                    "definitionId": definition_id, "preparationJobId": extraction_job["id"]})
        store.event(db, draft["id"], "session.metrics.ready" if automatic else "definition.published", {"manifestHash": result["manifestHash"]})
        return result


def extract(store, definition_id, gateway, budget_id):
    draft = store.get("definition", definition_id)
    require_not_deleted(draft)
    require(draft["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可重新解析")
    from .report_plan import extract_plan, progress, template
    progress(store, definition_id, budget_id, "STARTED", nodeCount=len(draft["nodes"]))
    if template(draft["nodes"])["slots"]:
        return extract_plan(store, draft, gateway, budget_id)
    output_schema = {"type": "object", "additionalProperties": False, "required": ["candidates", "coverage"], "properties": {"candidates": {"type": "array", "maxItems": 200, "items": {"type": "object"}}, "coverage": {"type": "array", "items": {"type": "object"}}}}
    # Parser metadata (for example LEGACY_DOC_TEXT's source marker) stays
    # local. Only evidence fields belong in the model request contract.
    nodes = [{"locator": n["locator"], "text": n["text"],
              "quoteSha256": n["quoteSha256"], "documentSha256": draft["documentHash"]}
             for n in draft["nodes"]]
    for amendment in draft["amendments"]:
        nodes.append({"locator": amendment["locator"], "text": amendment["text"], "documentSha256": amendment["sha256"], "quoteSha256": sha(amendment["text"].encode()), "replaces": amendment["replaces"]})
    from .models import redact_text
    require(all(redact_text(n["text"]) == n["text"] for n in nodes), "SENSITIVE_DEFINITION", "指标说明含敏感标识或本机路径，请导入脱敏后的指标文档再在线提取；原件仍可本地审阅")
    progress(store, definition_id, budget_id, "EXTRACTING", nodeCount=len(nodes), unit="nodes")
    response = gateway.call("definition", {"instruction": WORD_SPEC_EXTRACTION_INSTRUCTION, "nodes": nodes, "domain": ["hospital_ops/1.0.0", "surgery/1.0.0"], "ruleSchema": SCHEMA}, output_schema, budget_id)
    progress(store, definition_id, budget_id, "VALIDATING", completed=0, total=len(response["candidates"]), unit="metrics")
    # Invalid candidates become explicit semantic skips; coverage failures abort extraction.
    for candidate in response["candidates"]:
        if candidate.get("status") == "CLEAR":
            try:
                validate_rule(candidate.get("rule"), evidence_documents(draft))
            except (AppError, TypeError) as error:
                candidate.update(status="SKIP", reason=getattr(error, "code", "INVALID_RULE_SCHEMA"))
                candidate.pop("rule", None)
    calls = [c for c in store.list("model_call") if c["budgetId"] == budget_id and c["stage"] == "definition"]
    provenance = {"kind": "MODEL_EXTRACTION", "runtime": runtime_manifest(), "outputSchemaHash": digest(output_schema),
                  "extractionPromptVersion": WORD_SPEC_EXTRACTION_VERSION,
                  "extractionPromptHash": digest(WORD_SPEC_EXTRACTION_INSTRUCTION),
                  "calls": [{k: c.get(k) for k in ("id", "profileId", "profileRevision", "promptHash", "requestId", "responseModel", "status")} for c in calls]}
    result = save_candidates(store, definition_id, response["candidates"], response["coverage"], draft["revision"], provenance=provenance)
    progress(store, definition_id, budget_id, "READY", completed=len(result["candidates"]), total=len(result["candidates"]),
             clearCount=sum(c["status"] == "CLEAR" for c in result["candidates"]),
             skippedCount=sum(c["status"] == "SKIP" for c in result["candidates"]), unit="metrics")
    return result


def metadata(store, definition_id, revision, version_label, notes):
    require(isinstance(version_label, str) and 0 < len(version_label.strip()) <= 40 and len(notes) <= 2000, "INVALID_VERSION", "版本标签或说明无效")
    with store.transaction() as db:
        draft = store.get("definition", definition_id, db)
        require_not_deleted(draft)
        require(draft["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可修改")
        _require_editable(db, definition_id)
        require(not any(d["id"] != definition_id and d.get("familyId") == draft.get("familyId") and d.get("versionLabel") == version_label.strip() for d in store.list("definition", db)), "VERSION_LABEL_CONFLICT", "同一口径系列的版本标签不能重复")
        draft.update(versionLabel=version_label.strip(), notes=notes)
        return store.put("definition", draft, db, expected=revision)


def _contains_definition_reference(value, definition_id, key=None):
    """Find references in persisted JSON without treating arbitrary text as an ID."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key in {"definitionId", "definition_id", "parentId", "preparationDefinitionId"} and child == definition_id:
                return child_key
            found = _contains_definition_reference(child, definition_id, child_key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_definition_reference(child, definition_id, key)
            if found:
                return found
    return None


def delete(store, definition_id, revision, confirmation):
    """Remove a version from management; retain referenced snapshots for history."""
    require(confirmation == DELETE_CONFIRMATION_TEXT, "DELETE_CONFIRMATION", f"请输入“{DELETE_CONFIRMATION_TEXT}”确认删除")
    with store.transaction() as db:
        definition = store.get("definition", definition_id, db)
        require_not_deleted(definition)
        require(definition["revision"] == revision, "REVISION_CONFLICT", "指标说明已变化，请刷新后重试")
        active = db.execute(
            "SELECT 1 FROM jobs WHERE kind IN ('extract','metric_catalog') AND scope=? AND status IN ('QUEUED','RUNNING') LIMIT 1",
            (definition_id,),
        ).fetchone()
        require(not active, "DEFINITION_BUSY", "指标说明正在提取，请等待处理完成后再删除")

        references = []
        # A draft's own catalog is derived content, removed with an unused
        # draft. Other persisted references (including turns and fill attempts)
        # need the original snapshot even after it leaves the management list.
        for row in db.execute("SELECT kind,id,data FROM objects WHERE kind<>'metric_catalog'"):
            if row["kind"] == "definition" and row["id"] == definition_id:
                continue
            reference = _contains_definition_reference(json.loads(row["data"]), definition_id)
            if reference:
                references.append({"kind": row["kind"], "id": row["id"], "field": reference})
        if references:
            store.put("definition", definition | {"deletedAt": now(), "disabled": True, "default": False}, db, expected=revision)
            store.event(db, definition_id, "definition.deleted", {"revision": revision, "retainedForHistory": True, "references": references})
            return {"deletedDefinitionId": definition_id, "retainedForHistory": True}

        attachment_id = definition.get("attachmentId")
        db.execute("DELETE FROM objects WHERE kind='definition' AND id=?", (definition_id,))
        db.execute("DELETE FROM objects WHERE kind='metric_catalog' AND id=?", (definition_id,))
        store.event(db, definition_id, "definition.deleted", {"revision": revision})

        # The source attachment is an implementation detail of the draft. Keep it
        # when another object still uses it; otherwise leave its blobs for the
        # normal conservative blob cleanup pass.
        if attachment_id:
            still_used = any(
                value.get("attachmentId") == attachment_id
                for value in store.list("definition", db)
            )
            if not still_used:
                still_used = any(
                    attachment_id in value.get("attachmentIds", [])
                    for value in store.list("session", db)
                )
            if not still_used:
                db.execute("DELETE FROM objects WHERE kind='attachment' AND id=?", (attachment_id,))
        return {"deletedDefinitionId": definition_id}


def compare_versions(left, right):
    def rules(version):
        return {c["id"]: c | ({"maintenance": version["metricNotes"][c["id"]]} if c["id"] in version.get("metricNotes", {}) else {})
                for c in version["candidates"] if c["id"] not in version.get("metricExclusions", {})}
    old, new = rules(left), rules(right)
    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()), "changed": [{"id": id, "before": old[id], "after": new[id]} for id in sorted(old.keys() & new.keys()) if digest(old[id]) != digest(new[id])]}
