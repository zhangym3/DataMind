"""Durable local store; serialized writes and immutable content-addressed blobs."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .core import AppError, default_home, dumps, now, require, sha, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects(
 kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
 data TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,type TEXT NOT NULL,
 data TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(
 id TEXT PRIMARY KEY,hash TEXT NOT NULL,response TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL,
 payload TEXT NOT NULL,result TEXT,error TEXT,lease INTEGER NOT NULL DEFAULT 0,
 cancel INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created);
CREATE INDEX IF NOT EXISTS events_scope ON events(scope,seq);
"""


@dataclass(frozen=True)
class DeferredJob:
    dependency_id: str


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, home=None):
        self.home = Path(home or default_home()).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        for part in ("blobs", "exports", "tmp"):
            (self.home / part).mkdir(exist_ok=True)
        self.path = self.home / "datamind.sqlite3"
        with self.connect() as db:
            db.executescript(SCHEMA)
            version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            require(not version or version[0] == "1", "SCHEMA_VERSION", "数据目录来自不兼容版本，请先备份并使用对应应用")
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema','1')")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, kind, id, db=None):
        if db is None:
            with self.connect() as conn:
                return self.get(kind, id, conn)
        row = db.execute("SELECT * FROM objects WHERE kind=? AND id=?", (kind, id)).fetchone()
        require(row is not None, "NOT_FOUND", "记录不存在")
        data = json.loads(row["data"])
        data.update(id=id, revision=row["revision"], created=row["created"], updated=row["updated"])
        return data

    def list(self, kind, db=None):
        if db is None:
            with self.connect() as conn:
                return self.list(kind, conn)
        return [self.get(kind, row[0], db) for row in db.execute("SELECT id FROM objects WHERE kind=? ORDER BY created DESC,rowid DESC", (kind,))]

    def put(self, kind, value, db, expected=None):
        value = dict(value)
        id = value.pop("id", None) or uid(kind + "_")
        for key in ("revision", "created", "updated"):
            value.pop(key, None)
        row = db.execute("SELECT revision FROM objects WHERE kind=? AND id=?", (kind, id)).fetchone()
        if expected is not None:
            require(row is not None and row[0] == expected, "REVISION_CONFLICT", "内容已变化，请刷新后重试")
        if row:
            db.execute("UPDATE objects SET data=?,revision=revision+1,updated=? WHERE kind=? AND id=?", (dumps(value), now(), kind, id))
        else:
            db.execute("INSERT INTO objects VALUES(?,?,1,?,?,?)", (kind, id, dumps(value), now(), now()))
        return self.get(kind, id, db)

    def event(self, db, scope, type, data):
        db.execute("INSERT INTO events(scope,type,data,created) VALUES(?,?,?,?)", (scope, type, dumps(data), now()))

    def blob(self, data: bytes):
        hash = sha(data)
        path = self.home / "blobs" / hash
        if not path.exists():
            temp = self.home / "tmp" / uid()
            with temp.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        return hash

    def read_blob(self, hash):
        require(len(hash) == 64 and all(x in "0123456789abcdef" for x in hash), "INVALID_BLOB", "文件标识无效")
        path = self.home / "blobs" / hash
        require(path.is_file(), "BLOB_MISSING", "托管文件缺失，请从备份恢复")
        data = path.read_bytes()
        require(sha(data) == hash, "BLOB_CORRUPT", "托管文件完整性校验失败")
        return data

    def enqueue(self, db, kind, scope, payload):
        id = uid("job_")
        db.execute("INSERT INTO jobs(id,kind,scope,status,payload,created,updated) VALUES(?,?,?,'QUEUED',?,?,?)", (id, kind, scope, dumps(payload), now(), now()))
        self.event(db, scope, "job.queued", {"jobId": id, "kind": kind})
        return id

    def jobs(self):
        with self.connect() as db:
            return [dict(r) | {"payload": json.loads(r["payload"]), "result": json.loads(r["result"]) if r["result"] else None, "error": json.loads(r["error"]) if r["error"] else None} for r in db.execute("SELECT * FROM jobs ORDER BY created DESC,rowid DESC LIMIT 200")]

    def claim(self):
        with self.transaction() as db:
            row = db.execute("""SELECT queued.* FROM jobs AS queued WHERE queued.status='QUEUED'
                AND (queued.cancel=1 OR NOT EXISTS (
                    SELECT 1 FROM jobs AS dependency
                    WHERE dependency.id=json_extract(queued.payload,'$.waitForJobId')
                    AND dependency.status IN ('QUEUED','RUNNING')))
                ORDER BY CASE WHEN queued.kind='import' THEN 0 ELSE 1 END,queued.created,queued.rowid LIMIT 1""").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='RUNNING',lease=lease+1,updated=? WHERE id=?", (now(), row["id"]))
            return dict(row) | {"payload": json.loads(row["payload"]), "lease": row["lease"] + 1}

    def defer(self, job, dependency_id, db):
        """Release the worker while keeping a turn pending on a durable job."""
        row = db.execute("SELECT cancel,lease,status,payload FROM jobs WHERE id=?", (job["id"],)).fetchone()
        require(row and row["lease"] == job["lease"] and row["status"] == "RUNNING", "LEASE_LOST", "任务租约已失效")
        require(not row["cancel"], "CANCELLED", "任务已取消")
        require(job["kind"] == "turn" and dependency_id != job["id"]
                and db.execute("SELECT 1 FROM jobs WHERE id=? AND kind='extract'", (dependency_id,)).fetchone(),
                "INVALID_DEPENDENCY", "指标准备任务不存在")
        payload = json.loads(row["payload"]) | {"waitForJobId": dependency_id}
        db.execute("UPDATE jobs SET status='QUEUED',payload=?,updated=? WHERE id=?", (dumps(payload), now(), job["id"]))
        return DeferredJob(dependency_id)

    def check_job(self, id, lease):
        with self.connect() as db:
            row = db.execute("SELECT cancel,lease,status FROM jobs WHERE id=?", (id,)).fetchone()
        require(row and row[1] == lease and row[2] == "RUNNING", "LEASE_LOST", "任务已由其他进程接管")
        require(not row[0], "CANCELLED", "任务已取消")

    def finish(self, job, result=None, error=None):
        with self.transaction() as db:
            row = db.execute("SELECT cancel,lease,status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            require(row and row[1] == job["lease"] and row[2] == "RUNNING", "LEASE_LOST", "任务租约已失效")
            turn = self.get("turn", job["payload"]["turnId"], db) if job["kind"] == "turn" else None
            committed = turn and (turn["state"] == "COMPLETED" or turn.get("runId") and self.get("run", turn["runId"], db).get("committed"))
            if committed:
                error = None
            elif row[0]:
                error = AppError("CANCELLED", "任务已取消").as_dict()
            status = "CANCELLED" if error and error["code"] == "CANCELLED" else "FAILED" if error else "SUCCEEDED"
            if turn:
                turn.update(state="COMPLETED" if committed or not error else "CANCELLED" if status == "CANCELLED" else "REJECTED", error=error)
                self.put("turn", turn, db)
            db.execute("UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?", (status, dumps(result) if not error else None, dumps(error) if error else None, now(), job["id"]))
            self.event(db, job["scope"], "job.terminal", {"jobId": job["id"], "status": status})

    def recover(self, auto_resume=True):
        # Called only by the single instance owner after acquiring the process lock.
        with self.transaction() as db:
            for job in db.execute("SELECT * FROM jobs WHERE status IN ('RUNNING','QUEUED')").fetchall():
                turn = self.get("turn", json.loads(job["payload"])["turnId"], db) if job["kind"] == "turn" else None
                committed = bool(turn and (turn["state"] == "COMPLETED" or turn.get("runId") and self.get("run", turn["runId"], db).get("committed"))) or any(a.get("jobId") == job["id"] for kind in ("artifact", "comparison") for a in self.list(kind, db))
                if committed:
                    db.execute("UPDATE jobs SET status='SUCCEEDED',cancel=0,lease=lease+1,updated=? WHERE id=?", (now(), job["id"]))
                    if turn:
                        turn["state"] = "COMPLETED"
                        self.put("turn", turn, db)
            db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 OR ?=0 THEN 'CANCELLED' ELSE 'QUEUED' END,lease=lease+1,updated=? WHERE status='RUNNING'", (int(auto_resume), now()))
            if not auto_resume:
                db.execute("UPDATE jobs SET status='CANCELLED',cancel=1,lease=lease+1,updated=? WHERE status='QUEUED'", (now(),))
            for run in self.list("run", db):
                job = db.execute("SELECT status FROM jobs WHERE id=?", (run["jobId"],)).fetchone()
                if not run.get("committed") and job and job[0] == "CANCELLED":
                    run.update(status="CANCELLED", phase="TERMINAL", finishedAt=now())
                    self.put("run", run, db)
                elif run["status"] == "RUNNING":
                    run["status"] = "QUEUED"
                    self.put("run", run, db)
            for job in db.execute("SELECT payload FROM jobs WHERE kind='turn' AND status='CANCELLED'").fetchall():
                turn = self.get("turn", json.loads(job[0])["turnId"], db)
                if turn["state"] not in ("COMPLETED", "CANCELLED", "REJECTED"):
                    turn["state"] = "CANCELLED"
                    self.put("turn", turn, db)

    def expire(self, job_id, lease):
        """Fence a killed worker. The desktop must stop that process first."""
        with self.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "RUNNING" or job["lease"] != lease:
                return
            turn = self.get("turn", json.loads(job["payload"])["turnId"], db) if job["kind"] == "turn" else None
            committed = bool(turn and turn["state"] == "COMPLETED") or any(r.get("jobId") == job_id and r.get("committed") for r in self.list("run", db)) or any(a.get("jobId") == job_id for kind in ("artifact", "comparison") for a in self.list(kind, db))
            error = None if committed else AppError("CANCELLED" if job["cancel"] else "RUN_TIMEOUT", "后台任务已停止；未提交结果未发布").as_dict()
            status = "SUCCEEDED" if committed else "CANCELLED" if job["cancel"] else "FAILED"
            db.execute("UPDATE jobs SET status=?,error=?,lease=lease+1,updated=? WHERE id=?", (status, dumps(error) if error else None, now(), job_id))
            if turn:
                turn.update(state="COMPLETED" if committed else "CANCELLED" if job["cancel"] else "REJECTED", error=error)
                self.put("turn", turn, db)
            for run in self.list("run", db):
                if run.get("jobId") == job_id and not run.get("committed"):
                    run.update(status=status, phase="TERMINAL", technicalFailure=error, finishedAt=now())
                    self.put("run", run, db)
            self.event(db, job["scope"], "job.terminal", {"jobId": job_id, "status": status})
