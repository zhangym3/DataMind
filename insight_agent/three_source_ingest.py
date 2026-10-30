"""Bounded, source-specific XLSX import into a conversation's revisioned data space."""
from __future__ import annotations

import io
import json
import re
import unicodedata
import warnings
import zipfile
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook

from .core import LIMITS, AppError, digest, now, require, sha, uid
from .three_source_kb import active_release, bundled_catalog

PARSER_VERSION = "three-source-xlsx/1.0"
SKILLS = {
    "FIRST_SURGERY": "surgery-first-ingest",
    "CANCELLATION": "surgery-cancellation-ingest",
    "SURGERY_DETAIL": "surgery-detail-ingest",
}
TABLES = {
    "FIRST_SURGERY": "first_surgery_row",
    "CANCELLATION": "cancellation_row",
    "SURGERY_DETAIL": "surgery_detail_row",
}


def _filename_unit(name, routing):
    stem = unicodedata.normalize("NFKC", Path(name).stem).lower()
    stem = re.sub(r"\s+", "", stem)
    hits = []
    for unit in routing["units"]:
        matched = []
        for alias in unit["aliases"]:
            normalized = unicodedata.normalize("NFKC", alias).lower()
            prefix = r"(?<![十百千零一二三四五六七八九])" if normalized.startswith(("一", "二", "三")) else ""
            if re.search(prefix + re.escape(normalized), stem):
                matched.append(alias)
        if matched:
            hits.append({"code": unit["code"], "aliases": matched})
    evidence = {"filename": name, "matches": hits, "policy": routing["version"]}
    return (hits[0]["code"] if len(hits) == 1 else None), evidence


def _semantic_unit(name, routing, gateway):
    unit, evidence = _filename_unit(name, routing)
    if unit or gateway is None:
        return unit, evidence
    try:
        response = gateway.call("cancellation_unit", {"fileName": Path(name).name,
            "units": [{"code": item["code"], "display": item["display"], "aliases": item["aliases"]}
                      for item in routing["units"]],
            "instruction": "只比较文件名与三个手术室的语义。若没有唯一明确的手术室数字证据，必须返回 ABSTAIN。"},
            {"type": "object", "additionalProperties": False,
             "required": ["candidateUnitCode", "fileNameEvidence", "conflictingEvidence", "decision"],
             "properties": {"candidateUnitCode": {"enum": ["OR_1", "OR_2", "OR_3", None]},
                            "fileNameEvidence": {"type": "string"},
                            "conflictingEvidence": {"type": "string"},
                            "decision": {"enum": ["CANDIDATE", "ABSTAIN"]}}},
            uid("unit_judge_"))
        evidence["semanticJudge"] = response
        # The model supplies evidence for review; automatic assignment still
        # requires a locally verified explicit number in the original name.
        if response["decision"] == "CANDIDATE":
            explicit, checked = _filename_unit(name, routing)
            if explicit == response["candidateUnitCode"]:
                return explicit, evidence
    except AppError as exc:
        evidence["semanticJudgeStatus"] = exc.code
    return None, evidence


def _cell(cell, value_type):
    require(cell.data_type not in ("f", "e"), "WORKBOOK_CELL_UNSAFE", "工作簿包含公式或错误单元格")
    value = cell.value
    if value is None or value == "":
        return None
    if value_type == "DATE":
        if isinstance(value, (date, datetime)):
            return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
        raise AppError("WORKBOOK_DATE", "日期单元格格式无法解析")
    if value_type == "LOCAL_DATETIME":
        if isinstance(value, datetime):
            return value.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(text, fmt).isoformat(sep=" ", timespec="seconds")
            except ValueError:
                pass
        raise AppError("WORKBOOK_DATETIME", "退单时间单元格格式无法解析")
    if value_type == "TIME_HHMM":
        if isinstance(value, (datetime, time)):
            return value.strftime("%H:%M")
        text = str(value).strip()
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
            hour, minute = map(int, text.split(":")[:2])
            require(hour < 24 and minute < 60, "WORKBOOK_TIME", "时分单元格格式无法解析")
            return f"{hour:02d}:{minute:02d}"
        raise AppError("WORKBOOK_TIME", "时分单元格格式无法解析")
    if value_type == "INTEGER":
        require(isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) == value or
                isinstance(value, str) and re.fullmatch(r"\d+", value.strip()),
                "WORKBOOK_INTEGER", "整数单元格格式无法解析")
        return int(value)
    return str(value).strip()


