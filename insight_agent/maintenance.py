"""Consistent encrypted backups and verified, isolated restore."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from contextlib import closing

from .core import DELETE_CONFIRMATION_TEXT, VERSION, dumps, now, require, safe_child, sha, uid
from .store import Store


def backup(store, destination, password):
    import py7zr
    require(len(password) >= 12, "WEAK_BACKUP_PASSWORD", "备份密码至少 12 个字符，请妥善保存")
    require(not any(j["status"] in ("RUNNING", "QUEUED") for j in store.jobs()), "MAINTENANCE_BUSY", "请先完成或取消队列任务后再备份")
    destination = Path(destination).resolve()
    require(not destination.exists(), "FILE_EXISTS", "备份目标已存在，请选择新名称")
    require(not destination.is_relative_to(store.home), "BACKUP_LOCATION", "备份应保存到工作空间之外")
    with tempfile.TemporaryDirectory(prefix="datamind-backup-") as folder:
        staging = Path(folder)
        with store.connect() as source, closing(sqlite3.connect(staging / "datamind.sqlite3")) as target:
            source.backup(target)
            # OS credentials are intentionally absent. Profiles become unconfigured.
            for row in target.execute("SELECT id,data FROM objects WHERE kind='model'").fetchall():
                value = json.loads(row[1])
                value.update(hasKey=False, check=None)
                value["credentialRef"] = uid("credential_")
                target.execute("UPDATE objects SET data=? WHERE kind='model' AND id=?", (dumps(value), row[0]))
            target.execute("DELETE FROM objects WHERE kind='budget'")
            target.execute("UPDATE jobs SET status='CANCELLED',payload='{}' WHERE status IN ('QUEUED','RUNNING')")
            target.commit()
        shutil.copytree(store.home / "blobs", staging / "blobs")
        for name in ("checkpoints.sqlite3", "three-source-checkpoints.sqlite3"):
            checkpoint = store.home / name
            if checkpoint.exists():
                with closing(sqlite3.connect(checkpoint)) as source, closing(sqlite3.connect(staging / checkpoint.name)) as target:
                    source.backup(target)
        manifest = {"schemaVersion": 2, "appVersion": VERSION, "created": now(), "files": {str(p.relative_to(staging)).replace("\\", "/"): sha(p.read_bytes()) for p in staging.rglob("*") if p.is_file()}, "keysIncluded": False}
        (staging / "backup-manifest.json").write_text(dumps(manifest), encoding="utf-8")
        temp = destination.with_name(destination.name + ".tmp")
        try:
            with py7zr.SevenZipFile(temp, "w", password=password, header_encryption=True) as archive:
                archive.writeall(staging, arcname="workspace")
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return {"path": str(destination), "files": len(manifest["files"]), "keysIncluded": False}


def verify_restore(archive_path, password, destination):
    import py7zr
    destination = Path(destination).resolve()
    require(not destination.exists() or not any(destination.iterdir()), "RESTORE_TARGET_NOT_EMPTY", "恢复必须选择新的空目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="datamind-restore-", dir=destination.parent) as folder:
        staging = Path(folder)
        try:
            with py7zr.SevenZipFile(archive_path, "r", password=password) as archive:
                members = archive.list()
                require(len(members) <= 100000 and sum(m.uncompressed or 0 for m in members) <= 10 * 1024**3, "BACKUP_LIMIT", "备份超过恢复资源上限")
                for member in members:
                    safe_child(staging, member.filename)
                    require(member.filename.startswith("workspace/" ) or member.filename == "workspace", "BACKUP_INVALID", "备份目录结构无效")
                archive.extractall(staging)
        except Exception as exc:
            from .core import AppError
            if isinstance(exc, AppError):
                raise
            raise AppError("BACKUP_DECRYPT_FAILED", "密码错误或备份损坏") from None
        restored = staging / "workspace"
        manifest_path = restored / "backup-manifest.json"
        require(manifest_path.is_file(), "BACKUP_INVALID", "备份清单缺失")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        require(manifest["schemaVersion"] in (1, 2) and manifest["keysIncluded"] is False, "BACKUP_VERSION", "备份版本不兼容")
        actual = {str(p.relative_to(restored)).replace("\\", "/") for p in restored.rglob("*") if p.is_file()} - {"backup-manifest.json"}
        require(actual == set(manifest["files"]), "BACKUP_INVALID", "备份文件清单不一致")
        for name, hash in manifest["files"].items():
            require(sha(safe_child(restored, name).read_bytes()) == hash, "BACKUP_CORRUPT", "备份文件哈希不匹配")
        for name in ("datamind.sqlite3", "checkpoints.sqlite3", "three-source-checkpoints.sqlite3"):
            if (restored / name).exists():
                with closing(sqlite3.connect(restored / name)) as db:
                    require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "BACKUP_CORRUPT", "备份数据库完整性检查失败")
        for blob in (restored / "blobs").iterdir():
            require(blob.is_file() and blob.name == sha(blob.read_bytes()), "BACKUP_CORRUPT", "内容寻址文件校验失败")
        verify_references(restored)
        # Rename is the commit point. Existing workspaces are never overwritten.
        if destination.exists():
            destination.rmdir()  # Already verified empty; never recursive removal.
        os.replace(restored, destination)
    Store(destination)
    return {"path": str(destination), "status": "VERIFIED", "message": "恢复完成；启动时选择恢复目录。在线模型 Key 需重新填写。"}


def diagnostics(store):
    import platform
    from datetime import datetime
    data = {"appVersion": VERSION, "python": platform.python_version(), "platform": platform.system() + " " + platform.release(), "created": now(), "objectCounts": {}, "jobs": [{"jobId": j["id"], "kind": j["kind"], "status": j["status"], "created": j["created"], "updated": j["updated"], "lease": j["lease"], "errorCode": j["error"]["code"] if j.get("error") else None} for j in store.jobs()], "runs": []}
    with store.connect() as db:
        data["objectCounts"] = {r[0]: r[1] for r in db.execute("SELECT kind,count(*) FROM objects GROUP BY kind")}
        data["databaseIntegrity"] = db.execute("PRAGMA quick_check").fetchone()[0]
        data["requestIdHashes"] = [sha(r[0].encode()) for r in db.execute("SELECT id FROM requests ORDER BY created DESC LIMIT 200")]
        events = [(json.loads(r[0]), r[1]) for r in db.execute("SELECT data,created FROM events WHERE type IN ('run.phase','run.committed') ORDER BY seq")]
        for run in store.list("run", db)[:200]:
            sequence = [(e.get("phase", "COMPLETED"), timestamp) for e, timestamp in events if e.get("runId") == run["id"]]
            stages = [{"phase": phase, "startedAt": timestamp, "secondsToNextEvent": max(0, (datetime.fromisoformat(sequence[i+1][1])-datetime.fromisoformat(timestamp)).total_seconds()) if i+1 < len(sequence) else None} for i, (phase, timestamp) in enumerate(sequence)]
            data["runs"].append({"runId": run["id"], "jobId": run["jobId"], "status": run["status"], "stages": stages})
    file_name = uid("diagnostics_") + ".json"
    (store.home / "exports" / file_name).write_text(dumps(data), encoding="utf-8")
    return {"fileName": file_name, "data": data}


def migrate(store, destination):
    """Copy and verify first. The original workspace is the rollback copy."""
    require(not any(j["status"] in ("RUNNING", "QUEUED") for j in store.jobs()), "MAINTENANCE_BUSY", "请先完成或取消队列任务")
    destination = Path(destination).resolve()
    require(not destination.is_relative_to(store.home) and not store.home.is_relative_to(destination), "MIGRATION_TARGET", "迁移目录不能包含原目录或位于原目录内")
    require(not destination.exists() or not any(destination.iterdir()), "RESTORE_TARGET_NOT_EMPTY", "请选择新的空目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="datamind-migration-", dir=destination.parent) as folder:
        target = Path(folder) / "workspace"
        target.mkdir()
        for name in ("datamind.sqlite3", "checkpoints.sqlite3"):
            source_path = store.home / name
            if source_path.exists():
                with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(target / name)) as db:
                    source.backup(db)
                    require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "MIGRATION_CORRUPT", "迁移数据库校验失败")
        shutil.copytree(store.home / "blobs", target / "blobs")
        for source in (store.home / "blobs").iterdir():
            require(source.name == sha((target / "blobs" / source.name).read_bytes()), "MIGRATION_CORRUPT", "迁移文件校验失败")
        Store(target)
        verify_references(target)
        if destination.exists():
            destination.rmdir()
        os.replace(target, destination)
    return {"path": str(destination), "rollbackPath": str(store.home), "status": "VERIFIED"}


def cleanup_preview(store, retention_days):
    import re
    from datetime import datetime, timezone, timedelta
    require(not any(j["status"] in ("RUNNING", "QUEUED") for j in store.jobs()), "MAINTENANCE_BUSY", "请先完成或取消队列任务")
    require(isinstance(retention_days, int) and 1 <= retention_days <= 3650, "INVALID_SETTING", "保留天数无效")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    sessions = [{"id": s["id"], "name": s["name"], "revision": s["revision"]} for s in store.list("session") if s["archived"] and s["updated"] < cutoff]
    referenced = set()
    # Checkpoint and SQLite pages are scanned conservatively, including retained
    # intermediate hashes. This can retain extra blobs but cannot delete live data.
    for name in ("datamind.sqlite3", "datamind.sqlite3-wal", "checkpoints.sqlite3", "checkpoints.sqlite3-wal"):
        path = store.home / name
        if path.exists():
            referenced.update(x.decode() for x in re.findall(rb"[a-f0-9]{64}", path.read_bytes()))
    # Prepared/results blobs themselves reference other blobs. Traverse every
    # reachable content file before deciding that anything can be removed.
    pending = list(referenced)
    visited = set()
    while pending:
        hash = pending.pop()
        if hash in visited:
            continue
        visited.add(hash)
        path = store.home / "blobs" / hash
        if path.is_file():
            children = {x.decode() for x in re.findall(rb"[a-f0-9]{64}", path.read_bytes())}
            referenced.update(children)
            pending.extend(children - visited)
    blobs = sorted([{"sha256": p.name, "bytes": p.stat().st_size} for p in (store.home / "blobs").iterdir() if p.name not in referenced], key=lambda b: b["sha256"])
    from .core import digest
    return {"sessions": sessions, "unreferencedBlobs": blobs, "bytes": sum(b["bytes"] for b in blobs), "previewHash": digest(blobs)}


def cleanup_blobs(store, retention_days, preview_hash):
    preview = cleanup_preview(store, retention_days)
    require(preview["previewHash"] == preview_hash, "REVISION_CONFLICT", "清理范围已变化，请重新预览")
    for blob in preview["unreferencedBlobs"]:
        safe_child(store.home / "blobs", blob["sha256"]).unlink()
    return {"deletedBlobs": len(preview["unreferencedBlobs"]), "bytes": preview["bytes"]}


def delete_session(store, session_id, revision, confirmation):
    require(confirmation == DELETE_CONFIRMATION_TEXT, "DELETE_CONFIRMATION", f"请输入“{DELETE_CONFIRMATION_TEXT}”确认永久删除")
    require(not any(j["status"] in ("RUNNING", "QUEUED") for j in store.jobs()), "MAINTENANCE_BUSY", "请先完成或取消任务")
    with store.transaction() as db:
        session = store.get("session", session_id, db)
        require(session["revision"] == revision, "REVISION_CONFLICT", "只能删除版本未变化的数据对话")
        run_ids = [r["id"] for r in store.list("run", db) if r["sessionId"] == session_id]
        artifact_names = [a["fileName"] for a in store.list("artifact", db) if a["sessionId"] == session_id]
        for kind in ("message", "turn", "run", "comparison", "artifact", "intent", "history_context", "result_query", "mapping_revision", "relationship_revision", "metric_catalog"):
            for value in store.list(kind, db):
                if value.get("sessionId") == session_id:
                    db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, value["id"]))
        definition_attachments = {d["attachmentId"] for d in store.list("definition", db)}
        for attachment in store.list("attachment", db):
            if attachment["scope"] == session_id and attachment["id"] not in definition_attachments:
                db.execute("DELETE FROM objects WHERE kind='attachment' AND id=?", (attachment["id"],))
        db.execute("DELETE FROM objects WHERE kind='session' AND id=?", (session_id,))
        db.execute("DELETE FROM events WHERE scope=?", (session_id,))
        db.execute("DELETE FROM jobs WHERE scope=?", (session_id,))
        for row in db.execute("SELECT id,response FROM requests").fetchall():
            # Requests refer to deleted turns/runs and cannot be replayed after deletion.
            if not db.execute("SELECT 1 FROM objects WHERE kind='turn' AND id=?", (json.loads(row[1]).get("turnId"),)).fetchone():
                db.execute("DELETE FROM requests WHERE id=?", (row[0],))
    checkpoint = store.home / "checkpoints.sqlite3"
    if checkpoint.exists():
        from langgraph.checkpoint.sqlite import SqliteSaver
        with closing(sqlite3.connect(checkpoint)) as conn:
            saver = SqliteSaver(conn)
            for id in run_ids:
                saver.delete_thread(id)
    # Remove only this session's views; other sessions and diagnostics stay usable.
    for part in ("exports", "open"):
        directory = store.home / part
        if directory.exists():
            for name in artifact_names:
                safe_child(directory, name).unlink(missing_ok=True)
    return {"deletedSessionId": session_id}


def verify_references(folder):
    """Validate actual blob references, never confuse rule/result digests with files."""
    blob_keys = {"snapshotHash", "convertedSha256", "rowsHash", "prepared_hash", "result_hash", "audit_hash", "renderHash", "sourceDocxHash", "manualSourceHash"}
    checked = set()
    def check_blob(hash):
        require(isinstance(hash, str) and len(hash) == 64 and all(c in "0123456789abcdef" for c in hash), "BACKUP_CORRUPT", "备份文件引用无效")
        if hash in checked:
            return
        checked.add(hash)
        path = folder / "blobs" / hash
        require(path.is_file(), "BACKUP_CORRUPT", "备份缺少被引用的托管文件")
        content = path.read_bytes()
        require(sha(content) == hash, "BACKUP_CORRUPT", "备份文件引用哈希不匹配")
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return
        visit(value)
    def visit(value, kind=None):
        if isinstance(value, dict):
            for key, item in value.items():
                if item and (key in blob_keys or key == "sha256" and (kind in ("attachment", "artifact") or value.get("kind") == "USER_AMENDMENT")):
                    check_blob(item)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    with closing(sqlite3.connect(folder / "datamind.sqlite3")) as db:
        for kind, data in db.execute("SELECT kind,data FROM objects"):
            visit(json.loads(data), kind)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_source'").fetchone():
            for (file_hash,) in db.execute("SELECT DISTINCT file_sha256 FROM data_source"):
                check_blob(file_hash)
    # Checkpoint serializers store typed channels separately. All hashes which
    # name existing blobs are traversed too; missing explicit references above
    # are still fatal rather than confused with rule/contract digest values.
    import re
    checkpoint = folder / "checkpoints.sqlite3"
    if checkpoint.exists():
        from langgraph.checkpoint.sqlite import SqliteSaver
        with closing(sqlite3.connect(checkpoint)) as conn:
            saver = SqliteSaver(conn)
            for record in saver.list(None):
                visit(record.checkpoint.get("channel_values", {}))
                for _, channel, value in record.pending_writes or []:
                    visit({channel: value})
        for hash in set(x.decode() for x in re.findall(rb"[a-f0-9]{64}", checkpoint.read_bytes())):
            if (folder / "blobs" / hash).is_file():
                check_blob(hash)
    v2_checkpoint = folder / "three-source-checkpoints.sqlite3"
    if v2_checkpoint.exists():
        from langgraph.checkpoint.sqlite import SqliteSaver
        with closing(sqlite3.connect(v2_checkpoint)) as conn:
            saver = SqliteSaver(conn)
            for record in saver.list(None):
                visit(record.checkpoint.get("channel_values", {}))
                for _, channel, value in record.pending_writes or []:
                    visit({channel: value})
