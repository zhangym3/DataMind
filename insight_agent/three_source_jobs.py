"""Checkpointed ingestion and manual SQL-generation graphs."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .core import LIMITS, require
from .three_source_ingest import SKILLS, detect_source_type
from .three_source_metrics import generate_sql
from .skills import invoke


class IngestState(TypedDict, total=False):
    job_id: str
    source_id: str
    source_type: str
    row_count: int
    state: str
    revision_id: str | None


class SqlState(TypedDict, total=False):
    job_id: str
    metric_id: str
    version_id: str
    status: str


def _invoke(store, graph, job_id, initial):
    with closing(sqlite3.connect(store.home / "three-source-checkpoints.sqlite3", check_same_thread=False)) as connection:
        return graph.compile(checkpointer=SqliteSaver(connection)).invoke(
            initial, config={"configurable": {"thread_id": job_id}})


def execute_ingest_graph(store, session_id, path, job_id, gateway=None):
    def inspect(state):
        target = Path(path)
        require(target.is_file() and target.suffix.lower() == ".xlsx" and
                target.stat().st_size <= LIMITS["file_bytes"],
                "FORMAT_UNSUPPORTED", "请选择大小受限的 XLSX 文件")
        return {"source_type": detect_source_type(target)}

    def ingest(state):
        result = invoke(SKILLS[state["source_type"]], "ingest", store, session_id, path,
                        gateway, expected_source_type=state["source_type"])
        return {"source_id": result["sourceId"], "source_type": result["sourceType"],
                "row_count": result["rowCount"], "state": result["state"],
                "revision_id": result["revisionId"]}

    def audit(state):
        with store.connect() as db:
            source = db.execute("SELECT source_type,row_count,state FROM data_source WHERE id=? AND session_id=?",
                                (state["source_id"], session_id)).fetchone()
            require(source and source["source_type"] == state["source_type"] and
                    source["row_count"] == state["row_count"] and source["state"] == state["state"],
                    "INGEST_AUDIT", "入库来源核验失败")
        return {}

    graph = StateGraph(IngestState)
    for name, action in (("inspect", inspect), ("ingest", ingest), ("audit", audit)):
        graph.add_node(name, action)
    graph.add_edge(START, "inspect")
    graph.add_edge("inspect", "ingest")
    graph.add_edge("ingest", "audit")
    graph.add_edge("audit", END)
    final = _invoke(store, graph, job_id, {"job_id": job_id})
    return {"sourceId": final["source_id"], "sourceType": final["source_type"],
            "rowCount": final["row_count"], "state": final["state"],
            "revisionId": final["revision_id"]}


def execute_sql_graph(store, metric_id, expected_revision, job_id, gateway=None):
    def load(state):
        with store.connect() as db:
            version = db.execute("""SELECT id FROM manual_metric_version WHERE metric_id=?
                ORDER BY version_no DESC LIMIT 1""", (metric_id,)).fetchone()
            require(version, "METRIC_NOT_FOUND", "指标不存在")
            return {"version_id": version["id"]}

    def propose(state):
        result = generate_sql(store, metric_id, expected_revision, gateway, generation_id=job_id)
        return {"status": result["version"]["status"]}

    def audit(state):
        with store.connect() as db:
            row = db.execute("SELECT status FROM manual_metric_version WHERE id=?", (state["version_id"],)).fetchone()
            require(row and row["status"] == state["status"], "SQL_AUDIT", "指标 SQL 草稿核验失败")
        return {}

    graph = StateGraph(SqlState)
    for name, action in (("load", load), ("propose", propose), ("audit", audit)):
        graph.add_node(name, action)
    graph.add_edge(START, "load")
    graph.add_edge("load", "propose")
    graph.add_edge("propose", "audit")
    graph.add_edge("audit", END)
    final = _invoke(store, graph, job_id, {"job_id": job_id, "metric_id": metric_id})
    return {"metricId": metric_id, "status": final["status"]}
