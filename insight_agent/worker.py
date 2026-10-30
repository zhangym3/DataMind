"""One persistent process owns the bounded local queue. GUI never runs parsers."""
from __future__ import annotations

import time
from pathlib import Path

from .agent import execute_run
from .core import AppError, now, job_guard, require
from .definitions import create_draft
from .files import import_file
from .maintenance import diagnostics
from .models import Credentials, Gateway
from .skills import invoke
from .service import AppService
from .store import DeferredJob


def process_job(service, job, gateway=None):
    store, payload = service.store, job["payload"]
    def check():
        store.check_job(job["id"], job["lease"])
    gateway = gateway or Gateway(store, check_cancel=check)
    check()
    if job["kind"] == "import":
        accepted, failures = [], []
        accepted_details = []
        paths = payload["paths"]
        total = len(paths)
        for index, path in enumerate(paths, 1):
            check()
            scope = payload["sessionId"] or "library"
            with store.transaction() as db:
                store.event(db, scope, "import.progress", {"jobId": job["id"], "phase": "READING", "index": index, "total": total, "name": Path(path).name})
            try:
                with store.transaction() as db:
                    store.event(db, scope, "import.progress", {"jobId": job["id"], "phase": "PARSING", "index": index, "total": total, "name": Path(path).name})
                attachment = import_file(store, path, payload["sessionId"] or "library")
                with store.transaction() as db:
                    store.event(db, scope, "import.progress", {"jobId": job["id"], "phase": "ROUTING", "index": index, "total": total, "name": attachment["name"], "role": attachment["route"]["role"], "confidence": attachment["route"]["confidence"]})
                if payload["sessionId"]:
                    with store.transaction() as db:
                        session = store.get("session", payload["sessionId"], db)
                        if session["state"] != "DRAFT":
                            raise AppError("SESSION_LOCKED", "会话已锁定")
                        if attachment["route"]["role"] == "METRIC_DEFINITION":
                            has_other_metric = any(
                                item_id != attachment["id"]
                                and store.get("attachment", item_id, db)["route"]["role"] == "METRIC_DEFINITION"
                                for item_id in session["attachmentIds"]
                            )
                            if has_other_metric:
                                raise AppError("METRIC_DEFINITION_LIMIT", "一个数据对话只能有一份指标说明，请先移除当前指标说明后再导入。")
                        if attachment["id"] not in session["attachmentIds"]:
                            session["attachmentIds"].append(attachment["id"])
                            session.update(mappings={}, mappingRevisionId=None)
                            store.put("session", session, db)
                generated_definition = None
                extract_job_id = None
                if attachment["route"]["role"] == "METRIC_DEFINITION" and attachment["status"] == "READY" and attachment["profile"]["kind"] == "document":
                    existing_definition = next((d for d in store.list("definition")
                                                if d.get("attachmentId") == attachment["id"]
                                                and not d.get("deletedAt")
                                                and (not payload["sessionId"] or d.get("sessionId") == payload["sessionId"])), None)
                    generated_definition = existing_definition or create_draft(store, attachment["id"], attachment["name"], reuse_existing=True)
                    if payload["sessionId"]:
                        service.attach_session_definition(payload["sessionId"], generated_definition["id"])
                    if not payload["sessionId"] and existing_definition is None:
                        extract_job_id = service.ensure_definition_extract(generated_definition["id"])
                accepted.append(attachment["id"])
                route = attachment["route"]
                accepted_details.append({
                    "id": attachment["id"],
                    "name": attachment["name"],
                    "extension": attachment["extension"],
                    "kind": attachment["profile"]["kind"],
                    "role": route["role"],
                    "status": attachment["status"],
                    "confidence": route["confidence"],
                    "reason": route["reason"],
                    "evidenceCount": len(route.get("evidence", [])),
                    "evidencePreview": route.get("evidence", [])[:8],
                    "dataSheets": route.get("dataSheets", []),
                    "ruleSheets": route.get("ruleSheets", []),
                    "reportSheets": route.get("reportSheets", []),
                    "definitionId": generated_definition["id"] if generated_definition else None,
                    "definitionVersion": generated_definition.get("versionLabel") if generated_definition else None,
                    "extractJobId": extract_job_id,
                })
                with store.transaction() as db:
                    store.event(db, scope, "import.progress", {"jobId": job["id"], "phase": "COMPLETED", "index": index, "total": total, "name": attachment["name"], "role": route["role"], "status": attachment["status"]})
            except AppError as exc:
                name = Path(path).name
                failure = {"name": name, **exc.as_dict()}
                failures.append(failure)
                with store.transaction() as db:
                    store.event(db, scope, "import.progress", {"jobId": job["id"], "phase": "FAILED", "index": index, "total": total, "name": name, "code": exc.code, "message": exc.message})
        if payload["sessionId"] and accepted:
            # Decide after every file is routed: Word+Excel must behave the same
            # regardless of their order in the import batch. Reimports also
            # resume failed drafts instead of showing an old terminal job.
            prepared = service.prepare_uploaded_metrics(payload["sessionId"])
            definition = service.session_metrics(payload["sessionId"])
            for detail in accepted_details:
                if definition and detail["definitionId"] == definition["id"]:
                    detail["extractJobId" if prepared.get("kind") == "extract" else "catalogJobId"] = prepared.get("jobId")
        return {"accepted": accepted, "acceptedDetails": accepted_details, "failures": failures}
    if job["kind"] == "turn":
        run_id = service.process_turn(payload["turnId"], job, gateway)
        if isinstance(run_id, DeferredJob):
            return run_id
        if run_id:
            if not store.get("run", run_id).get("committed"):
                execute_run(store, run_id, job, gateway)
            return {"runId": run_id}
        return {"turnId": payload["turnId"]}
    if job["kind"] == "metric_catalog":
        parsed = invoke("surgery-word-spec", "catalog", store, payload["definitionId"], gateway, job["id"], job=job)
        return {"definitionId": parsed["definitionId"], "catalogId": parsed["id"], "metricCount": len(parsed["items"])}
    if job["kind"] == "extract":
        existing = store.get("definition", payload["definitionId"])
        if existing["status"] == "PUBLISHED" and existing.get("manifest", {}).get("extractionJobId") == job["id"]:
            return {"definitionId": existing["id"]}
        definition = invoke("surgery-word-spec", "extract", store, payload["definitionId"], gateway, job["id"])
        if definition.get("sessionId") and (definition.get("reportPlan") or any(c["status"] == "CLEAR" for c in definition["candidates"])):
            session = store.get("session", definition["sessionId"])
            from .definitions import publish
            definition = publish(store, definition["id"], definition["revision"], "文档自动解析", False, session["id"], session["revision"], extraction_job=job)
        return {"definitionId": definition["id"]}
    if job["kind"] == "model_test":
        temporary_ref = payload.get("temporaryCredentialRef")
        try:
            if "profile" in payload:
                return gateway.test(profile=payload["profile"])
            return gateway.test(payload["profileId"])
        finally:
            if temporary_ref:
                (gateway.credentials or Credentials()).delete(temporary_ref)
    if job["kind"] == "export":
        return invoke("surgery-word-report", "export", store, payload["runId"], payload["format"], service.settings()["reportTitle"], job=job)
    if job["kind"] == "diagnostics":
        return diagnostics(store)
    if job["kind"] == "comparison":
        from .comparisons import compare_runs
        return {"comparisonId": compare_runs(store, job["scope"], payload["runIds"], payload["kind"], job=job)["id"]}
    raise AppError("UNKNOWN_JOB", "未知任务类型")


