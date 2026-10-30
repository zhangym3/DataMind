"""Bounded local parsers. Document contents are evidence, never instructions."""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .core import AppError, LIMITS, checkpoint, require, sha

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RULE_WORDS = re.compile(r"指标|指标公式|指标说明|计算规则|分子|分母|计算公式|统计口径|纳入|排除|不计算在内|metric|denominator", re.I)


def inspect_zip(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        require(len(entries) <= LIMITS["zip_members"], "FILE_LIMIT", "压缩包文件数超限")
        require(sum(x.file_size for x in entries) <= LIMITS["zip_bytes"], "FILE_LIMIT", "解压后文件大小超限")
        for entry in entries:
            require(not entry.flag_bits & 1, "ENCRYPTED_FILE", "请先移除文档密码")
            require(entry.file_size / max(entry.compress_size, 1) <= LIMITS["zip_ratio"], "FILE_LIMIT", "压缩比超限")
            require(not entry.filename.startswith(("/", "\\")) and ".." not in Path(entry.filename).parts, "INVALID_FILE", "文档包含非法路径")
        return archive
    except (zipfile.BadZipFile, RuntimeError):
        raise AppError("INVALID_FILE", "文件结构损坏或不是有效的 Office 文件") from None


def converter_path():
    candidates = [shutil.which("soffice"), r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def converter_version():
    executable = converter_path()
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        version = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        return {"name": "LibreOffice", "version": version[0][:160] if version else "unknown"}
    except (OSError, subprocess.TimeoutExpired):
        return {"name": "LibreOffice", "version": "unknown"}


def convert_document(data, extension, target="docx"):
    executable = converter_path()
    require(executable, "CONVERTER_UNAVAILABLE", "需要安装 LibreOffice 后才能进行 DOCX/PDF 页面渲染")
    with tempfile.TemporaryDirectory(prefix="datamind-convert-") as folder:
        root = Path(folder)
        source = root / ("source" + extension)
        source.write_bytes(data)
        output = root / "output"
        output.mkdir()
        import time
        process = subprocess.Popen([executable, "-env:UserInstallation=" + (root / "profile").as_uri(), "--headless", "--convert-to", target, "--outdir", str(output), str(source)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        try:
            while process.poll() is None:
                checkpoint()
                require(time.monotonic() - started <= 120, "CONVERTER_TIMEOUT", "文档转换超过 120 秒")
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                if __import__('os').name == 'nt':
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                process.wait(timeout=10)
        artifact = output / ("source." + target)
        require(process.returncode == 0 and artifact.is_file(), "CONVERSION_FAILED", "文档转换失败，请另存为 DOCX 后重试")
        return artifact.read_bytes()


def normalize_cell(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value)


def _header_values(values):
    """Return a valid header row, or None for a preamble/non-header row."""
    if any(isinstance(value, dict) for value in values):
        return None
    headers = [str(value).strip() if value is not None else "" for value in values]
    while headers and not headers[-1]:
        headers.pop()
    if not headers or not all(headers) or len(set(headers)) != len(headers):
        return None
    return headers


def _nonempty_count(values):
    return sum(value is not None for value in values)


def parse_xlsx(data):
    import openpyxl
    archive = inspect_zip(data)
    names = set(archive.namelist())
    require("xl/workbook.xml" in names, "FORMAT_MISMATCH", "文件扩展名与实际格式不一致")
    require(not any("vbaProject" in n for n in names), "MACRO_UNSUPPORTED", "不支持含宏的工作簿")
    # Workbooks are read-only; no formulas, links or macros are executed.
    workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True, keep_links=False)
    formulas = openpyxl.load_workbook(io.BytesIO(data), data_only=False, read_only=True, keep_links=False)
    require(len(workbook.worksheets) <= LIMITS["sheets"], "FILE_LIMIT", "工作表数量超限")
    sheets, total = [], 0
    try:
        for sheet, formula_sheet in zip(workbook.worksheets, formulas.worksheets):
            require((sheet.max_row or 0) <= LIMITS["sheet_rows"] + 1 and (sheet.max_column or 0) <= LIMITS["sheet_columns"], "FILE_LIMIT", "工作表行列数超限")
            # Dimension metadata can be missing or falsely small. Scan the
            # actual XML extent and enforce limits while consuming rows.
            sheet.reset_dimensions()
            formula_sheet.reset_dimensions()
            raw_numbers = {}
            xml_path = sheet._worksheet_path.lstrip("/")
            for _, cell in ET.iterparse(io.BytesIO(archive.read(xml_path)), events=("end",)):
                if cell.tag == "{" + NS["s"] + "}c":
                    val = cell.find("s:v", NS)
                    if cell.get("t", "n") == "n" and val is not None:
                        raw_numbers[cell.get("r")] = val.text
                    cell.clear()
            rows, headers, header_row, warnings = [], None, None, []
            preamble = []
            for row_number, (cells, fcells) in enumerate(zip(sheet.iter_rows(), formula_sheet.iter_rows()), 1):
                require(row_number <= LIMITS["sheet_rows"] + 1 and len(cells) <= LIMITS["sheet_columns"], "FILE_LIMIT", "工作表行列数超限")
                if row_number % 256 == 1:
                    checkpoint()
                values = []
                for cell, formula_cell in zip(cells, fcells):
                    if formula_cell.data_type == "f":
                        values.append({"invalid": "FORMULA_CACHE_UNTRUSTED", "cached": normalize_cell(cell.value)})
                    elif cell.data_type == "e":
                        values.append({"invalid": "EXCEL_ERROR"})
                    elif getattr(cell, "coordinate", None) in raw_numbers and isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                        values.append(raw_numbers[cell.coordinate])
                    else:
                        values.append(normalize_cell(cell.value))
                if not any(v is not None for v in values):
                    continue
                if headers is None:
                    candidate = _header_values(values)
                    preamble.append((row_number, values))
                    # Exported reports often have a title/date row before the
                    # actual table. A multi-column, complete row is a stronger
                    # header signal than those one-cell preambles.
                    if candidate and _nonempty_count(values) >= 2:
                        headers = candidate
                        header_row = row_number
                        if len(preamble) > 1:
                            warnings.append("PREAMBLE_ROWS_SKIPPED")
                        preamble = []
                    continue
                require(not any(v is not None for v in values[len(headers):]), "HEADER_UNRESOLVED", "数据列超出表头范围")
                rows.append({"row": row_number, "values": dict(zip(headers, values[:len(headers)]))})
            if headers is None and preamble:
                header_row, header_values = preamble[0]
                headers = _header_values(header_values)
                require(headers, "HEADER_UNRESOLVED", "存在空表头或重复表头，请整理为一行唯一表头", {"sheet": sheet.title, "row": header_row})
                for row_number, values in preamble[1:]:
                    require(not any(v is not None for v in values[len(headers):]), "HEADER_UNRESOLVED", "数据列超出表头范围")
                    rows.append({"row": row_number, "values": dict(zip(headers, values[:len(headers)]))})
            total += len(rows)
            require(total <= LIMITS["session_rows"], "FILE_LIMIT", "总数据行数超限")
            if sheet.sheet_state != "visible":
                warnings.append("HIDDEN_SHEET_INCLUDED")
            if headers:
                sheets.append({"name": sheet.title, "headers": headers, "headerRow": header_row, "rows": rows, "rowCount": len(rows), "warnings": warnings, "visibility": sheet.sheet_state})
    finally:
        workbook.close()
        formulas.close()
        archive.close()
    require(sheets, "EMPTY_FILE", "工作簿没有可读取内容")
    return {"kind": "workbook", "sheets": sheets, "rowCount": total, "parserVersion": "xlsx/1.0.0"}


def parse_xls(data):
    import xlrd
    import struct
    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
    except xlrd.XLRDError:
        raise AppError("INVALID_FILE", "无法读取 XLS，请另存为 XLSX") from None
    require(book.nsheets <= LIMITS["sheets"], "FILE_LIMIT", "工作表数量超限")
    require(book.biff_version >= 50, "FORMAT_UNSUPPORTED", "仅支持 Excel 5/95/97-2003 XLS，请另存为 XLSX")
    sheets, total = [], 0
    try:
        for index in range(book.nsheets):
            # xlrd exposes only formula caches. Inspect the BIFF records as well so
            # formulas cannot become trusted constants during parsing.
            formula_cells, position = set(), book._sh_abs_posn[index]
            while position + 4 <= len(book.mem):
                code, size = struct.unpack_from("<HH", book.mem, position)
                position += 4
                require(position + size <= len(book.mem), "INVALID_FILE", "XLS 记录损坏")
                if code in (0x0006, 0x0206, 0x0406) and size >= 4:
                    formula_cells.add(struct.unpack_from("<HH", book.mem, position))
                position += size
                if code == 0x000A:
                    break
            sheet = book.sheet_by_index(index)
            require(sheet.nrows <= LIMITS["sheet_rows"] + 1 and sheet.ncols <= LIMITS["sheet_columns"], "FILE_LIMIT", "工作表行列数超限")
            headers, rows, header_row = None, [], None
            preamble = []
            for r in range(sheet.nrows):
                if r % 256 == 0:
                    checkpoint()
                values = []
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    if (r, c) in formula_cells:
                        value = {"invalid": "FORMULA_CACHE_UNTRUSTED"}
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        value = {"invalid": "EXCEL_ERROR"}
                    elif cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            dt = xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
                            value = {"invalid": "TIME_WITHOUT_DATE"} if cell.value < 1 else (dt.date().isoformat() if dt.time().isoformat() == "00:00:00" else dt.isoformat())
                        except (ValueError, OverflowError):
                            value = {"invalid": "EXCEL_DATE_INVALID"}
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = bool(cell.value)
                    elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        value = None
                    elif cell.ctype == xlrd.XL_CELL_NUMBER:
                        value = str(int(cell.value)) if cell.value.is_integer() else repr(cell.value)
                    else:
                        value = str(cell.value)
                    values.append(value)
                if not any(v is not None for v in values):
                    continue
                if headers is None:
                    candidate = _header_values(values)
                    preamble.append((r + 1, values))
                    if candidate and _nonempty_count(values) >= 2:
                        headers = candidate
                        header_row = r + 1
                        preamble = []
                    continue
                else:
                    require(not any(v is not None for v in values[len(headers):]), "HEADER_UNRESOLVED", "数据列超出表头")
                    rows.append({"row": r + 1, "values": dict(zip(headers, values))})
            if headers is None and preamble:
                header_row, header_values = preamble[0]
                headers = _header_values(header_values)
                require(headers, "HEADER_UNRESOLVED", "存在空表头或重复表头")
                for row_number, values in preamble[1:]:
                    require(not any(v is not None for v in values[len(headers):]), "HEADER_UNRESOLVED", "数据列超出表头")
                    rows.append({"row": row_number, "values": dict(zip(headers, values))})
            total += len(rows)
            require(total <= LIMITS["session_rows"], "FILE_LIMIT", "总数据行数超限")
            if headers:
                sheets.append({"name": sheet.name, "headers": headers, "rowCount": len(rows), "rows": rows, "warnings": ["HIDDEN_SHEET_INCLUDED"] if sheet.visibility else [], "visibility": sheet.visibility, "headerRow": header_row})
    finally:
        book.release_resources()
    require(sheets, "EMPTY_FILE", "工作簿没有可读取内容")
    return {"kind": "workbook", "sheets": sheets, "rowCount": total, "parserVersion": "xls-biff/1.0.0", "dateSystem": book.datemode, "numericRepresentation": "IEEE754_SHORTEST_DECIMAL"}


def parse_docx(data):
    from docx.oxml.ns import qn
    archive = inspect_zip(data)
    require("word/document.xml" in archive.namelist(), "FORMAT_MISMATCH", "文件扩展名与实际格式不一致")
    # Enumerate package parts, rather than python-docx's body-only API. Reject
    # unresolved final-view changes and non-text regions instead of losing clauses.
    parts = sorted(n for n in archive.namelist() if re.fullmatch(r"word/(document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml", n))
    nodes = []

    def visit(element, path):
        for index, child in enumerate(element, 1):
            tag = child.tag.rsplit("}", 1)[-1]
            locator = f"{path}/{tag}:{index}"
            if tag == "p":
                def inside_nested_box(node):
                    for ancestor in node.iterancestors():
                        if ancestor is child:
                            return False
                        if ancestor.tag == qn("w:txbxContent"):
                            return True
                    return False
                text = "".join(n.text or "" for n in child.iter(qn("w:t")) if not inside_nested_box(n))
                if text.strip():
                    nodes.append({"locator": locator, "text": text, "quoteSha256": sha(text.encode())})
                for box_index, box in enumerate(child.iter(qn("w:txbxContent")), 1):
                    visit(box, locator + f"/textbox:{box_index}")
            elif tag not in ("pPr", "rPr", "sectPr"):
                visit(child, locator)
            require(len(nodes) <= LIMITS["document_nodes"], "FILE_LIMIT", "文档节点数超限")

    from lxml import etree
    for part in parts:
        root = etree.fromstring(archive.read(part), parser=etree.XMLParser(resolve_entities=False, no_network=True))
        tags = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
        require(not tags & {"ins", "del", "moveFrom", "moveTo", "commentRangeStart"}, "DOCUMENT_FINAL_VIEW_UNRESOLVED", "文档含修订或批注，请核对并接受/拒绝全部修订、移除批注后导入最终正文")
        require(not tags & {"OLEObject", "object", "pict"} and "blip" not in tags, "DOCUMENT_UNSUPPORTED_REGION", "文档含图片或嵌入对象，请将相关口径转换为可编辑文字，移除图片后再导入")
        visit(root, part)
    archive.close()
    text_bytes = sum(len(n["text"].encode()) for n in nodes)
    require(text_bytes <= LIMITS["document_bytes"], "FILE_LIMIT", "文档文本大小超限")
    require(nodes, "EMPTY_FILE", "文档没有可读取文字；扫描图片需先转换为可编辑文字")
    return {"kind": "document", "nodes": nodes, "nodeCount": len(nodes), "parts": parts, "finalViewPolicy": "FINAL_TEXT_ONLY/1.0", "parserVersion": "docx/1.1.0"}


def classify(snapshot):
    def decision(role, reason, confidence, evidence, **details):
        return {"role": role, "reason": reason, "confidence": confidence,
                "routerVersion": "content-rules/1.1", "evidence": evidence, **details}

    if snapshot["kind"] == "document":
        nodes = snapshot["nodes"]
        # A mention of an indicator or a historical number is not a rule.
        strong = re.compile(r"分子|分母|计算公式|指标公式|指标说明|计算规则|统计口径|纳入条件|排除条件|不计算在内|denominator|numerator", re.I)
        rule_nodes = [n for n in nodes if strong.search(n["text"])]
        report_nodes = [n for n in nodes if re.search(r"(?:分析|统计|运营|质量|年度|月度|季度)报告|historical report", n["text"], re.I)]
        result_nodes = [n for n in nodes if re.search(r"同比|环比|实际完成|本期结果|上期结果|本月完成", n["text"])]
        evidence = [{"locator": n["locator"], "quoteSha256": n["quoteSha256"]} for n in nodes if n in rule_nodes or n in report_nodes or n in result_nodes]
        if report_nodes and result_nodes:
            return decision("UNKNOWN" if rule_nodes else "OTHER", "历史报告含规则与结果，需人工核对用途" if rule_nodes else "历史报告结果不作为数据明细或指标口径", "LOW" if rule_nodes else "HIGH", evidence)
        # Repeated Word table rows are recognizable as records, but only Excel
        # has an execution parser. Keep the role separate from that capability.
        tables = {}
        for node in nodes:
            match = re.match(r"(.*/tbl:\d+)/tr:(\d+)/", node["locator"])
            if match:
                tables.setdefault(match[1], {}).setdefault(int(match[2]), []).append(node)
        records = []
        for rows in tables.values():
            if len(rows) < 3:
                continue
            header = " ".join(n["text"] for n in rows[min(rows)])
            if re.search(r"(?:患者|手术|就诊|事件|住院)(?:编号|号|ID)|surgery_id|event_id", header, re.I) and re.search(r"日期|时间|date|time", header, re.I):
                records.extend(n for group in rows.values() for n in group)
        if records:
            evidence += [{"locator": n["locator"], "quoteSha256": n["quoteSha256"]} for n in records]
            return decision("MIXED" if rule_nodes else "DATA_FILE", "Word 同时含记录与规则，请拆分" if rule_nodes else "Word 含业务记录；数据执行要求 Excel，请另存为 XLSX", "HIGH", evidence)
        if rule_nodes:
            return decision("METRIC_DEFINITION", "定位到指标口径条款，将逐项解析并校验计算条件", "HIGH" if len(rule_nodes) >= 2 else "LOW", evidence)
        return decision("UNKNOWN", "缺少明确口径或记录结构，需要人工确认", "LOW", evidence)
    data_tables, rule_tables, report_tables, evidence = [], [], [], []
    for s in snapshot["sheets"]:
        rule = any(RULE_WORDS.search(h) for h in s["headers"]) and any(re.search(r"公式|口径|分子|分母", h) for h in s["headers"])
        report = any(re.search(r"指标", h) for h in s["headers"]) and any(re.search(r"同比|环比|本期值|上期值|本期结果|上期结果", h) for h in s["headers"])
        (rule_tables if rule else report_tables if report else data_tables).append(s["name"])
        evidence.append({"sheet": s["name"], "headerRow": s["headerRow"], "headers": s["headers"]})
    role = "MIXED" if sum(bool(x) for x in (data_tables, rule_tables, report_tables)) > 1 else "METRIC_DEFINITION" if rule_tables else "OTHER" if report_tables else "DATA_FILE"
    return decision(role, "根据实际表头与文档结构识别；历史结果表不作为明细接入" if report_tables else "根据实际表头与文档结构识别", "LOW" if role == "MIXED" else "HIGH", evidence, dataSheets=data_tables, ruleSheets=rule_tables, reportSheets=report_tables)


def route_status(role, kind, confidence):
    if role == "OTHER":
        return "EXCLUDED"
    if (role == "DATA_FILE" and kind != "workbook") or (role == "METRIC_DEFINITION" and kind != "document"):
        return "UNSUPPORTED_ROLE_FORMAT"
    return "READY" if role in ("DATA_FILE", "METRIC_DEFINITION") and confidence in ("HIGH", "CONFIRMED") else "NEEDS_CONFIRMATION"


def import_file(store, path, scope):
    path = Path(path)
    require(path.is_file(), "FILE_NOT_FOUND", "文件不存在或无法访问")
    require(0 < path.stat().st_size <= LIMITS["file_bytes"], "FILE_LIMIT", "请选择非空且不超过 50 MB 的文件")
    extension = path.suffix.lower()
    require(extension in (".xlsx", ".xls", ".docx", ".doc"), "UNSUPPORTED_FORMAT", "支持 XLSX、XLS、DOCX、DOC 文件")
    data = path.read_bytes()
    require(0 < len(data) <= LIMITS["file_bytes"], "FILE_LIMIT", "文件大小在读取期间变化")
    original_hash = store.blob(data)
    converted_hash = None
    from .skills import invoke
    parser_skill = "surgery-excel-ingest" if extension in (".xlsx", ".xls") else "surgery-word-spec"
    snapshot = invoke(parser_skill, "parse", data, extension)
    snapshot_hash = store.blob(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode())
    route = classify(snapshot)
    with store.transaction() as db:
        existing = next((x for x in store.list("attachment", db) if x["scope"] == scope and x["sha256"] == original_hash), None)
        if existing:
            return existing
        if scope != "library" and snapshot["kind"] == "workbook":
            session = store.get("session", scope, db)
            current = [store.get("attachment", id, db) for id in session["attachmentIds"]]
            sheets = [s for a in current for s in a["profile"].get("sheets", [])] + snapshot["sheets"]
            require(sum(s["rowCount"] for s in sheets) <= LIMITS["session_rows"] and len(sheets) <= LIMITS["sheets"], "FILE_LIMIT", "会话累计数据行数或工作表数量超限；请拆分为独立会话")
        attachment = store.put("attachment", {"scope": scope, "name": path.name, "size": len(store.read_blob(original_hash)), "extension": extension, "sha256": original_hash, "convertedSha256": converted_hash, "snapshotHash": snapshot_hash, "route": route, "status": route_status(route["role"], snapshot["kind"], route["confidence"]), "profile": {"kind": snapshot["kind"], "sheets": [{k: v for k, v in s.items() if k != "rows"} for s in snapshot.get("sheets", [])], "nodeCount": snapshot.get("nodeCount", 0), "executionBlocked": snapshot.get("executionBlocked")}}, db)
        store.event(db, scope, "attachment.imported", {"attachmentId": attachment["id"], "role": route["role"]})
    return attachment


def snapshot_for(store, attachment):
    return json.loads(store.read_blob(attachment["snapshotHash"]))
