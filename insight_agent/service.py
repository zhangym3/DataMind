"""Application API shared by desktop and integration tests."""
from __future__ import annotations

import json
import time

from . import definitions
from .core import AppError, ENGINE_VERSION, LIMITS, VERSION, digest, dumps, now, require
from .data import catalog, record_mapping_revision, suggest_mappings, validate_mappings
from .engine import validate_parameters
from .word_spec import legacy_doc_available
from .files import converter_path, route_status, snapshot_for
from .models import Credentials, Gateway, PRESETS, public_profile
from .store import Store
from .skills import runtime_manifest
from .conversation import (
    LOCAL_ACTIONS,
    LOCAL_COMMANDS,
    formula_reply,
    infer_local_action,
    local_reply,
    material_context,
    material_references,
    query_result,
    resolve_history,
)

TERMINAL = {"SUCCEEDED", "SUCCEEDED_WITH_SKIPS", "NO_VALID_DEFINITION", "NO_ELIGIBLE_METRICS", "FAILED", "CANCELLED"}
DEFAULT_SETTINGS = {"fontScale": 100, "contextCollapsed": False, "retentionDays": 90, "reportTitle": "数据指标分析报告", "modelEnabled": True, "autoResume": True, "runSeconds": 900, "modelCalls": 30, "modelTokens": LIMITS["model_tokens"], "template": "builtin/1.0"}