def run_once(service, gateway=None):
    store = service.store
    job = store.claim()
    if not job:
        return False
    started = time.monotonic()
    seconds = service.settings()["runSeconds"]
    def guard():
        store.check_job(job["id"], job["lease"])
        require(time.monotonic() - started <= seconds, "RUN_TIMEOUT", "任务超过运行时间预算")
    token = job_guard.set(guard)
    try:
        result = process_job(service, job, gateway)
        if not isinstance(result, DeferredJob):
            store.finish(job, result=result)
    except BaseException as exc:
        error = exc.as_dict() if isinstance(exc, AppError) else {"code": "INTERNAL_ERROR", "message": "内部处理失败，已保留输入。可导出不含业务内容的诊断记录。", "details": {"exceptionType": type(exc).__name__}}
        with store.transaction() as db:
            lease = db.execute("SELECT lease,status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            for run in store.list("run", db) if lease and lease[0] == job["lease"] and lease[1] == "RUNNING" else []:
                if run["jobId"] == job["id"] and not run.get("committed"):
                    finished_at = now()
                    history = list(run.get("phaseHistory", []))
                    if history and history[-1].get("endedAt") is None:
                        started_epoch = history[-1].pop("startedEpoch", time.time())
                        history[-1]["endedAt"] = finished_at
                        history[-1]["durationMs"] = max(0, int((time.time() - float(started_epoch)) * 1000))
                    history.append({"phase": "TERMINAL", "startedAt": finished_at, "endedAt": finished_at, "durationMs": 0})
                    run.update(status="CANCELLED" if error["code"] == "CANCELLED" else "FAILED", phase="TERMINAL", phaseHistory=history, technicalFailure=error, finishedAt=finished_at)
                    store.put("run", run, db)
                    store.event(db, run["sessionId"], "run.phase", {"runId": run["id"], "phase": "TERMINAL", "startedAt": finished_at, "endedAt": finished_at, "durationMs": 0, "errorCode": error["code"], "phaseIndex": len(history) - 1})
        try:
            store.finish(job, error=error)
        except AppError:
            pass  # A fenced worker cannot publish or overwrite the current owner.
    finally:
        job_guard.reset(token)
    return True


def worker_main(home, stop_event):
    from .resources import bound_worker_memory
    _memory_job = bound_worker_memory(2 * 1024**3)
    service = AppService(home)
    while not stop_event.is_set():
        if not run_once(service):
            stop_event.wait(0.25)