def _safe_xlsx(raw):
    require(len(raw) <= LIMITS["file_bytes"] and zipfile.is_zipfile(io.BytesIO(raw)),
            "FORMAT_UNSUPPORTED", "仅支持大小受限的 XLSX 工作簿")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) <= LIMITS["zip_members"] and sum(m.file_size for m in members) <= LIMITS["zip_bytes"],
                "FILE_LIMIT", "工作簿解压规模超限")
        for member in members:
            name = member.filename.lower()
            require(not name.startswith("/") and ".." not in Path(name).parts and
                    not any(marker in name for marker in ("vbaproject", "externallink", "connections.xml")),
                    "WORKBOOK_UNSAFE", "工作簿包含不支持的外部内容")
            require(member.file_size <= max(1, member.compress_size) * LIMITS["zip_ratio"],
                    "FILE_LIMIT", "工作簿压缩比例超限")


def detect_source_type(path):
    path = Path(path)
    require(path.suffix.lower() == ".xlsx" and path.is_file(), "FORMAT_UNSUPPORTED", "请选择 XLSX 文件")
    raw = path.read_bytes()
    _safe_xlsx(raw)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    try:
        require(len(workbook.worksheets) == 1 and workbook.worksheets[0].sheet_state == "visible", "WORKBOOK_SHEETS", "工作簿必须只有一个可见工作表")
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        prefix = [next(rows, ()) for _ in range(3)]
        matches = []
        for layout in bundled_catalog()["layout"]["layouts"]:
            header = prefix[layout["headerRow"] - 1]
            actual = [str(c.value).strip() if c.value is not None else "" for c in header]
            expected = [column["header"] for column in layout["columns"]]
            title = str(prefix[0][0].value or "") if prefix[0] else ""
            if layout["titleContains"] in title and actual == expected:
                matches.append(layout["sourceType"])
        require(len(matches) == 1, "LAYOUT_UNKNOWN", "标题或表头与三类固定格式不一致")
        return matches[0]
    finally:
        workbook.close()


def _revision(db, session_id, add_source_id, replace_source_id=None):
    prior = db.execute("SELECT id,revision_no,manifest_sha256 FROM data_revision WHERE session_id=? ORDER BY revision_no DESC LIMIT 1", (session_id,)).fetchone()
    prior_ids = [row[0] for row in db.execute("SELECT source_id FROM data_revision_source WHERE revision_id=? ORDER BY source_id", (prior[0],))] if prior else []
    source_ids = sorted(set(prior_ids) - ({replace_source_id} if replace_source_id else set()) | {add_source_id})
    placeholders = ",".join("?" for _ in source_ids)
    sources = db.execute(f"SELECT id,file_sha256,source_fingerprint FROM data_source WHERE id IN ({placeholders}) ORDER BY id", source_ids).fetchall()
    manifest = digest([{"id": r[0], "file": r[1], "fingerprint": r[2]} for r in sources])
    if prior and prior[2] == manifest:
        return prior[0]
    revision_id = uid("datarev_")
    db.execute("INSERT INTO data_revision VALUES (?,?,?,?,?,?)", (revision_id, session_id, 1 if not prior else prior[1] + 1, prior[0] if prior else None, manifest, now()))
    for source in sources:
        db.execute("INSERT INTO data_revision_source VALUES (?,?,?)", (revision_id, session_id, source[0]))
    return revision_id


def _publish(db, source_id, unit=None, basis=None, evidence=None, replace_source_id=None):
    source = db.execute("SELECT * FROM data_source WHERE id=?", (source_id,)).fetchone()
    require(source and source["state"] in ("STAGING", "NEEDS_UNIT_CONFIRMATION"), "SOURCE_STATE", "来源状态无法发布")
    fingerprint = digest({"file": source["file_sha256"], "name": source["file_name"],
                          "layout": source["layout_sha256"], "parser": PARSER_VERSION,
                          "skill": source["ingest_skill_sha256"], "unit": unit,
                          "basis": basis, "routing": source["unit_routing_policy_sha256"]})
    db.execute("""UPDATE data_source SET state='READY',source_fingerprint=?,origin_operating_unit=?,
        origin_unit_basis=?,origin_unit_evidence_json=?,published_at=? WHERE id=?""",
        (fingerprint, unit, basis, json.dumps(evidence, ensure_ascii=False) if evidence else None, now(), source_id))
    return _revision(db, source["session_id"], source_id, replace_source_id)


