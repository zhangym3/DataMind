"""Trusted parser adapter owned by the surgery-word-spec skill.

The Word skill owns both modern DOCX extraction and the text-only legacy DOC
fallback. It returns evidence-oriented snapshots; no document content is
executed as instructions.
"""
from __future__ import annotations

import io
import struct
import tempfile
from pathlib import Path

from .core import AppError, LIMITS, checkpoint, require, sha


PARSER_VERSION = "surgery-word-spec/1.0.0"


def legacy_doc_available():
    """Return whether the bundled legacy DOC extractor is importable."""
    try:
        from doc2txt import get_antiword_binary

        return Path(get_antiword_binary()).is_file()
    except (ImportError, OSError, RuntimeError):
        return False


def _legacy_ole_text(data: bytes) -> str:
    """Read the main text range from an OLE Word document.

    This deliberately remains a text-only fallback. It does not interpret
    fields, macros, embedded objects, layout, or arbitrary binary structures.
    """
    try:
        import olefile
    except ImportError:
        raise AppError("DOC_PARSER_UNAVAILABLE", "旧版 DOC 回退解析器未安装") from None

    ole = None
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
        if not ole.exists("WordDocument"):
            raise AppError("FORMAT_MISMATCH", "文件不包含 WordDocument 数据流")
        stream = ole.openstream("WordDocument").read()
    except AppError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise AppError("DOC_PARSE_FAILED", "OLE 文档结构无法读取", {"reason": str(exc)[:200]}) from None
    finally:
        if ole is not None:
            ole.close()

    require(len(stream) >= 0x20, "DOC_PARSE_FAILED", "WordDocument 数据流不完整")
    flags = struct.unpack_from("<H", stream, 0x0A)[0]
    require(not flags & 0x0100, "DOC_ENCRYPTED", "加密 DOC 无法在无 Office 环境下解析")
    fc_min, fc_mac = struct.unpack_from("<II", stream, 0x18)
    require(0x20 <= fc_min < fc_mac <= len(stream), "DOC_PARSE_FAILED", "DOC 正文范围无效")
    raw = stream[fc_min:fc_mac]
    candidates = []
    for encoding in ("utf-16le", "gb18030", "cp1252", "latin1"):
        text = raw.decode(encoding, errors="replace")
        printable = sum(ch.isprintable() or ch in "\r\n\t\x07\x0b" for ch in text)
        cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
        score = printable + 10 * cjk - 100 * text.count("\ufffd")
        candidates.append((score, text))
    text = max(candidates, key=lambda item: item[0])[1]
    return text.replace("\x00", "").replace("\x0b", "\n").replace("\x07", "\t")


def _legacy_text(data: bytes) -> str:
    # Antiword is the fast path. The OLE reader handles valid files that the
    # bundled executable rejects because of a newer producer/FIB revision.
    try:
        from doc2txt import extract_text
    except (ImportError, OSError):
        return _legacy_ole_text(data)
    if not legacy_doc_available():
        return _legacy_ole_text(data)

    with tempfile.TemporaryDirectory(prefix="datamind-doc-") as folder:
        source = Path(folder) / "source.doc"
        source.write_bytes(data)
        try:
            text = extract_text(str(source), optimize_format=True)
        except Exception as exc:
            try:
                return _legacy_ole_text(data)
            except AppError as fallback:
                raise AppError(
                    "DOC_PARSE_FAILED",
                    "旧版 DOC 文档解析失败，请另存为 DOCX 后重试",
                    {"reason": str(exc)[:300], "fallback": fallback.code},
                ) from None
    require(isinstance(text, str), "DOC_PARSE_FAILED", "旧版 DOC 文档未返回可读文本")
    return text


def _legacy_nodes(text: str):
    nodes = []
    table_index = 0
    table_row = 0
    previous_blank = True
    for line_number, raw in enumerate(text.splitlines(), 1):
        value = raw.replace("\x00", "").strip()
        if not value:
            previous_blank = True
            continue
        is_table = value.count("|") >= 2
        if is_table and (previous_blank or table_row == 0):
            table_index += 1
            table_row = 0
        if is_table:
            table_row += 1
            locator = f"legacy-doc/tbl:{table_index}/tr:{table_row}/p:1"
        else:
            locator = f"legacy-doc/p:{len(nodes) + 1}"
            table_row = 0
        nodes.append({"locator": locator, "text": value, "quoteSha256": sha(value.encode("utf-8")), "source": "LEGACY_DOC_TEXT"})
        previous_blank = False
        if len(nodes) % 256 == 0:
            checkpoint()
    return nodes


def parse_doc_legacy(data: bytes):
    require(data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")), "FORMAT_MISMATCH", "不是有效的 DOC 文件")
    text = _legacy_text(data)
    require(len(text.encode("utf-8")) <= LIMITS["document_bytes"], "FILE_LIMIT", "文档文本大小超限")
    nodes = _legacy_nodes(text)
    require(nodes, "EMPTY_FILE", "文档没有可读取文字；请另存为 DOCX 后重试")
    require(len(nodes) <= LIMITS["document_nodes"], "FILE_LIMIT", "文档节点数超限")
    return {
        "kind": "document",
        "nodes": nodes,
        "nodeCount": len(nodes),
        "parts": ["WordDocument"],
        "finalViewPolicy": "TEXT_ONLY/1.0",
        "parserVersion": PARSER_VERSION,
        "layoutVerified": False,
        "warnings": ["LEGACY_DOC_TEXT_ONLY", "EMBEDDED_OBJECTS_NOT_EXTRACTED"],
    }


def parse_word_document(data: bytes, extension: str):
    extension = str(extension).lower()
    if extension == ".doc":
        return parse_doc_legacy(data)
    if extension == ".docx":
        from .files import parse_docx

        snapshot = parse_docx(data)
        snapshot["parserVersion"] = PARSER_VERSION
        snapshot["skill"] = "surgery-word-spec"
        return snapshot
    raise AppError("UNSUPPORTED_FORMAT", "指标说明支持 DOCX 和 DOC 文件")
