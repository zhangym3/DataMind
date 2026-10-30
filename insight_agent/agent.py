"""Fixed LangGraph workflow. Checkpoints contain IDs/hashes, never credentials/rows."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .core import AppError, ENGINE_VERSION, digest, dumps, now, require
from .engine import validate_parameters
from .domains import manifest as domain_manifest
from .models import Gateway
from .skills import invoke, runtime_manifest


class GraphState(TypedDict, total=False):
    run_id: str
    prepared_hash: str
    result_hash: str
    audit_hash: str
    completed: bool


def result_digest(results):
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in ("sources", "name")}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return digest(strip(results))


def execute_run(store, run_id, job, gateway=None):
    require(store.get("run", run_id).get("runtime") == runtime_manifest(), "VERSION_MISMATCH", "运行所用图、技能或规则结构已升级；请保留旧记录并从冻结输入新建运行")
    def check():
        store.check_job(job["id"], job["lease"])
        run = store.get("run", run_id)
        require(time.time() - run["startedEpoch"] <= run["contract"]["limits"]["run_seconds"], "RUN_TIMEOUT", "任务超过冻结的运行时间预算")

    gateway = gateway or Gateway(store, check_cancel=check)

    def phase(name):
        check()
        started_at = now()
        with store.transaction() as db:
            lease = db.execute("SELECT lease,cancel,status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            require(lease and lease[0] == job["lease"] and lease[2] == "RUNNING", "LEASE_LOST", "任务租约已失效")
            require(not lease[1], "CANCELLED", "任务已取消")
            run = store.get("run", run_id, db)
            history = list(run.get("phaseHistory", []))
            if history and history[-1].get("endedAt") is None:
                history[-1]["endedAt"] = started_at
                history[-1]["durationMs"] = max(0, int((time.time() - float(history[-1].get("startedEpoch", time.time()))) * 1000))
                history[-1].pop("startedEpoch", None)
            entry = {"phase": name, "startedAt": started_at, "endedAt": None, "durationMs": None, "startedEpoch": time.time()}
            history.append(entry)
            run.update(status="RUNNING", phase=name)
            run["phaseHistory"] = history
            store.put("run", run, db)
            store.event(db, run["sessionId"], "run.phase", {"runId": run_id, "phase": name, "startedAt": started_at, "phaseIndex": len(history) - 1})
        return run

    def definition_metadata(candidate, order):
        rule = candidate.get("rule") or {}
        return {
            "definitionOrder": order,
            "metricId": candidate.get("id") or rule.get("metricId"),
            "name": candidate.get("name") or rule.get("name"),
            "unit": rule.get("unit"),
            "entityType": rule.get("entityType"),
            "countBasis": rule.get("countBasis"),
            "timeWindow": rule.get("timeWindow"),
            "requiredFields": rule.get("requiredFields", []),
            "filters": rule.get("filters", []),
            "exclusions": rule.get("exclusions", []),
            "population": rule.get("population"),
            "expression": rule.get("expression"),
            "groupBy": rule.get("groupBy", []),
            "precision": rule.get("precision"),
            "missingPolicy": rule.get("missingPolicy"),
            "invalidPolicy": rule.get("invalidPolicy"),
            "emptySetPolicy": rule.get("emptySetPolicy"),
            "zeroDenominatorPolicy": rule.get("zeroDenominatorPolicy"),
            "sourceEvidence": rule.get("sourceEvidence", []),
            "ruleDigest": digest(rule) if rule else None,
        }

    def prepare(state):
        run = phase("PREPARING")
        contract = run["contract"]
        require(contract["engineVersion"] == ENGINE_VERSION, "VERSION_MISMATCH", "冻结合同引擎版本不匹配")
        require(contract["domainPack"] == domain_manifest(contract["domainPack"]["id"]), "VERSION_MISMATCH", "冻结领域包与当前实现不匹配")
        session = store.get("session", run["sessionId"])
        require(session["baselineHash"] == contract["baselineHash"], "CONTRACT_MISMATCH", "会话冻结基线发生变化")
        if contract.get("reportPlan"):
            sources = invoke("surgery-excel-ingest", "prepare_fill", store, contract)
            return {"prepared_hash": store.blob(dumps({"fillSources": sources}).encode())}
        items = []
        for order, candidate in enumerate(contract["candidates"], 1):
            check()
            metadata = definition_metadata(candidate, order)
            if candidate["id"] in contract.get("metricExclusions", {}):
                items.append({"metricId": candidate["id"], "name": candidate["name"], "definitionOrder": order, "definition": metadata,
                              "outcome": "SKIP", "reasonCode": "METRIC_DELETED", "reason": "指标已删除，不参与计算"})
                continue
            if candidate["status"] == "SKIP":
                items.append({"metricId": candidate["id"], "name": candidate["name"], "definitionOrder": order, "definition": metadata, "outcome": "SKIP", "reasonCode": candidate["reason"]})
                continue
            rule = candidate["rule"]
            try:
                params = {k: v for k, v in contract["parameters"].items() if k == "scope" or k in {p["name"] for p in rule["allowedQueryParameters"]}}
                validate_parameters(rule, params)
                rows, provenance = invoke("surgery-excel-ingest", "materialize", store, session, rule, contract["mappings"].get(candidate["id"]), params, metadata_only=run["mode"] == "PLAN")
                row_hash = store.blob(dumps(rows).encode()) if run["mode"] != "PLAN" else None
                items.append({"metricId": candidate["id"], "rule": rule, "definitionOrder": order, "definition": metadata, "rowsHash": row_hash, "provenance": provenance, "parameters": params})
            except AppError as exc:
                if exc.code not in {"MAPPING_UNRESOLVED", "UNSUPPORTED_PREPARATION", "COVERAGE_UNCONFIRMED", "DATA_INSUFFICIENT", "DATA_CONFLICT", "ROUTE_UNRESOLVED", "FORMAT_UNSUPPORTED", "UNSUPPORTED_CARDINALITY"}:
                    raise
                items.append({"metricId": candidate["id"], "name": candidate["name"], "definitionOrder": order, "definition": metadata, "outcome": "SKIP", "reasonCode": exc.code})
        return {"prepared_hash": store.blob(dumps(items).encode())}

    def compute(state):
        run = phase("CALCULATING" if store.get("run", run_id)["mode"] != "PLAN" else "PLANNING")
        items = json.loads(store.read_blob(state["prepared_hash"]))
        if run["contract"].get("reportPlan"):
            results = []
            for order, slot in enumerate(run["contract"]["reportPlan"]["slots"], 1):
                check()
                if slot["id"] in run["contract"].get("metricExclusions", {}):
                    result = {"metricId": slot["id"], "name": slot["plan"]["name"], "outcome": "SKIP", "reasonCode": "METRIC_DELETED", "reason": "指标已删除，不参与计算"}
                elif run["mode"] == "PLAN":
                    result = {"metricId": slot["id"], "name": slot["plan"]["name"], "outcome": "PLANNED"}
                else:
                    result = invoke("surgery-metric-calc", "calculate_fill", slot, items["fillSources"], run["contract"])
                result["definitionOrder"] = order
                results.append(result)
                with store.transaction() as db:
                    store.event(db, run["sessionId"], "run.item", {"runId": run_id, "completed": order,
                        "total": len(run["contract"]["reportPlan"]["slots"]), "name": result["name"],
                        "outcome": result["outcome"], "reason": result.get("reason")})
            return {"result_hash": store.blob(dumps(results).encode())}
        results = []
        for item in items:
            check()
            if "outcome" in item:
                results.append(item)
            elif run["mode"] == "PLAN":
                results.append({"metricId": item["metricId"], "name": item["rule"]["name"], "definitionOrder": item["definitionOrder"], "definition": item["definition"], "outcome": "PLANNED", "expression": item["rule"]["expression"], "provenance": item["provenance"], "parameters": item["parameters"]})
            else:
                result = invoke("surgery-metric-calc", "calculate", item["rule"], json.loads(store.read_blob(item["rowsHash"])), item["parameters"])
                result["definitionOrder"] = item["definitionOrder"]
                result["definition"] = item["definition"]
                result["provenance"] = item["provenance"]
                result["evidence"] = item["rule"]["sourceEvidence"]
                results.append(result)
        return {"result_hash": store.blob(dumps(results).encode())}

    def audit(state):
        run = phase("AUDITING")
        results = json.loads(store.read_blob(state["result_hash"]))
        contract = run["contract"]
        hash = result_digest(results)
        review = {"policy": contract["reviewPolicy"], "status": "PASS", "resultHash": hash, "contractHash": run["contractHash"], "engineVersion": ENGINE_VERSION}
        if run["mode"] != "PLAN" and any(r["outcome"] == "VALUE" for r in results) and contract["reviewPolicy"] == "AI_REQUIRED":
            if run.get("sourceRunId"):
                source = store.get("run", run["sourceRunId"])
                require(source.get("review", {}).get("status") == "PASS" and source.get("resultDigest") == hash and source["contractHash"] == run["contractHash"], "REVIEW_REUSE_FORBIDDEN", "离线重算不能复用不匹配的复核")
                review["mode"] = "REUSED_PRIOR_REVIEW"
            else:
                sanitized = [{"metricId": r["metricId"], "outcome": r["outcome"], "audit": r.get("audit"), "groups": [{k: v for k, v in g.items() if k not in ("sources", "group")} for g in r.get("groups", [])]} for r in results]
                if isinstance(gateway, Gateway):
                    gateway.frozen_profile = contract["modelProfile"]
                    gateway.frozen_limits = contract["limits"]
                response = gateway.call("review", {"instruction": "核对合同规则和确定性结果的逻辑一致性；不得修改数值；无法核对则 FAIL。", "rules": [c["rule"] for c in contract["candidates"] if c["status"] == "CLEAR" and c["id"] not in contract.get("metricExclusions", {})], "results": sanitized}, {"type": "object", "required": ["status", "reason"], "additionalProperties": False, "properties": {"status": {"enum": ["PASS", "FAIL"]}, "reason": {"type": "string", "maxLength": 2000}}}, run_id)
                review.update(response)
                require(response["status"] == "PASS", "AI_REVIEW_FAILED", "在线模型复核未通过，本次结果未发布")
        if run.get("sourceRunId"):
            source = store.get("run", run["sourceRunId"])
            require(source["contractHash"] == run["contractHash"] and source.get("resultDigest") == hash, "RECALCULATION_MISMATCH", "离线重算结果与原合同不一致")
        return {"audit_hash": store.blob(dumps(review).encode())}

    def commit(state):
        phase("COMMITTING")
        results = json.loads(store.read_blob(state["result_hash"]))
        review = json.loads(store.read_blob(state["audit_hash"]))
        report = None
        current_run = store.get("run", run_id)
        if current_run["contract"].get("reportPlan") and current_run["mode"] != "PLAN":
            report = invoke("surgery-word-report", "render_fill", current_run["contract"]["reportPlan"], results)
        with store.transaction() as db:
            lease = db.execute("SELECT cancel,lease,status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            require(lease and lease[1] == job["lease"] and lease[2] == "RUNNING", "LEASE_LOST", "任务租约已失效")
            require(not lease[0], "CANCELLED", "任务已取消，未发布部分结果")
            run = store.get("run", run_id, db)
            if run.get("committed"):
                return {"completed": True}
            history = list(run.get("phaseHistory", []))
            completed_at = now()
            if history and history[-1].get("endedAt") is None:
                history[-1]["endedAt"] = completed_at
                history[-1]["durationMs"] = max(0, int((time.time() - float(history[-1].get("startedEpoch", time.time()))) * 1000))
                history[-1].pop("startedEpoch", None)
            history.append({"phase": "COMPLETED", "startedAt": completed_at, "endedAt": completed_at, "durationMs": 0})
            successes = sum(r["outcome"] in ("VALUE", "PLANNED") for r in results)
            skips = sum(r["outcome"] == "SKIP" or any(g["outcome"] == "SKIP" for g in r.get("groups", [])) for r in results)
            status = "SUCCEEDED_WITH_SKIPS" if successes and skips else "SUCCEEDED" if successes else "NO_ELIGIBLE_METRICS" if run["contract"].get("reportPlan") or any(c["status"] == "CLEAR" for c in run["contract"]["candidates"]) else "NO_VALID_DEFINITION"
            run.update(status=status, phase="COMPLETED", phaseHistory=history, results=results, resultDigest=review["resultHash"], review=review, committed=True, finishedAt=completed_at)
            if report is not None:
                run["filledReport"] = report
            store.put("run", run, db)
            summary = f"已按指标说明核验并回填：{successes} 处有数值，{skips} 处待补充。具体原因已保留在正文和缺口清单中。" if report else f"{'计划' if run['mode'] == 'PLAN' else '分析'}已完成：{successes} 项通过，{skips} 项存在跳过。"
            message = store.put("message", {"sessionId": run["sessionId"], "role": "assistant", "text": summary, "runId": run_id, "feedback": None}, db)
            store.event(db, run["sessionId"], "run.phase", {"runId": run_id, "phase": "COMPLETED", "startedAt": completed_at, "endedAt": completed_at, "durationMs": 0, "phaseIndex": len(history) - 1})
            store.event(db, run["sessionId"], "run.committed", {"runId": run_id, "messageId": message["id"], "status": status})
        return {"completed": True}

    graph = StateGraph(GraphState)
    for name, fn in (("prepare", prepare), ("compute", compute), ("audit", audit), ("commit", commit)):
        graph.add_node(name, fn)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "compute")
    graph.add_edge("compute", "audit")
    graph.add_edge("audit", "commit")
    graph.add_edge("commit", END)
    with closing(sqlite3.connect(store.home / "checkpoints.sqlite3", check_same_thread=False)) as conn:
        compiled = graph.compile(checkpointer=SqliteSaver(conn))
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 2048}
        snapshot = compiled.get_state(config)
        compiled.invoke(None if snapshot.next else {"run_id": run_id}, config=config)
    return store.get("run", run_id)
