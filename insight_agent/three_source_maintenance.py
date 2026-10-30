"""Delete one three-source conversation without touching another data space."""
from __future__ import annotations

import sqlite3
import json
from contextlib import closing

from .core import DELETE_CONFIRMATION_TEXT, require


def delete_session(store, session_id, revision, confirmation):
    require(confirmation == DELETE_CONFIRMATION_TEXT, "DELETE_CONFIRMATION", "请输入确认删除")
    with store.transaction() as db:
        session = store.get("session", session_id, db)
        require(session.get("schemaVersion") == 2 and session["revision"] == revision,
                "REVISION_CONFLICT", "只能删除版本未变化的三源对话")
        require(not db.execute("SELECT 1 FROM jobs WHERE scope=? AND status IN ('RUNNING','QUEUED')", (session_id,)).fetchone(),
                "SESSION_BUSY", "请先等待或取消当前对话任务")
        source_rows = db.execute("SELECT id,file_sha256 FROM data_source WHERE session_id=?", (session_id,)).fetchall()
        source_ids = [row["id"] for row in source_rows]
        run_ids = [row[0] for row in db.execute("SELECT id FROM objects WHERE kind='run_v2' AND json_extract(data,'$.sessionId')=?", (session_id,))]
        turn_ids = [row[0] for row in db.execute("SELECT DISTINCT json_extract(data,'$.turnId') FROM objects WHERE kind='message' AND json_extract(data,'$.sessionId')=?", (session_id,))]
        graph_ids = list(run_ids)
        for job in db.execute("SELECT id,kind,payload FROM jobs WHERE scope=?", (session_id,)):
            if job["kind"] == "import_v2":
                graph_ids.extend(f"{job['id']}:{index}" for index in range(len(json.loads(job["payload"])["paths"])))
        for source_id in source_ids:
            db.execute("""DELETE FROM cancellation_tag WHERE cancellation_row_id IN
                (SELECT r.id FROM cancellation_row r JOIN data_sheet sh ON sh.id=r.sheet_id WHERE sh.source_id=?)""", (source_id,))
            for table in ("first_surgery_row", "cancellation_row", "surgery_detail_row"):
                db.execute(f"DELETE FROM {table} WHERE sheet_id IN (SELECT id FROM data_sheet WHERE source_id=?)", (source_id,))
            db.execute("DELETE FROM data_issue WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM data_revision_source WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM data_revision WHERE session_id=?", (session_id,))
        for source_id in source_ids:
            db.execute("DELETE FROM data_sheet WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM data_source WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM data_space WHERE session_id=?", (session_id,))
        for kind in ("message", "run_v2"):
            db.execute("DELETE FROM objects WHERE kind=? AND json_extract(data,'$.sessionId')=?", (kind, session_id))
        db.execute("DELETE FROM objects WHERE kind='session' AND id=?", (session_id,))
        db.execute("DELETE FROM events WHERE scope=?", (session_id,))
        db.execute("DELETE FROM jobs WHERE scope=?", (session_id,))
        for row in db.execute("SELECT id,response FROM requests").fetchall():
            try:
                reference = json.loads(row["response"])
            except (ValueError, TypeError):
                continue
            if reference.get("runId") in run_ids or reference.get("turnId") in turn_ids:
                db.execute("DELETE FROM requests WHERE id=?", (row["id"],))
    checkpoint = store.home / "three-source-checkpoints.sqlite3"
    if checkpoint.exists():
        from langgraph.checkpoint.sqlite import SqliteSaver
        with closing(sqlite3.connect(checkpoint)) as connection:
            saver = SqliteSaver(connection)
            for graph_id in graph_ids:
                saver.delete_thread(graph_id)
    # Blob garbage collection needs a store-wide lock because other sessions
    # may concurrently reuse the same content-addressed original workbook.
    return {"deletedSessionId": session_id, "deletedSources": len(source_ids)}
