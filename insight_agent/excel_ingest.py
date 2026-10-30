"""Trusted parser adapter owned by the surgery-excel-ingest skill."""
from __future__ import annotations

from .core import AppError


PARSER_VERSION = "surgery-excel-ingest/1.0.0"


def parse_excel_workbook(data: bytes, extension: str):
    extension = str(extension).lower()
    if extension == ".xlsx":
        from .files import parse_xlsx

        snapshot = parse_xlsx(data)
    elif extension == ".xls":
        from .files import parse_xls

        snapshot = parse_xls(data)
    else:
        raise AppError("UNSUPPORTED_FORMAT", "数据接入支持 XLSX 和 XLS 文件")
    snapshot["parserVersion"] = PARSER_VERSION
    snapshot["skill"] = "surgery-excel-ingest"
    return snapshot