class AppService:
    def __init__(self, home=None, credentials=None):
        self.store = Store(home)
        self.credentials = credentials

    def bootstrap(self, search="", archived=False, offset=0, limit=30):
        page = self.session_list(search, archived, offset, limit)
        jobs = self.store.jobs()
        processing = [{"scope": j["scope"], "kind": j["kind"], "status": j["status"], "updated": j["updated"]}
                      for j in jobs if j["status"] in ("QUEUED", "RUNNING")]
        definitions_in_store = self.store.list("definition")
        library = [{k: d.get(k) for k in ("id", "name", "versionLabel", "status", "disabled", "default", "updated", "created", "revision", "familyId", "sourceKind")}
                   | {"metricCount": sum(c["id"] not in d.get("metricExclusions", {}) for c in d["candidates"])} for d in definitions_in_store
                   if not d.get("sessionId") and not d.get("deletedAt")]
        definition_attachments = {d.get("attachmentId") for d in definitions_in_store}
        library_files = [{k: a[k] for k in ("id", "name", "status", "route")} for a in self.store.list("attachment") if a["scope"] == "library" and a["id"] not in definition_attachments and a["status"] != "EXCLUDED"]
        from .library_progress import activities
        return {"version": VERSION, "definitions": library, "libraryAttachments": library_files, "libraryActivities": activities(self.store, definitions_in_store), "sessions": page["items"], "sessionPage": {k: v for k, v in page.items() if k != "items"}, "profiles": [public_profile(p) for p in self.store.list("model")], "jobs": [{k: v for k, v in j.items() if k not in ("payload", "result")} for j in jobs], "processing": processing, "settings": self.settings(), "readiness": self.readiness()}

    def session_list(self, search="", archived=False, offset=0, limit=30):
        require(isinstance(search, str) and len(search) <= 120 and isinstance(offset, int) and offset >= 0 and 1 <= limit <= 100, "INVALID_PAGE", "分页或搜索参数无效")
        with self.store.connect() as db:
            where = "kind='session' AND json_extract(data,'$.archived')=? AND (instr(lower(json_extract(data,'$.name')),lower(?))>0 OR instr(id,?)>0)"
            args = (int(archived), search, search)
            count = db.execute("SELECT count(*) FROM objects WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT id FROM objects WHERE " + where + " ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?", args + (limit, offset)).fetchall()
            items = [{k: value[k] for k in ("id", "name", "state", "archived", "updated")} for row in rows for value in [self.store.get("session", row[0], db)]]
        return {"items": items, "total": count, "offset": offset, "limit": limit}

    def settings(self):
        existing = self.store.list("settings")
        return DEFAULT_SETTINGS | existing[0] if existing else DEFAULT_SETTINGS | {"id": "general", "revision": 0}

    def save_settings(self, values):
        require(set(values) <= set(DEFAULT_SETTINGS), "INVALID_SETTING", "设置项不受支持")
        settings = self.settings() | values
        require(settings["fontScale"] in (100, 125, 150), "INVALID_SETTING", "界面缩放支持 100%、125%、150%")
        require(type(settings["contextCollapsed"]) is bool, "INVALID_SETTING", "上下文折叠状态无效")
        require(settings["template"] == "builtin/1.0" and isinstance(settings["reportTitle"], str) and 1 <= len(settings["reportTitle"].strip()) <= 120 and type(settings["autoResume"]) is bool and type(settings["modelEnabled"]) is bool, "INVALID_SETTING", "模板、标题或开关设置无效")
        require(1 <= settings["retentionDays"] <= 3650 and 60 <= settings["runSeconds"] <= 900 and 1 <= settings["modelCalls"] <= 30 and 1000 <= settings["modelTokens"] <= LIMITS["model_tokens"], "INVALID_SETTING", "预算或保留期限超出允许范围")
        with self.store.transaction() as db:
            return self.store.put("settings", settings, db)

    def readiness(self):
        profiles = self.store.list("model")
        default = next((p for p in profiles if p.get("default") and p.get("enabled")), None)
        return {"python": "3.11", "modelConfigured": bool(default and default.get("hasKey")), "modelTested": bool(default and default.get("check", {}) and default["check"]["status"] == "PASS"), "converter": bool(converter_path()), "xlsx": True, "docx": True, "xlsPreview": True, "xlsExecution": True, "pdf": bool(converter_path()), "doc": legacy_doc_available(), "docLegacy": legacy_doc_available(), "join": True, "firstEvent": False, "sequenceClassify": False, "snapshotAsOf": False, "dataHome": str(self.store.home), "presets": PRESETS}

    def create_session(self, name="新数据对话", definition_id=None):
        if definition_id:
            definition = self.store.get("definition", definition_id)
            require(definition["status"] == "PUBLISHED" and not definition["disabled"], "DEFINITION_UNAVAILABLE", "请选择启用的已发布指标版本")
            require(not definition.get("sessionId"), "SCOPE_FORBIDDEN", "对话内指标说明不能绑定到其他对话")
        with self.store.transaction() as db:
            return self.store.put("session", {"name": name[:120] or "新数据对话", "definitionId": definition_id, "domainPackId": definition["manifest"]["domainPack"]["id"] if definition_id else None, "attachmentIds": [], "mappings": {}, "state": "DRAFT", "archived": False, "draft": "", "baselineHash": None}, db)

    def session_detail(self, session_id):
        definition = self.session_metrics(session_id)
        session = self.store.get("session", session_id)
        candidates = [c for c in definition["candidates"] if c["id"] not in definition.get("metricExclusions", {})] if definition else []
        summary = {k: definition[k] for k in ("id", "name", "status", "attachmentId")} | {"metricCount": len(candidates), "clearCount": sum(c["status"] == "CLEAR" for c in candidates)} if definition else None
        if summary:
            summary.update(sessionId=definition.get("sessionId"), sourceKind=definition.get("sourceKind", "WORD"), versionLabel=definition.get("versionLabel", "v1"))
            summary["fillSlotCount"] = sum(s["id"] not in definition.get("metricExclusions", {}) for s in definition.get("reportPlan", {}).get("slots", []))
            summary["extractionMode"] = definition.get("extractionMode")
            from .metric_catalog import current_catalog
            parsed = current_catalog(self.store, definition)
            summary["catalogSummary"] = {"status": parsed["status"], "count": len(parsed["items"]),
                "completed": sum(i["status"] != "PENDING" for i in parsed["items"])} if parsed else None
        return {"session": session, "metricDefinition": summary, "attachments": [self.store.get("attachment", id) for id in session["attachmentIds"]], "messages": list(reversed([m for m in self.store.list("message") if m["sessionId"] == session_id])), "runs": [r for r in self.store.list("run") if r["sessionId"] == session_id], "tables": catalog(self.store, session), "artifacts": [a for a in self.store.list("artifact") if a["sessionId"] == session_id], "comparisons": [c for c in self.store.list("comparison") if c["sessionId"] == session_id], "activities": self.session_activities(session_id)}

    def session_metrics(self, session_id):
        session = self.store.get("session", session_id)
        if not session.get("definitionId"):
            definition = self._recover_session_metrics(session_id)
            if not definition:
                return None
        else:
            definition = self.store.get("definition", session["definitionId"])
        if definition.get("sessionId"):
            attachment = self.store.get("attachment", definition["attachmentId"])
            require(definition["sessionId"] == session_id and attachment["id"] in session["attachmentIds"] and attachment["route"]["role"] == "METRIC_DEFINITION", "SCOPE_FORBIDDEN", "指标说明不属于当前对话")
        return definition

    def _recover_session_metrics(self, session_id):
        """Reconnect one legacy upload without changing published snapshots."""
        with self.store.transaction() as db:
            session = self.store.get("session", session_id, db)
            if session.get("definitionId"):
                return self.store.get("definition", session["definitionId"], db)
            if session["state"] != "DRAFT":
                return None
            attachments = [self.store.get("attachment", aid, db) for aid in session["attachmentIds"]]
            documents = [a for a in attachments if a["route"]["role"] == "METRIC_DEFINITION"]
            if len(documents) != 1:
                return None
            attachment = documents[0]
            if attachment["scope"] != session_id or attachment["status"] != "READY" or attachment["profile"]["kind"] != "document":
                return None
            drafts = [d for d in self.store.list("definition", db)
                      if d.get("attachmentId") == attachment["id"] and d["status"] == "DRAFT"
                      and not d.get("disabled") and d.get("sessionId") in (None, session_id)]
            if len(drafts) != 1:
                return None
            definition = drafts[0]
            if any(s["id"] != session_id and s.get("definitionId") == definition["id"]
                   for s in self.store.list("session", db)):
                return None
            if not definition.get("sessionId"):
                definition = self.store.put("definition", definition | {"sessionId": session_id}, db)
            session.update(definitionId=definition["id"], domainPackId=None, mappings={}, mappingRevisionId=None)
            self.store.put("session", session, db)
            self.store.event(db, session_id, "session.metrics.recovered", {"definitionId": definition["id"], "attachmentId": attachment["id"]})
            return definition

    def attach_session_definition(self, session_id, definition_id):
        with self.store.transaction() as db:
            session = self.store.get("session", session_id, db)
            definition = self.store.get("definition", definition_id, db)
            definitions.require_not_deleted(definition)
            attachment = self.store.get("attachment", definition["attachmentId"], db)
            require(session["state"] == "DRAFT", "SESSION_LOCKED", "会话已锁定")
            require(definition.get("sessionId") == session_id and attachment["id"] in session["attachmentIds"] and attachment["route"]["role"] == "METRIC_DEFINITION", "SCOPE_FORBIDDEN", "只能使用当前对话的指标说明")
            if session.get("definitionId") == definition_id:
                return session
            session.update(definitionId=definition_id, domainPackId=definition.get("manifest", {}).get("domainPack", {}).get("id"), mappings={}, mappingRevisionId=None)
            return self.store.put("session", session, db)

    def retry_session_metrics(self, session_id):
        session = self.store.get("session", session_id)
        require(session["state"] == "DRAFT", "SESSION_LOCKED", "会话已锁定，请在新对话导入新的指标说明")
        definition = self.session_metrics(session_id)
        require(definition and definition.get("sessionId") == session_id, "DEFINITION_UNAVAILABLE", "请先上传当前对话的指标说明")
        return self.prepare_uploaded_metrics(session_id, retry=True)

    def prepare_uploaded_metrics(self, session_id, *, retry=False):
        """Read a Word-only upload; prepare executable rules when data exists."""
        session = self.store.get("session", session_id)
        require(session["state"] == "DRAFT" and not session["archived"], "SESSION_LOCKED", "请先恢复对话或新建数据对话")
        definition = self.session_metrics(session_id)
        if not definition or definition["status"] != "DRAFT":
            return {}
        has_data = any(self.store.get("attachment", aid)["route"]["role"] == "DATA_FILE"
                       for aid in session["attachmentIds"])
        if has_data:
            if definition["candidates"] and not retry:
                return {}
            return {"jobId": self.ensure_definition_extract(definition["id"]), "kind": "extract"}
        return self.parse_session_metrics(session_id) | {"kind": "metric_catalog"}

    def session_metric_catalog(self, session_id):
        from .metric_catalog import current_catalog
        definition = self.session_metrics(session_id)
        return current_catalog(self.store, definition) if definition else None

    def parse_session_metrics(self, session_id):
        """Read the current Word independently of executable rules or Excel."""
        definition = self.session_metrics(session_id)
        require(definition and definition.get("sessionId") == session_id,
                "DEFINITION_UNAVAILABLE", "请先上传并确认当前对话的 Word 指标说明")
        from .metric_catalog import current_catalog
        with self.store.transaction() as db:
            session = self.store.get("session", session_id, db)
            require(not session["archived"], "SESSION_ARCHIVED", "请先恢复已归档的对话")
            definition = self.store.get("definition", definition["id"], db)
            definitions.require_not_deleted(definition)
            attachment = self.store.get("attachment", definition["attachmentId"], db)
            require(session.get("definitionId") == definition["id"] and attachment["id"] in session["attachmentIds"]
                    and attachment["route"]["role"] == "METRIC_DEFINITION", "SCOPE_FORBIDDEN", "指标说明已移除或更换")
            parsed = current_catalog(self.store, definition)
            if parsed and parsed["status"] == "READY":
                return {"catalogId": parsed["id"], "status": "READY"}
            active = db.execute("SELECT id FROM jobs WHERE kind='metric_catalog' AND scope=? AND status IN ('QUEUED','RUNNING') ORDER BY created LIMIT 1",
                                (definition["id"],)).fetchone()
            job_id = active[0] if active else self.store.enqueue(db, "metric_catalog", definition["id"], {"definitionId": definition["id"]})
            return {"jobId": job_id, "definitionId": definition["id"]}

    def session_activities(self, session_id):
        """Return safe, user-facing processing history for one conversation.

        Extraction jobs use a definition id as their scope, so they are included
        when the definition was created from an attachment in this session. Job
        payloads are intentionally omitted because they can contain local paths.
        """
        session = self.store.get("session", session_id)
        attachment_ids = set(session.get("attachmentIds", []))
        definition_ids = {
            definition["id"]
            for definition in self.store.list("definition")
            if definition.get("attachmentId") in attachment_ids
        }
        jobs = []
        for job in self.store.jobs():
            if job["scope"] != session_id and not (job["kind"] in ("extract", "metric_catalog") and job["scope"] in definition_ids):
                continue
            error_details = (job.get("error") or {}).get("details", {})
            jobs.append({
                "id": job["id"],
                "kind": job["kind"],
                "definitionId": job["scope"] if job["kind"] in ("extract", "metric_catalog") else error_details.get("definitionId"),
                "scope": session_id if job["scope"] != session_id else job["scope"],
                "status": job["status"],
                "created": job["created"],
                "updated": job["updated"],
                "result": job["result"],
                "error": job["error"],
            })
        scopes = [session_id, *sorted(definition_ids)]
        placeholders = ",".join("?" for _ in scopes)
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT seq,type,data,created FROM events WHERE scope IN ({placeholders}) ORDER BY seq",
                scopes,
            ).fetchall()
        events = [{"seq": row["seq"], "type": row["type"], "data": json.loads(row["data"]), "created": row["created"]} for row in rows]
        return {"jobs": jobs, "events": events}

    def update_session(self, session_id, revision, changes, mapping_review=None):
        require(set(changes) <= {"name", "archived", "draft", "definitionId", "mappings", "attachmentIds"}, "INVALID_SESSION", "会话修改包含未知字段")
        with self.store.transaction() as db:
            session = self.store.get("session", session_id, db)
            if "archived" in changes:
                require(type(changes["archived"]) is bool, "INVALID_SESSION", "归档状态无效")
                require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('QUEUED','RUNNING')", (session_id,)).fetchone(), "SESSION_BUSY", "请先完成或取消当前会话任务")
            if set(changes) & {"definitionId", "mappings", "attachmentIds"}:
                require(session["state"] == "DRAFT", "SESSION_LOCKED", "会话已锁定，请新建独立数据对话后修改")
                require(not any(j["scope"] == session_id and j["status"] in ("QUEUED", "RUNNING") for j in self.store.jobs()), "SESSION_BUSY", "任务运行期间不能修改数据基线")
            if changes.get("definitionId"):
                definition = self.store.get("definition", changes["definitionId"], db)
                require(definition["status"] == "PUBLISHED" and not definition["disabled"], "DEFINITION_UNAVAILABLE", "指标版本不可用")
                require(not definition.get("sessionId") or definition["sessionId"] == session_id, "SCOPE_FORBIDDEN", "指标说明不属于当前对话")
                changes = changes | {"domainPackId": definition["manifest"]["domainPack"]["id"]}
            if "definitionId" in changes:
                changes = changes | {"mappings": {}}
                if not changes["definitionId"]:
                    changes = changes | {"domainPackId": None}
            if "attachmentIds" in changes:
                require(set(changes["attachmentIds"]) <= set(session["attachmentIds"]), "SCOPE_FORBIDDEN", "只能移除当前会话附件")
                if session.get("definitionId") and self.store.get("definition", session["definitionId"], db).get("attachmentId") in set(session["attachmentIds"]) - set(changes["attachmentIds"]):
                    changes = changes | {"definitionId": None, "domainPackId": None, "mappings": {}}
                elif changes["attachmentIds"] != session["attachmentIds"]:
                    changes = changes | {"mappings": {}}
            session.update(changes)
            if "definitionId" in changes or "attachmentIds" in changes:
                session["mappingRevisionId"] = None
            if "mappings" in changes:
                require(session["definitionId"] or not changes["mappings"], "DEFINITION_UNAVAILABLE", "请先上传并解析指标说明")
                if session["definitionId"] and not ("attachmentIds" in changes or "definitionId" in changes):
                    record_mapping_revision(self.store, session, self.store.get("definition", session["definitionId"], db), db, mapping_review)
            if "name" in changes:
                require(isinstance(changes["name"], str) and 0 < len(changes["name"].strip()) <= 120, "INVALID_SESSION", "标题需为 1 至 120 字")
            return self.store.put("session", session, db, expected=revision)

    def clear_conversation(self, session_id, revision):
        """Remove visible chat messages while retaining the session's data history."""
        with self.store.transaction() as db:
            session = self.store.get("session", session_id, db)
            require(not session["archived"], "SESSION_ARCHIVED", "请先恢复归档会话")

            attachment_ids = set(session.get("attachmentIds", []))
            definition_ids = [
                definition["id"]
                for definition in self.store.list("definition", db)
                if definition.get("attachmentId") in attachment_ids
            ]
            scopes = [session_id, *definition_ids]
            placeholders = ",".join("?" for _ in scopes)
            active = db.execute(
                f"SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING') AND scope IN ({placeholders}) LIMIT 1",
                scopes,
            ).fetchone()
            require(not active, "SESSION_BUSY", "当前会话有正在处理的任务，请完成或取消后再清除")

            message_ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM objects WHERE kind='message' AND json_extract(data,'$.sessionId')=?",
                    (session_id,),
                ).fetchall()
            ]
            for message_id in message_ids:
                db.execute("DELETE FROM objects WHERE kind='message' AND id=?", (message_id,))

            session["draft"] = ""
            result = self.store.put("session", session, db, expected=revision)
            self.store.event(db, session_id, "conversation.cleared", {"messageCount": len(message_ids)})
            return result

    def import_paths(self, paths, session_id=None):
        require(0 < len(paths) <= LIMITS["batch_files"], "FILE_LIMIT", "每批最多 20 个文件")
        from pathlib import Path
        require(sum(Path(p).stat().st_size for p in paths if Path(p).is_file()) <= LIMITS["batch_bytes"], "FILE_LIMIT", "每批总大小不能超过 200 MB")
        files = []
        for index, value in enumerate(paths, 1):
            path = Path(value)
            files.append({
                "index": index,
                "name": path.name,
                "size": path.stat().st_size if path.is_file() else None,
            })
        with self.store.transaction() as db:
            if session_id:
                session = self.store.get("session", session_id, db)
                require(session["state"] == "DRAFT", "SESSION_LOCKED", "会话已锁定，请新建对话导入文件")
                require(not any(j["scope"] == session_id and j["kind"] == "turn" and j["status"] in ("QUEUED", "RUNNING") for j in self.store.jobs()), "SESSION_BUSY", "分析已提交，暂不能变更数据")
            scope = session_id or "library"
            job_id = self.store.enqueue(db, "import", scope, {"paths": paths, "sessionId": session_id})
            self.store.event(db, scope, "import.started", {"jobId": job_id, "files": files, "total": len(files)})
            return {"jobId": job_id}

    def ensure_definition_extract(self, definition_id, db=None):
        """Queue one extraction attempt for a draft and reuse an active attempt."""
        if db is None:
            with self.store.transaction() as connection:
                return self.ensure_definition_extract(definition_id, connection)
        definition = self.store.get("definition", definition_id, db)
        definitions.require_not_deleted(definition)
        require(definition["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可重新提取")
        active = db.execute(
            "SELECT id FROM jobs WHERE kind='extract' AND scope=? AND status IN ('QUEUED','RUNNING') ORDER BY created,rowid LIMIT 1",
            (definition_id,),
        ).fetchone()
        if active:
            return active[0]
        return self.store.enqueue(db, "extract", definition_id, {"definitionId": definition_id})

    def route(self, attachment_id, role):
        require(role in ("DATA_FILE", "METRIC_DEFINITION", "OTHER"), "INVALID_ROUTE", "请选择数据、指标说明或其他文件")
        with self.store.transaction() as db:
            attachment = self.store.get("attachment", attachment_id, db)
            detected = attachment.get("detectedRoute", attachment["route"])
            if attachment["scope"] != "library":
                session = self.store.get("session", attachment["scope"], db)
                require(session["state"] == "DRAFT", "SESSION_LOCKED", "会话已锁定")
                require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('QUEUED','RUNNING')", (session["id"],)).fetchone(), "SESSION_BUSY", "任务进行期间不能改变文件用途")
            require(role == "OTHER" or detected["role"] != "MIXED", "MIXED_UNSUPPORTED", "混合文件需先拆分，不允许强制作为单一用途")
            require(role == "OTHER" or route_status(detected["role"], attachment["profile"]["kind"], detected["confidence"]) != "UNSUPPORTED_ROLE_FORMAT", "FORMAT_UNSUPPORTED", "文件用途与可执行格式不匹配，请另存为对应格式；不能通过改用途绕过")
            require(role != "DATA_FILE" or attachment["profile"]["kind"] == "workbook", "FORMAT_UNSUPPORTED", "数据文件要求 Excel")
            require(role != "METRIC_DEFINITION" or attachment["profile"]["kind"] == "document", "FORMAT_UNSUPPORTED", "指标说明要求 Word，请另存为 DOCX")
            if role == "METRIC_DEFINITION" and attachment["scope"] != "library":
                has_other_metric = any(
                    item_id != attachment_id
                    and self.store.get("attachment", item_id, db)["route"]["role"] == "METRIC_DEFINITION"
                    for item_id in session["attachmentIds"]
                )
                require(not has_other_metric, "METRIC_DEFINITION_LIMIT", "一个数据对话只能有一份指标说明，请先移除当前指标说明后再确认。")
            attachment.setdefault("routeHistory", []).append({"route": attachment["route"], "changedAt": now()})
            attachment["detectedRoute"] = detected
            attachment["route"] = {"role": role, "reason": "用户显式确认用途", "confidence": "CONFIRMED", "routerVersion": "manual/1.0"}
            attachment["status"] = route_status(role, attachment["profile"]["kind"], "CONFIRMED")
            result = self.store.put("attachment", attachment, db)
            if attachment["scope"] != "library":
                if role != "METRIC_DEFINITION" and session.get("definitionId") and self.store.get("definition", session["definitionId"], db)["attachmentId"] == attachment_id:
                    session.update(definitionId=None, domainPackId=None, mappings={}, mappingRevisionId=None)
                elif role != "METRIC_DEFINITION":
                    session.update(mappings={}, mappingRevisionId=None)
                self.store.put("session", session, db)
        if role == "METRIC_DEFINITION" and result["profile"]["kind"] == "document":
            definition = definitions.create_draft(self.store, attachment_id, result["name"], reuse_existing=True)
            if attachment["scope"] != "library":
                self.attach_session_definition(attachment["scope"], definition["id"])
                if definition["status"] == "DRAFT":
                    self.prepare_uploaded_metrics(attachment["scope"])
        elif role == "DATA_FILE" and attachment["scope"] != "library":
            self.prepare_uploaded_metrics(attachment["scope"])
        return result

    def preview(self, attachment_id, session_id=None):
        attachment = self.store.get("attachment", attachment_id)
        require(attachment["scope"] == (session_id or "library") or session_id and attachment_id in self.store.get("session", session_id)["attachmentIds"], "SCOPE_FORBIDDEN", "附件不属于当前范围")
        snapshot = snapshot_for(self.store, attachment)
        if snapshot["kind"] == "workbook":
            return {"attachment": attachment, "sheets": [{**s, "rows": s["rows"][:20]} for s in snapshot["sheets"]]}
        return {"attachment": attachment, "nodes": snapshot["nodes"]}

    def suggest_mapping(self, session_id):
        session = self.store.get("session", session_id)
        definition = self.require_analysis_definition(session)
        return suggest_mappings(self.store, session, definition)

    def run_context(self, session_id, run_id):
        run = self.store.get("run", run_id)
        require(run["sessionId"] == session_id, "SCOPE_FORBIDDEN", "运行不属于当前会话")
        result = {"run": run}
        for key, kind in (("intentDecisionId", "intent"), ("historyContextId", "history_context")):
            if run.get(key):
                result[kind] = self.store.get(kind, run[key])
        if run["contract"].get("mappingRevisionId"):
            mapping = self.store.get("mapping_revision", run["contract"]["mappingRevisionId"])
            result["mapping_revision"] = mapping
            result["relationship_revisions"] = [self.store.get("relationship_revision", id) for id in mapping["relationshipRevisionIds"]]
        return result

    def submit_user_turn(self, session_id, revision, request_id, text, action="AUTO", parameters=None, source_run_id=None):
        require(action in LOCAL_ACTIONS | {"AUTO", "PLAN", "ANALYZE", "EXPLAIN", "SUMMARY", "MATERIAL_QUERY", "RECALCULATE_FROZEN", "REGENERATE", "RESULT_QUERY", "FORMULA_INSPECT"}, "INVALID_ACTION", "操作不受支持")
        require(isinstance(text, str) and len(text) <= 4000 and (text.strip() or action != "AUTO"), "INVALID_MESSAGE", "请输入消息，最多 4000 字")
        require(parameters is None or isinstance(parameters, dict), "INVALID_PARAMETER", "参数必须为对象")
        payload = {"sessionId": session_id, "revision": revision, "text": text, "action": action, "parameters": parameters or {}, "sourceRunId": source_run_id}
        hash = digest(payload)
        with self.store.transaction() as db:
            existing = db.execute("SELECT hash,response FROM requests WHERE id=?", (request_id,)).fetchone()
            if existing:
                require(existing[0] == hash, "IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同内容")
                return json.loads(existing[1])
            session = self.store.get("session", session_id, db)
            require(session["revision"] == revision, "REVISION_CONFLICT", "会话状态已变化，请刷新")
            require(not session["archived"], "SESSION_ARCHIVED", "请先恢复归档会话")
            require(not any(j["scope"] == session_id and j["status"] in ("QUEUED", "RUNNING") for j in self.store.jobs()), "SESSION_BUSY", "当前会话已有任务，请等待或取消")
            preparation = {}
            requested_action = (LOCAL_COMMANDS.get(text.strip()) or infer_local_action(text) or "AUTO") if action == "AUTO" else action
            if requested_action in ("PLAN", "ANALYZE", "AUTO") and session["state"] == "DRAFT" and session.get("definitionId"):
                definition = self.store.get("definition", session["definitionId"], db)
                if definition["status"] == "DRAFT" and definition.get("sessionId") == session_id and not definition.get("disabled"):
                    active = db.execute("SELECT id FROM jobs WHERE kind='extract' AND scope=? AND status IN ('QUEUED','RUNNING') ORDER BY created,rowid LIMIT 1", (definition["id"],)).fetchone()
                    if active:
                        preparation = {"preparationJobId": active[0], "preparationDefinitionId": definition["id"]}
            # Register an already active preparation before it can publish and
            # advance the session revision ahead of this queued turn.
            turn = self.store.put("turn", payload | {"sessionRevision": revision, "state": "PENDING", "requestId": request_id} | preparation, db)
            job_payload = {"turnId": turn["id"]}
            if preparation:
                job_payload["waitForJobId"] = preparation["preparationJobId"]
            job_id = self.store.enqueue(db, "turn", session_id, job_payload)
            response = {"turnId": turn["id"], "jobId": job_id}
            db.execute("INSERT INTO requests VALUES(?,?,?,?)", (request_id, hash, dumps(response), now()))
            self.store.event(db, session_id, "turn.accepted", response)
            if preparation:
                self.store.event(db, session_id, "turn.preparing", response | preparation)
            return response

    def require_analysis_definition(self, session):
        """Only a validated snapshot of this conversation's document may execute."""
        definition = self.session_metrics(session["id"])
        if not definition:
            raise AppError("DEFINITION_UNAVAILABLE", "请上传当前对话的 Word 指标说明，解析完成后即可计算。", {"nextAction": "UPLOAD_DEFINITION"})
        if definition["status"] == "PUBLISHED" and (session["state"] == "LOCKED" or not definition["disabled"]):
            return definition
        details = {"definitionId": definition["id"], "nextAction": "RETRY_EXTRACTION"}
        with self.store.connect() as db:
            latest = db.execute("SELECT status,error FROM jobs WHERE kind='extract' AND scope=? ORDER BY created DESC,rowid DESC LIMIT 1", (definition["id"],)).fetchone()
            if not latest:
                latest = db.execute("SELECT status,error FROM jobs WHERE kind='metric_catalog' AND scope=? ORDER BY created DESC,rowid DESC LIMIT 1", (definition["id"],)).fetchone()
        if latest and latest["status"] in ("QUEUED", "RUNNING"):
            details["nextAction"] = "WAIT_EXTRACTION"
            message = "指标说明正在解析，请等待完成后再计算。"
        elif latest and latest["status"] in ("FAILED", "CANCELLED"):
            error = json.loads(latest["error"]) if latest["error"] else {}
            details["extractionErrorCode"] = error.get("code")
            reason = error.get('message', '解析已停止').rstrip('。.!！')
            message = f"指标说明解析未完成：{reason}。请重试解析。"
        elif definition["candidates"] and not any(c["status"] == "CLEAR" for c in definition["candidates"]):
            details["nextAction"] = "VIEW_METRICS"
            message = "当前说明中的指标均无法计算，请查看各项原因并补充说明文档后重新上传。"
        else:
            message = "指标说明尚未完成校验，请重试解析或更换说明文档。"
        raise AppError("DEFINITION_UNAVAILABLE", message, details)

    def prepare_analysis_definition(self, session, turn, job):
        """Prepare this turn's own Word rules once, then resume the same turn."""
        store = self.store
        with store.transaction() as db:
            current = store.get("session", session["id"], db)
            require(current["revision"] == turn["sessionRevision"], "REVISION_CONFLICT", "准备期间会话发生变化，请刷新后重试")
            require(not current["archived"], "SESSION_ARCHIVED", "请先恢复归档会话")
            definition = store.get("definition", current["definitionId"], db) if current.get("definitionId") else None
            dependency_id = turn.get("preparationJobId")
            if dependency_id:
                require(definition and definition["id"] == turn.get("preparationDefinitionId")
                        and definition.get("sessionId") == current["id"], "REVISION_CONFLICT", "等待期间指标说明已变更，请重新分析")
                dependency = db.execute("SELECT kind,scope,status,error FROM jobs WHERE id=?", (dependency_id,)).fetchone()
                require(dependency and dependency["kind"] == "extract" and dependency["scope"] == definition["id"],
                        "DEFINITION_UNAVAILABLE", "本次指标准备任务已丢失，请重新分析。",
                        {"definitionId": definition["id"], "nextAction": "RETRY_EXTRACTION"})
                if dependency["status"] in ("QUEUED", "RUNNING"):
                    return store.defer(job, dependency_id, db)
                if dependency["status"] != "SUCCEEDED":
                    error = json.loads(dependency["error"]) if dependency["error"] else {}
                    reason = error.get("message", "解析已停止").rstrip('。.!！')
                    raise AppError("DEFINITION_UNAVAILABLE", f"本次指标规则准备未完成：{reason}。可重试分析。",
                                   {"definitionId": definition["id"], "preparationJobId": dependency_id,
                                    "nextAction": "RETRY_EXTRACTION", "extractionErrorCode": error.get("code", dependency["status"])})
                return None
            if (not definition or definition["status"] != "DRAFT" or definition.get("disabled")
                    or definition.get("sessionId") != current["id"] or current["state"] != "DRAFT"):
                return None
            # A completed all-SKIP extraction is a business gap, not another
            # model attempt every time the user asks to calculate.
            if definition["candidates"] and all(c["status"] == "SKIP" for c in definition["candidates"]):
                return None
            attachments = [store.get("attachment", aid, db) for aid in current["attachmentIds"]]
            require(definition["attachmentId"] in current["attachmentIds"]
                    and store.get("attachment", definition["attachmentId"], db)["route"]["role"] == "METRIC_DEFINITION",
                    "SCOPE_FORBIDDEN", "指标说明已移除或用途已变更")
            require(all(a["status"] in ("READY", "EXCLUDED") for a in attachments), "ROUTE_UNRESOLVED", "请先确认或排除全部未知、混合和格式不匹配的附件")
            require(any(a["route"]["role"] == "DATA_FILE" for a in attachments), "DATA_REQUIRED", "请先导入 Excel 数据")
            dependency_id = self.ensure_definition_extract(definition["id"], db)
            turn.update(preparationJobId=dependency_id, preparationDefinitionId=definition["id"])
            store.put("turn", turn, db)
            deferred = store.defer(job, dependency_id, db)
            store.event(db, current["id"], "turn.preparing", {"turnId": turn["id"], "jobId": job["id"],
                        "definitionId": definition["id"], "preparationJobId": dependency_id})
            return deferred

    def process_turn(self, turn_id, job, gateway=None):
        store = self.store
        turn = store.get("turn", turn_id)
        if turn.get("runId"):
            return turn["runId"]
        if turn["state"] == "COMPLETED":
            return None
        session = store.get("session", turn["sessionId"])
        if turn.get("preparationJobId"):
            deferred = self.prepare_analysis_definition(session, turn, job)
            if deferred:
                return deferred
        action, parameters = turn["action"], dict(turn["parameters"])
        decision = store.get("intent", turn["intentDecisionId"]) if turn.get("intentDecisionId") else None
        if decision:
            action, parameters = decision["action"], decision["parameters"]
        method = "EXPLICIT_ACTION"
        if action == "AUTO" and turn["text"].strip() in LOCAL_COMMANDS:
            action = LOCAL_COMMANDS[turn["text"].strip()]
            method = "LOCAL_COMMAND"
        if action == "AUTO":
            inferred = infer_local_action(turn["text"])
            if inferred:
                action = inferred
                method = "LOCAL_NATURAL_LANGUAGE"
        if action == "AUTO":
            gateway = gateway or Gateway(store, check_cancel=lambda: store.check_job(job["id"], job["lease"]))
            definition = store.get("definition", session["definitionId"]) if session["definitionId"] else None
            source_runs = [r for r in store.list("run") if r.get("sessionId") == session["id"] and r.get("committed") and r.get("mode") != "PLAN"]
            newest = source_runs[0] if source_runs else None
            result_catalog = []
            if newest:
                result_catalog = [{
                    "id": r["metricId"], "name": r["name"], "unit": r.get("unit", ""),
                    "groupFields": sorted({key for group in r.get("groups", []) for key in group.get("group", {})}),
                } for r in newest.get("results", [])]
            materials = material_context(store, session)
            decision = gateway.call(
                "intent",
                {
                    "question": turn["text"],
                    "metricCatalog": [{"id": c["id"], "name": c["name"]} for c in definition["candidates"] if c["id"] not in definition.get("metricExclusions", {})] if definition else [],
                    "resultCatalog": result_catalog,
                    "materialCatalog": materials["attachments"],
                    "allowedActions": ["HELP", "PLAN", "ANALYZE", "EXPLAIN", "SUMMARY", "MATERIAL_QUERY", "RESULT_QUERY", "UNSUPPORTED"],
                },
                {
                    "type": "object", "required": ["action", "parameters"], "additionalProperties": False,
                    "properties": {
                        "action": {"enum": ["HELP", "PLAN", "ANALYZE", "EXPLAIN", "SUMMARY", "MATERIAL_QUERY", "RESULT_QUERY", "UNSUPPORTED"]},
                        "parameters": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "scope": {"enum": ["ALL_DATA"]},
                                "date_start": {"type": "string"}, "date_end": {"type": "string"},
                                "group_by": {"type": "array", "items": {"type": "string"}},
                                "metricIds": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                                "groupFilters": {"type": "object", "additionalProperties": {"type": "string"}},
                                "sortBy": {"enum": ["source", "value"]},
                                "direction": {"enum": ["asc", "desc"]},
                                "offset": {"type": "integer", "minimum": 0},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                            },
                        },
                    },
                },
                turn_id,
            )
            action, proposed = decision["action"], decision["parameters"]
            # Model output cannot silently override explicit parameters or invent dates.
            require(all(k in parameters or k not in ("date_start", "date_end") or v in turn["text"] for k, v in proposed.items()), "AMBIGUOUS_TIME_WINDOW", "请明确输入起止日期（YYYY-MM-DD），或使用分析面板选择窗口")
            parameters = proposed | parameters
            method = "MODEL_PROPOSAL"
            decision = None
        # The analysis buttons and the current UI use one explicit, stable
        # scope contract. Date parameters remain accepted for old saved runs
        # and deliberate natural-language requests.
        if action in ("PLAN", "ANALYZE") and not any(key in parameters for key in ("scope", "date_start", "date_end")):
            parameters["scope"] = "ALL_DATA"
        if not turn.get("intentDecisionId"):
            with store.transaction() as db:
                current = store.get("session", session["id"], db)
                require(current["revision"] == turn["sessionRevision"], "REVISION_CONFLICT", "分类期间会话发生变化，请刷新后重试")
                lease = db.execute("SELECT cancel,lease,status FROM jobs WHERE id=?", (job["id"],)).fetchone()
                require(lease and lease[1] == job["lease"] and lease[2] == "RUNNING", "LEASE_LOST", "任务租约已失效")
                require(not lease[0], "CANCELLED", "任务已取消")
                decision = store.put("intent", {"sessionId": session["id"], "turnId": turn_id, "action": action, "parameters": parameters,
                    "method": method, "sessionRevision": turn["sessionRevision"], "sessionHash": digest(session),
                    "inputHash": digest({"text": turn["text"], "parameters": turn["parameters"]}), "attempt": job["lease"],
                    "modelCallIds": [r["id"] for r in store.list("model_call", db) if r.get("budgetId") == turn_id]}, db)
                turn["intentDecisionId"] = decision["id"]
                store.put("turn", turn, db)
        require(action != "UNSUPPORTED", "INTENT_UNSUPPORTED", "当前请求超出说明文档中的指标范围，请使用分析面板或补充说明文档后重新上传")
        store.check_job(job["id"], job["lease"])
        source, parameters, history = resolve_history(store, session, turn, action, parameters)
        contract = None
        if action in ("RECALCULATE_FROZEN", "REGENERATE", "EXPLAIN", "SUMMARY"):
            if action == "RECALCULATE_FROZEN":
                require(source["mode"] != "PLAN" and not parameters, "CONTRACT_MISMATCH", "冻结重算不能修改查询参数")
                require(source.get("runtime") == runtime_manifest(), "VERSION_MISMATCH", "历史运行的图、技能或规则结构版本不同，不能声明为同版本冻结重算")
                contract = source["contract"]
        elif action in ("PLAN", "ANALYZE"):
            deferred = self.prepare_analysis_definition(session, turn, job)
            if deferred:
                return deferred
            definition = self.require_analysis_definition(session)
            require(all(store.get("attachment", id)["status"] in ("READY", "EXCLUDED") for id in session["attachmentIds"]), "ROUTE_UNRESOLVED", "请先确认或排除全部未知、混合和格式不匹配的附件")
            require(any(store.get("attachment", id)["route"]["role"] == "DATA_FILE" for id in session["attachmentIds"]), "DATA_REQUIRED", "请先导入 Excel 数据")
            clear = [c for c in definition["candidates"] if c["status"] == "CLEAR" and c["id"] not in definition.get("metricExclusions", {})]
            if definition.get("sessionId") == session["id"] and session["state"] == "DRAFT" and not session.get("mappingRevisionId") and parameters.get("scope") == "ALL_DATA":
                from .data import document_mappings
                session["mappings"] = document_mappings(store, session, definition) | session["mappings"]
            validate_mappings(store, session, definition, session["mappings"])
            allowed = {p["name"] for c in clear for p in c["rule"]["allowedQueryParameters"]} | {"scope"}
            require(set(parameters) <= allowed, "PARAMETER_NOT_ALLOWED", "查询参数不在说明文档的规则范围中")
            for candidate in clear:
                validate_parameters(candidate["rule"], {k: v for k, v in parameters.items() if k == "scope" or k in {p["name"] for p in candidate["rule"]["allowedQueryParameters"]}})
            if definition["reviewPolicy"] == "AI_REQUIRED" and action == "ANALYZE":
                profile = (gateway or Gateway(store)).profile()
                require(profile.get("hasKey"), "MODEL_KEY_MISSING", "该版本要求在线复核，请先配置 Key")
            files = [{"attachmentId": id, "sha256": store.get("attachment", id)["sha256"], "snapshotHash": store.get("attachment", id)["snapshotHash"], "route": store.get("attachment", id)["route"]} for id in session["attachmentIds"]]
            baseline = {"definitionId": definition["id"], "definitionManifestHash": definition["manifestHash"], "files": files, "mappings": session["mappings"]}
            baseline_hash = digest(baseline)
            require(session["state"] != "LOCKED" or session["baselineHash"] == baseline_hash, "CONTRACT_MISMATCH", "冻结数据基线校验失败")
            limits = LIMITS | {"run_seconds": self.settings()["runSeconds"], "model_calls": self.settings()["modelCalls"], "model_tokens": self.settings()["modelTokens"]}
            profile_snapshot = public_profile((gateway or Gateway(store)).profile()) if definition["reviewPolicy"] == "AI_REQUIRED" and action == "ANALYZE" else None
            contract = {"schemaVersion": "computation-contract/1.0", "baselineHash": baseline_hash, "definitionId": definition["id"], "definitionManifestHash": definition["manifestHash"], "domainPack": definition["manifest"]["domainPack"], "modelProfile": profile_snapshot, "candidates": definition["candidates"], "parameters": parameters, "mappings": session["mappings"], "files": files, "engineVersion": ENGINE_VERSION, "reviewPolicy": definition["reviewPolicy"], "operatorVersion": "operator-catalog/1.0", "limits": limits}
            if definition["manifest"].get("metricExclusions"):
                contract["metricExclusions"] = definition["manifest"]["metricExclusions"]
            if definition.get("reportPlan"):
                require(parameters == {"scope": "ALL_DATA"}, "PARAMETER_NOT_ALLOWED", "说明回填按当前全部材料逐日取数，请使用全部数据分析")
                contract.update(schemaVersion="computation-contract/1.1", reportPlan=definition["manifest"]["reportPlan"],
                                reportDocumentHash=definition["documentHash"])
        explanation, query = None, None
        if action in LOCAL_ACTIONS:
            require(not parameters and not source, "INVALID_PARAMETER", "本地准备查询不接受历史运行或计算参数")
            if action == "METRIC_SPEC_PARSE":
                parsed = self.parse_session_metrics(session["id"])
                explanation = ("指标说明已解析完成，可在右侧查看解析结果。" if parsed.get("status") == "READY" else
                               "已提交指标说明解析。无需上传数据文件；可在右侧查看指标清单、公式、条件、原文依据和待确认项。")
            else:
                explanation = local_reply(store, session, action)
        elif action == "RESULT_QUERY":
            query, explanation = query_result(source, parameters)
        elif action == "FORMULA_INSPECT":
            require(not parameters, "INVALID_PARAMETER", "查看公式不接受计算参数")
            explanation = formula_reply(source)
        elif action == "MATERIAL_QUERY":
            require(not parameters, "INVALID_PARAMETER", "资料问答不接受计算参数")
            materials = material_context(store, session)
            response = (gateway or Gateway(store)).call(
                "material",
                {"question": turn["text"], "materials": materials,
                 "instruction": "只依据 materials 中的文件、指标定义、表头和行数回答。不得补充来源之外的事实，不得暴露原始明细、路径或凭据。references 必须引用 materials 中已有的文件名、定位符或表名。"},
                {"type": "object", "required": ["answer", "references"], "additionalProperties": False,
                 "properties": {"answer": {"type": "string", "minLength": 1, "maxLength": 4000},
                                "references": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}},
                turn_id,
            )
            allowed_references = material_references(materials)
            require(set(response["references"]) <= allowed_references, "MATERIAL_REFERENCE_INVALID", "资料回答引用了当前资料之外的来源")
            explanation = response["answer"]
            if response["references"]:
                explanation += "\n依据：" + "、".join(response["references"])
        if source and action in ("EXPLAIN", "SUMMARY", "REGENERATE"):
            require(source["mode"] != "PLAN", "RUN_NOT_ANALYSIS", "请选择已完成的分析结果")
            # DTO is rebuilt from approved aggregates; never forward row provenance or history.
            summaries = [{"metricId": r["metricId"], "name": r["name"], "outcome": r["outcome"], "reasonCode": r.get("reasonCode"), "values": [{"displayValue": g.get("displayValue"), "unit": r.get("unit"), "outcome": g["outcome"]} for g in r.get("groups", [])]} for r in source.get("results", [])]
            instruction = "仅解释已提供指标的口径、可用性和限制。不作医疗判断，不引入外部事实或新数值。explanation 不得包含数字，数值由应用按原运行附加。metricIds 只能选择给定指标。"
            if action == "SUMMARY":
                instruction += "用户请求的是分析汇总，请按主要发现、异常或跳过原因组织简洁摘要。"
            response = (gateway or Gateway(store)).call("explain", {"question": turn["text"], "results": summaries, "instruction": instruction}, {"type":"object","additionalProperties":False,"required":["explanation","metricIds"],"properties":{"explanation":{"type":"string","minLength":1,"maxLength":2000},"metricIds":{"type":"array","items":{"enum":[r["metricId"] for r in summaries]},"minItems":1,"uniqueItems":True}}}, turn_id)
            require(not any(ch.isdigit() for ch in response["explanation"]), "EXPLANATION_UNSUPPORTED_FACT", "解释引入了未经引用的数值，已拒绝发布")
            lines = [response["explanation"], "引用运行 " + source["id"]]
            for result in source["results"]:
                if result["metricId"] in response["metricIds"]:
                    values = [g["displayValue"] + " " + result["unit"] for g in result.get("groups", []) if g["outcome"] == "VALUE"]
                    lines.append(result["name"] + "：" + ("；".join(values) if values else "未计算 / " + result.get("reasonCode", "无可用分组")))
            explanation = "\n".join(lines)
        with store.transaction() as db:
            current = store.get("session", session["id"], db)
            require(current["revision"] == turn["sessionRevision"], "REVISION_CONFLICT", "处理期间会话发生变化，请重试")
            lease = db.execute("SELECT cancel,lease FROM jobs WHERE id=?", (job["id"],)).fetchone()
            require(lease and lease[1] == job["lease"], "LEASE_LOST", "任务租约已失效")
            require(not lease[0], "CANCELLED", "任务已取消")
            history = store.put("history_context", history, db)
            turn["historyContextId"] = history["id"]
            turn["resolvedAction"] = action
            if action != "REGENERATE":
                store.put("message", {"sessionId": session["id"], "role": "user", "text": turn["text"] or action, "turnId": turn_id}, db)
            if contract:
                if action != "RECALCULATE_FROZEN":
                    if current["mappings"] != session["mappings"]:
                        current.update(mappings=session["mappings"], mappingRevisionId=None)
                    mapping_id = current.get("mappingRevisionId")
                    mapping_revision = store.get("mapping_revision", mapping_id, db) if mapping_id else record_mapping_revision(store, current, definition, db)
                    require(mapping_revision["mappingHash"] == digest(contract["mappings"]) and mapping_revision["definitionId"] == definition["id"], "CONTRACT_MISMATCH", "映射版本与当前计算合同不符")
                    contract["mappingRevisionId"] = mapping_revision["id"]
                    contract["mappingRevisionDigest"] = mapping_revision["digest"]
                current.update(state="LOCKED", baselineHash=contract["baselineHash"], draft="")
                run = store.put("run", {"sessionId": session["id"], "turnId": turn_id, "mode": "ANALYZE" if action == "RECALCULATE_FROZEN" else action, "status": "QUEUED", "phase": "QUEUED", "contract": contract, "contractHash": digest(contract), "sourceRunId": source["id"] if source else None, "startedEpoch": time.time(), "committed": False, "jobId": job["id"]}, db)
                run["runtime"] = runtime_manifest()
                run["historySourceRunId"] = source["id"] if source else None
                if action != "RECALCULATE_FROZEN":
                    run["sourceRunId"] = None
                run["intentDecisionId"] = turn["intentDecisionId"]
                run["historyContextId"] = history["id"]
                run = store.put("run", run, db)
                turn.update(state="RUN_CREATED", runId=run["id"])
                store.event(db, session["id"], "run.created", {"runId": run["id"], "contractHash": run["contractHash"]})
            else:
                query_record = store.put("result_query", query | {"sessionId": session["id"], "turnId": turn_id}, db) if query else None
                store.put("message", {"sessionId": session["id"], "role": "assistant", "text": explanation,
                    "turnId": turn_id, "historyContextId": history["id"], "queryId": query_record["id"] if query_record else None,
                    "uiAction": "SETTINGS" if action == "SETTINGS_ACTION" else None,
                    "runId": source["id"] if source else None, "sourceRunId": source["id"] if source else None, "feedback": None}, db)
                current["draft"] = ""
                turn["state"] = "COMPLETED"
            store.put("session", current, db, expected=turn["sessionRevision"])
            store.put("turn", turn, db)
        return turn.get("runId")

    def cancel(self, job_id):
        temporary_ref = None
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            require(row is not None, "NOT_FOUND", "任务不存在")
            if row["kind"] == "turn" and self.store.get("turn", json.loads(row["payload"])["turnId"], db)["state"] == "COMPLETED":
                return {"status": "ALREADY_COMMITTED"}
            if any(r["jobId"] == job_id and r.get("committed") for r in self.store.list("run", db)):
                return {"status": "ALREADY_COMMITTED"}
            if any(a.get("jobId") == job_id for a in self.store.list("artifact", db)):
                return {"status": "ALREADY_COMMITTED"}
            if any(a.get("jobId") == job_id for a in self.store.list("comparison", db)):
                return {"status": "ALREADY_COMMITTED"}
            if row["status"] in ("QUEUED", "RUNNING"):
                db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='QUEUED' THEN 'CANCELLED' ELSE status END,updated=? WHERE id=?", (now(), job_id))
                if row["kind"] == "model_test" and row["status"] == "QUEUED":
                    payload = json.loads(row["payload"])
                    temporary_ref = payload.get("temporaryCredentialRef")
                if row["status"] == "QUEUED" and row["kind"] == "turn":
                    turn = self.store.get("turn", json.loads(row["payload"])["turnId"], db)
                    turn["state"] = "CANCELLED"
                    self.store.put("turn", turn, db)
            result = {"status": "CANCEL_REQUESTED"}
        if temporary_ref:
            try:
                (self.credentials or Credentials()).delete(temporary_ref)
            except AppError:
                # Non-Windows test hosts have no production credential backend.
                pass
        return result

    def feedback(self, message_id, helpful):
        require(helpful in (True, False, None), "INVALID_FEEDBACK", "反馈值无效")
        with self.store.transaction() as db:
            message = self.store.get("message", message_id, db)
            require(message["role"] == "assistant", "INVALID_FEEDBACK", "仅支持对回答反馈")
            message["feedback"] = helpful
            return self.store.put("message", message, db)

    def queue(self, kind, scope, payload):
        require(kind in ("extract", "model_test", "export", "diagnostics", "comparison"), "INVALID_JOB", "任务类型不受支持")
        with self.store.transaction() as db:
            if kind == "model_test":
                require(scope == "settings" and isinstance(payload, dict), "INVALID_REQUEST", "模型测试参数无效")
                if set(payload) == {"profileId"}:
                    require(isinstance(payload["profileId"], str), "INVALID_REQUEST", "模型测试参数无效")
                    self.store.get("model", payload["profileId"], db)
                else:
                    require(set(payload) == {"profile", "temporaryCredentialRef"}, "INVALID_REQUEST", "模型测试参数无效")
                    profile, reference = payload["profile"], payload["temporaryCredentialRef"]
                    require(isinstance(profile, dict) and isinstance(reference, str) and reference.startswith("credential_test_"), "INVALID_REQUEST", "模型测试参数无效")
                    allowed_profile = {"id", "name", "provider", "protocol", "baseUrl", "model", "enabled", "default", "temperature", "maxOutputTokens", "timeoutSeconds", "credentialRef", "hasKey", "temporary", "revision"}
                    require(set(profile) <= allowed_profile and profile.get("temporary") is True and profile.get("credentialRef") == reference and profile.get("hasKey") is True, "INVALID_REQUEST", "模型测试配置无效")
            if kind == "export":
                require(set(payload) == {"runId", "format"}, "INVALID_REQUEST", "导出参数无效")
                run = self.store.get("run", payload["runId"], db)
                require(run["sessionId"] == scope, "SCOPE_FORBIDDEN", "导出结果不属于当前会话")
            if kind == "extract":
                require(set(payload) == {"definitionId"} and payload["definitionId"] == scope, "INVALID_REQUEST", "提取参数无效")
                definition = self.store.get("definition", scope, db)
                definitions.require_not_deleted(definition)
                require(definition["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可重新提取")
                active = db.execute(
                    "SELECT id FROM jobs WHERE kind='extract' AND scope=? AND status IN ('QUEUED','RUNNING') ORDER BY created LIMIT 1",
                    (scope,),
                ).fetchone()
                if active:
                    return {"jobId": active[0], "deduplicated": True}
            if kind == "comparison":
                require(set(payload) == {"runIds", "kind"} and isinstance(payload["runIds"], list), "INVALID_REQUEST", "比较参数无效")
                require(all(self.store.get("run", id, db)["sessionId"] == scope for id in payload["runIds"]), "SCOPE_FORBIDDEN", "比较运行不属于当前会话")
            return {"jobId": self.store.enqueue(db, kind, scope, payload)}

    def jobs_detail(self):
        return [{k: v for k, v in j.items() if k != "payload"} for j in self.store.jobs()]

    def confirm_artifact(self, artifact_id, actor):
        from .reports import confirm_layout
        return confirm_layout(self.store, artifact_id, actor)

    def publish_bind(self, definition_id, revision, actor, reviewed, session_id=None, session_revision=None):
        # A single SQLite transaction publishes and optionally binds the reviewed version.
        return definitions.publish(self.store, definition_id, revision, actor, reviewed, session_id, session_revision)

    def dispatch(self, method, args):
        if method == "job.queue":
            if args.get("kind") == "extract":
                draft = self.managed_definition(args.get("scope"))
                require(draft.get("sourceKind") != "MANUAL", "INVALID_JOB", "手工指标无需在线提取")
        methods = {
            "bootstrap": self.bootstrap, "session.create": self.create_session, "session.list": self.session_list,
            "session.detail": self.session_detail, "session.update": self.update_session,
            "session.clear": self.clear_conversation,
            "session.metrics": self.session_metrics, "session.metrics.retry": self.retry_session_metrics,
            "session.metricCatalog": self.session_metric_catalog, "session.metrics.parse": self.parse_session_metrics,
            "attachment.route": self.route, "attachment.preview": self.preview,
            "mapping.suggest": self.suggest_mapping, "turn.submit": self.submit_user_turn,
            "run.context": self.run_context,
            "job.cancel": self.cancel, "message.feedback": self.feedback,
            "settings.save": self.save_settings, "job.queue": self.queue, "job.list": self.jobs_detail,
            "artifact.confirm": self.confirm_artifact,
            "definition.get": self.managed_definition,
            "library.metrics": lambda **kw: __import__('insight_agent.library_metrics', fromlist=['list_metrics']).list_metrics(self.store, **kw),
            "definition.metric.edit": lambda **kw: __import__('insight_agent.metric_maintenance', fromlist=['edit']).edit(self.store, **kw),
            "definition.metrics.delete": lambda **kw: __import__('insight_agent.metric_maintenance', fromlist=['delete_many']).delete_many(self.store, **kw),
            "definition.create": self.create_definition,
            "definition.clone": lambda **kw: self.manage_definition("clone", **kw),
            "definition.manual.save": lambda **kw: self.manage_definition("manual", **kw),
            "definition.save": lambda **kw: self.manage_definition("save", **kw),
            "definition.amend": lambda **kw: self.manage_definition("amend", **kw),
            "definition.metadata": lambda **kw: self.manage_definition("metadata", **kw),
            "definition.publish": lambda **kw: self.manage_definition("publish", **kw),
            "definition.delete": lambda **kw: self.manage_definition("delete", **kw),
            "definition.disable": lambda **kw: self.manage_definition("disable", **kw),
            "definition.default": lambda **kw: self.manage_definition("default", **kw),
            "definition.compare": self.compare_definitions,
            "session.delete": lambda **kw: __import__('insight_agent.maintenance', fromlist=['delete_session']).delete_session(self.store, **kw),
        }
        require(method in methods, "API_FORBIDDEN", "接口未开放")
        return methods[method](**args)

    def managed_definition(self, definition_id):
        value = self.store.get("definition", definition_id)
        definitions.require_not_deleted(value)
        require(not value.get("sessionId"), "SCOPE_FORBIDDEN", "对话内指标说明请在所属对话查看和维护")
        return value

    def create_definition(self, attachment_id, name="", parent_id=None):
        attachment = self.store.get("attachment", attachment_id)
        require(attachment["scope"] == "library", "SCOPE_FORBIDDEN", "请通过指标管理导入指标说明")
        if parent_id:
            self.managed_definition(parent_id)
        return definitions.create_draft(self.store, attachment_id, name, parent_id)

    def manage_definition(self, action, **args):
        from . import manual_metrics
        if args.get("definition_id"):
            self.managed_definition(args["definition_id"])
        functions = {"clone": lambda **kw: manual_metrics.clone(self.store, **kw),
                     "manual": lambda **kw: manual_metrics.save(self.store, **kw),
                     "save": lambda **kw: definitions.save_candidates(self.store, **kw),
                     "amend": lambda **kw: definitions.amend(self.store, **kw),
                     "metadata": lambda **kw: definitions.metadata(self.store, **kw),
                     "publish": self.publish_bind, "delete": self.delete_definition,
                     "disable": self.disable_definition, "default": self.default_definition}
        return functions[action](**args)

    def compare_definitions(self, left_id, right_id):
        return definitions.compare_versions(self.managed_definition(left_id), self.managed_definition(right_id))

    def delete_definition(self, definition_id, revision, confirmation):
        return definitions.delete(self.store, definition_id, revision, confirmation)

    def disable_definition(self, definition_id, disabled):
        with self.store.transaction() as db:
            definition = self.store.get("definition", definition_id, db)
            definitions.require_not_deleted(definition)
            require(disabled or not definition["disabled"], "IMMUTABLE_DISABLE", "停用不可撤回，请创建并发布新版本")
            definition["disabled"] = bool(disabled)
            if disabled:
                definition["default"] = False
            return self.store.put("definition", definition, db)

    def default_definition(self, definition_id):
        with self.store.transaction() as db:
            selected = self.store.get("definition", definition_id, db)
            require(selected["status"] == "PUBLISHED" and not selected["disabled"], "DEFINITION_UNAVAILABLE", "默认版本必须是允许新绑定的已发布版本")
            for definition in self.store.list("definition", db):
                if definition.get("default") or definition["id"] == definition_id:
                    definition["default"] = definition["id"] == definition_id
                    self.store.put("definition", definition, db)
            return {"definitionId": definition_id}