def import_workbook(store, session_id, path, gateway=None, expected_source_type=None):
    path = Path(path)
    require(path.suffix.lower() == ".xlsx" and path.is_file(), "FORMAT_UNSUPPORTED", "请选择 XLSX 文件")
    raw = path.read_bytes()
    _safe_xlsx(raw)
    bundle = bundled_catalog()
    with store.connect() as db:
        active_release(db)
        require(store.get("session", session_id, db), "NOT_FOUND", "对话不存在")
        prior = db.execute("""SELECT src.* FROM data_source src
            JOIN data_revision_source drs ON drs.source_id=src.id
            JOIN data_revision rev ON rev.id=drs.revision_id
            WHERE src.session_id=? AND src.file_sha256=? AND src.state='READY'
            AND rev.id=(SELECT id FROM data_revision WHERE session_id=? ORDER BY revision_no DESC LIMIT 1)
            LIMIT 1""", (session_id, sha(raw), session_id)).fetchone()
        if prior:
            revision = db.execute("SELECT id FROM data_revision WHERE session_id=? ORDER BY revision_no DESC LIMIT 1", (session_id,)).fetchone()
            return {"sourceId": prior["id"], "sourceType": prior["source_type"], "state": "READY", "rowCount": prior["row_count"], "revisionId": revision[0] if revision else None, "deduplicated": True}
        pending = db.execute("""SELECT * FROM data_source WHERE session_id=? AND file_sha256=?
            AND file_name=? AND state='NEEDS_UNIT_CONFIRMATION' LIMIT 1""",
            (session_id, sha(raw), path.name)).fetchone()
        if pending:
            return {"sourceId": pending["id"], "sourceType": pending["source_type"],
                    "state": pending["state"], "rowCount": pending["row_count"],
                    "revisionId": None, "deduplicated": True}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    try:
        require(len(workbook.worksheets) == 1 and workbook.worksheets[0].sheet_state == "visible", "WORKBOOK_SHEETS", "工作簿必须只有一个可见工作表")
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        prefix = [next(rows, ()) for _ in range(3)]
        matches = []
        for layout in bundle["layout"]["layouts"]:
            header = prefix[layout["headerRow"] - 1]
            actual = [str(c.value).strip() if c.value is not None else "" for c in header]
            expected = [c["header"] for c in layout["columns"]]
            title = str(prefix[0][0].value or "") if prefix[0] else ""
            if layout["titleContains"] in title and actual == expected:
                matches.append(layout)
        require(len(matches) == 1, "LAYOUT_UNKNOWN", "标题或表头与三类固定格式不一致")
        layout = matches[0]
        source_type = layout["sourceType"]
        require(expected_source_type is None or source_type == expected_source_type,
                "SKILL_TYPE_MISMATCH", "工作簿类型与入库 Skill 不一致")
        headers = [c["header"] for c in layout["columns"]]
        source_id, sheet_id = uid("source_"), uid("sheet_")
        blob_hash = store.blob(raw)
        unit, evidence = _semantic_unit(path.name, bundle["routing"], gateway) if source_type == "CANCELLATION" else (None, None)
        with store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO data_space VALUES (?,?)", (session_id, now()))
            db.execute("""INSERT INTO data_source
                (id,session_id,source_type,file_sha256,file_name,layout_version,layout_sha256,
                 parser_version,ingest_skill_id,ingest_skill_version,ingest_skill_sha256,state,
                 unit_routing_policy_sha256,origin_unit_evidence_json,imported_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, session_id, source_type, blob_hash, path.name, layout["id"], digest(layout),
                 PARSER_VERSION, SKILLS[source_type], "1.0", digest({"skill": SKILLS[source_type], "layout": layout}),
                 "STAGING", bundle["hashes"]["routing"] if source_type == "CANCELLATION" else None,
                 json.dumps(evidence, ensure_ascii=False) if evidence else None, now()))
            db.execute("INSERT INTO data_sheet VALUES (?,?,?,?,?,?)", (sheet_id, source_id, sheet.title, layout["headerRow"], digest(headers), 0))
            count, first_date, last_date = 0, None, None
            for excel_row, cells in enumerate(sheet.iter_rows(), 1):
                if excel_row <= layout["headerRow"]:
                    continue
                require(excel_row <= LIMITS["sheet_rows"], "FILE_LIMIT", "工作表行数超限")
                require(all(c.value is None or c.value == "" for c in cells[len(headers):]),
                        "LAYOUT_UNKNOWN", "固定表头之外存在数据")
                if all(cell.value is None or cell.value == "" for cell in cells):
                    continue
                values = {}
                for index, column in enumerate(layout["columns"]):
                    if index < len(cells):
                        try:
                            value = _cell(cells[index], column["type"])
                        except AppError as exc:
                            raise AppError(exc.code, f"第 {excel_row} 行第 {index + 1} 列：{exc.message}") from exc
                    else:
                        value = None
                    values[column["column"]] = value
                    if column["type"] == "TIME_HHMM":
                        values[column["column"].replace("_hhmm", "_minute")] = None if value is None else int(value[:2]) * 60 + int(value[3:5])
                if source_type == "SURGERY_DETAIL":
                    age = values.get("age_raw")
                    values["age_years"] = int(age) if age and re.fullmatch(r"\d{1,3}", age) else None
                row_hash = digest([c.value.isoformat() if isinstance(c.value, (date, datetime, time)) else c.value for c in cells])
                columns = ["sheet_id", "excel_row", *values, "row_sha256"]
                sql = f"INSERT INTO {TABLES[source_type]} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
                cursor = db.execute(sql, [sheet_id, excel_row, *values.values(), row_hash])
                if source_type == "CANCELLATION" and values["cancellation_categories_raw"]:
                    seen = set()
                    for tag in re.split(r"[,，、;；]", values["cancellation_categories_raw"]):
                        tag = tag.strip()
                        if tag and tag not in seen:
                            seen.add(tag)
                            db.execute("INSERT INTO cancellation_tag VALUES (?,?,?)", (cursor.lastrowid, len(seen), tag))
                date_value = values.get("operation_date") or values.get("record_date")
                if date_value:
                    first_date = min(first_date, date_value) if first_date else date_value
                    last_date = max(last_date, date_value) if last_date else date_value
                count += 1
            db.execute("UPDATE data_sheet SET row_count=? WHERE id=?", (count, sheet_id))
            db.execute("UPDATE data_source SET row_count=?,observed_start_date=?,observed_end_date=? WHERE id=?",
                       (count, first_date, last_date, source_id))
            require(count > 0, "WORKBOOK_EMPTY", "工作簿没有数据行")
            if source_type == "CANCELLATION" and not unit:
                db.execute("UPDATE data_source SET state='NEEDS_UNIT_CONFIRMATION' WHERE id=?", (source_id,))
                revision = None
                state = "NEEDS_UNIT_CONFIRMATION"
            else:
                revision = _publish(db, source_id, unit, "FILENAME_SEMANTIC" if unit else None, evidence)
                state = "READY"
        return {"sourceId": source_id, "sourceType": source_type, "state": state,
                "rowCount": count, "revisionId": revision, "unitEvidence": evidence if source_type == "CANCELLATION" else None}
    finally:
        workbook.close()


def confirm_unit(store, session_id, source_id, unit_code):
    require(unit_code in ("OR_1", "OR_2", "OR_3"), "INVALID_UNIT", "请选择一号、二号或三号手术室")
    with store.transaction() as db:
        source = db.execute("SELECT * FROM data_source WHERE id=? AND session_id=?", (source_id, session_id)).fetchone()
        require(source and source["source_type"] == "CANCELLATION" and source["state"] == "NEEDS_UNIT_CONFIRMATION", "SOURCE_STATE", "来源无需确认手术室")
        evidence = json.loads(source["origin_unit_evidence_json"])
        evidence["manualSelection"] = {"unit": unit_code, "at": now()}
        revision = _publish(db, source_id, unit_code, "USER_CONFIRMED", evidence)
        return {"sourceId": source_id, "state": "READY", "revisionId": revision, "originOperatingUnit": unit_code}


def correct_unit(store, session_id, source_id, unit_code):
    require(unit_code in ("OR_1", "OR_2", "OR_3"), "INVALID_UNIT", "请选择一号、二号或三号手术室")
    with store.transaction() as db:
        source = db.execute("SELECT * FROM data_source WHERE id=? AND session_id=? AND source_type='CANCELLATION' AND state='READY'", (source_id, session_id)).fetchone()
        require(source, "SOURCE_STATE", "只允许更正已发布的本对话退单来源")
        require(source["origin_operating_unit"] != unit_code, "SOURCE_STATE", "手术室没有变化")
        current = db.execute("SELECT id FROM data_revision WHERE session_id=? ORDER BY revision_no DESC LIMIT 1", (session_id,)).fetchone()
        require(current and db.execute("SELECT 1 FROM data_revision_source WHERE revision_id=? AND source_id=?", (current[0], source_id)).fetchone(),
                "SOURCE_STATE", "只能更正当前数据版本中的来源")
        new_id = uid("source_")
        fields = [row[1] for row in db.execute("PRAGMA table_info(data_source)") if row[1] not in
                  ("id", "source_fingerprint", "state", "origin_operating_unit", "origin_unit_basis",
                   "origin_unit_evidence_json", "imported_at", "published_at")]
        columns = ["id", *fields, "state", "origin_unit_evidence_json", "imported_at"]
        evidence = json.loads(source["origin_unit_evidence_json"])
        evidence["manualCorrection"] = {"fromSourceId": source_id, "fromUnit": source["origin_operating_unit"],
                                        "toUnit": unit_code, "at": now()}
        db.execute(f"INSERT INTO data_source ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                   [new_id, *(source[name] for name in fields), "STAGING", json.dumps(evidence, ensure_ascii=False), now()])
        for sheet in db.execute("SELECT * FROM data_sheet WHERE source_id=?", (source_id,)).fetchall():
            new_sheet = uid("sheet_")
            db.execute("INSERT INTO data_sheet VALUES (?,?,?,?,?,?)", (new_sheet, new_id, sheet["sheet_name"],
                       sheet["header_row"], sheet["header_sha256"], sheet["row_count"]))
            columns = [row[1] for row in db.execute("PRAGMA table_info(cancellation_row)") if row[1] not in ("id", "sheet_id")]
            db.execute(f"INSERT INTO cancellation_row (sheet_id,{','.join(columns)}) SELECT ?,{','.join(columns)} FROM cancellation_row WHERE sheet_id=?",
                       (new_sheet, sheet["id"]))
            db.execute("""INSERT INTO cancellation_tag(cancellation_row_id,position,tag)
                SELECT new.id,tag.position,tag.tag FROM cancellation_tag tag
                JOIN cancellation_row old ON old.id=tag.cancellation_row_id
                JOIN cancellation_row new ON new.sheet_id=? AND new.excel_row=old.excel_row
                WHERE old.sheet_id=?""", (new_sheet, sheet["id"]))
        revision = _publish(db, new_id, unit_code, "USER_CONFIRMED", evidence, source_id)
        return {"sourceId": new_id, "replacesSourceId": source_id, "state": "READY",
                "revisionId": revision, "originOperatingUnit": unit_code}


def list_sources(store, session_id):
    store.get("session", session_id)
    with store.connect() as db:
        revision = db.execute("SELECT * FROM data_revision WHERE session_id=? ORDER BY revision_no DESC LIMIT 1", (session_id,)).fetchone()
        rows = db.execute("""SELECT id,source_type,file_name,state,row_count,origin_operating_unit,
            origin_unit_basis,observed_start_date,observed_end_date FROM data_source
            WHERE session_id=? ORDER BY imported_at,id""", (session_id,)).fetchall()
        active = {row[0] for row in db.execute("SELECT source_id FROM data_revision_source WHERE revision_id=?", (revision["id"],))} if revision else set()
        return {"revision": dict(revision) if revision else None,
                "sources": [dict(row) | {"current": row["id"] in active} for row in rows]}
