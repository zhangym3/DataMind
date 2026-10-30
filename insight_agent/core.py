"""Shared, JSON-only contracts. No patient values or secrets in exceptions."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import rfc8785

VERSION = "1.0.0-rc1"
ENGINE_VERSION = "exact-engine/1.0.0"
DELETE_CONFIRMATION_TEXT = "确认删除"
ROOT = Path(__file__).resolve().parent
job_guard = ContextVar("job_guard", default=lambda: None)


def checkpoint():
    job_guard.get()()


LIMITS = {
    "file_bytes": 50 * 1024**2, "batch_bytes": 200 * 1024**2,
    "batch_files": 20, "sheet_rows": 100000, "sheet_columns": 128,
    "session_rows": 300000, "sheets": 50, "document_bytes": 2 * 1024**2,
    "document_nodes": 10000, "candidates": 200,
    "zip_bytes": 512 * 1024**2, "zip_members": 5000, "zip_ratio": 200,
    "groups": 10000, "run_seconds": 900, "model_calls": 30,
    "model_tokens": 1000000, "output_tokens": 1000000,
    "model_response_bytes": 32 * 1024**2,
}


class AppError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def require(condition, code, message, details=None):
    if not condition:
        raise AppError(code, message, details)


def uid(prefix=""):
    return prefix + uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def default_home():
    return Path(os.environ.get("DATAMIND_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DataMind")


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uid() + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def safe_child(root: Path, name: str):
    candidate = (root / name).resolve()
    require(candidate.is_relative_to(root.resolve()), "PATH_FORBIDDEN", "禁止访问托管目录之外的文件")
    return candidate
