"""Durable three-source query workflow; checkpoints contain IDs and plans only."""
from __future__ import annotations

import sqlite3
import json
from contextlib import closing
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .core import AppError, digest, dumps, now, require
from .three_source_metrics import execute_metric, route_metric
from .three_source_query import basic_plan, execute_plan, validate_question_plan

KB_PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["plan"],
                  "properties": {"plan": {"type": "object"}}}


class QueryState(TypedDict, total=False):
    run_id: str
    route: dict
    plan: dict
    result_hash: str
    answer: str


def execute_query_graph(store, run_id, gateway=None):
    from .models import Gateway
    from .three_source_metrics import _catalog
    run = store.get("run_v2", run_id)
    if run.get("status") == "COMPLETED":
        return run
    gateway = gateway or Gateway(store)

    def route(state):
        run = store.get("run_v2", run_id)
        if run.get("route"):
            return {"route": run["route"]}
        with store.connect() as db:
            catalog = db.execute("""SELECT m.id,m.active_version_id FROM manual_metric m
                WHERE m.deleted_at IS NULL AND m.active_version_id IS NOT NULL ORDER BY m.id""").fetchall()
            require(digest([tuple(row) for row in catalog]) == run["metricCatalogSha256"],
                    "METRIC_CATALOG_CHANGED", "指标目录在排队期间变化，请重新提交查询")
        try:
            decision = route_metric(store, run["question"], gateway, run_id)
        except AppError as exc:
            if exc.code.startswith("MODEL_") or exc.code == "BUDGET_EXCEEDED":
                decision = route_metric(store, run["question"])
            else:
                raise
        with store.transaction() as db:
            current = store.get("run_v2", run_id, db)
            current["route"] = decision
            store.put("run_v2", current, db)
        return {"route": decision}

    def plan(state):
        run = store.get("run_v2", run_id)
        decision = state["route"]
        require(decision["decision"] in ("MATCH", "NO_MATCH"),
                decision["decision"], "指标匹配尚不确定，请明确指标名称或完成候选确认", decision)
        if decision["decision"] == "MATCH":
            return {"plan": {"metricId": decision["metricId"], "versionId": decision["versionId"]}}
        if run.get("plan"):
            return {"plan": run["plan"]}
        try:
            candidate = basic_plan(run["question"])
        except AppError as exc:
            if exc.code != "PLAN_NEEDS_MODEL":
                raise
            with store.connect() as db:
                catalog = _catalog(db, run["question"])
            response = gateway.call("kb_plan", {"question": run["question"], "catalog": catalog,
                "instruction": "只能从知识库中选择来源记录行数能力。缺少去重键、实施状态、跨表关系或日期含义时不要编造计划。"},
                KB_PLAN_SCHEMA, run_id)
            candidate = response["plan"]
        with store.transaction() as db:
            current = store.get("run_v2", run_id, db)
            current["plan"] = candidate
            store.put("run_v2", current, db)
        return {"plan": candidate}

    def execute(state):
        run = store.get("run_v2", run_id)
        plan_value = state["plan"]
        if "metricId" in plan_value:
            with store.connect() as db:
                published = db.execute("SELECT plan_json,kb_release_id,kb_content_sha256 FROM manual_metric_version WHERE id=?", (plan_value["versionId"],)).fetchone()
                require(published, "METRIC_UNPUBLISHED", "已发布指标版本不可用")
                require(published["kb_release_id"] == run["kbReleaseId"] and
                        published["kb_content_sha256"] == run["kbContentSha256"],
                        "METRIC_SQL_STALE", "指标 SQL 使用旧知识库版本，请重新生成并发布")
                validate_question_plan(run["question"], json.loads(published[0]))
            result = execute_metric(store, run["sessionId"], plan_value["metricId"], run["dataRevisionId"])
            require(result["metricVersionId"] == plan_value["versionId"], "METRIC_CHANGED", "指标版本已变化，请重新查询")
        else:
            validate_question_plan(run["question"], plan_value)
            result = execute_plan(store, run["sessionId"], plan_value, run["dataRevisionId"],
                                  run["kbReleaseId"], run["kbContentSha256"])
        return {"result_hash": store.blob(dumps(result).encode("utf-8"))}

    def commit(state):
        result = json.loads(store.read_blob(state["result_hash"]))
        rows = result["rows"]
        if rows and set(rows[0]) == {"source_row_count"}:
            answer = f"{result.get('metricName') or result['label']}：{rows[0]['source_row_count']} {result['unit']}"
        else:
            answer = f"{result.get('metricName') or result['label']}：{len(rows)} 个分组。"
        with store.transaction() as db:
            current = store.get("run_v2", run_id, db)
            if current.get("status") == "COMPLETED":
                return {"answer": current["answer"]}
            current.update(status="COMPLETED", answer=answer, result=result, route=state["route"],
                           plan=state["plan"], finishedAt=now(), resultSha256=digest(result))
            store.put("run_v2", current, db)
            store.put("message", {"role": "assistant", "sessionId": current["sessionId"],
                "turnId": current["turnId"], "runId": run_id, "text": answer}, db)
            store.event(db, current["sessionId"], "v2.query.completed", {"runId": run_id, "rowCount": len(rows)})
        return {"answer": answer}

    graph = StateGraph(QueryState)
    for name, function in (("route", route), ("plan", plan), ("execute", execute), ("commit", commit)):
        graph.add_node(name, function)
    graph.add_edge(START, "route")
    graph.add_edge("route", "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "commit")
    graph.add_edge("commit", END)
    with closing(sqlite3.connect(store.home / "three-source-checkpoints.sqlite3", check_same_thread=False)) as connection:
        graph.compile(checkpointer=SqliteSaver(connection)).invoke({"run_id": run_id},
            config={"configurable": {"thread_id": run_id}})
    return store.get("run_v2", run_id)
