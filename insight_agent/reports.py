"""Versioned exports built exclusively from committed, approved results."""
from __future__ import annotations

import io
import zipfile

from .core import VERSION, digest, dumps, now, require, sha, uid
from .files import convert_document, converter_path
from .models import redact_text


def _redact_definition(value):
    """Keep the published metric definition in exports while masking text fields."""
    if isinstance(value, dict):
        return {key: _redact_definition(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_definition(child) for child in value]
    return redact_text(value) if isinstance(value, str) else value


def _analysis_scope(parameters):
    return "ALL_DATA" if parameters.get("scope") == "ALL_DATA" else "DATE_WINDOW"


def _scope_label(parameters):
    if _analysis_scope(parameters) == "ALL_DATA":
        return "当前会话全部数据文件"
    return f"{parameters.get('date_start', '')} 至 {parameters.get('date_end', '')}"


def approved_values(run):
    require(run.get("committed") and run["mode"] != "PLAN" and run.get("review", {}).get("status") == "PASS", "REPORT_NOT_ELIGIBLE", "只有已提交且复核通过的分析结果可生成报告")
    rows = []
    parameters = run["contract"]["parameters"]
    analysis_scope = _analysis_scope(parameters)
    for metric in run["results"]:
        if metric["outcome"] != "VALUE":
            continue
        for group in metric["groups"]:
            if group["outcome"] == "VALUE":
                evidence = [{k: (redact_text(v) if k == "quote" else v) for k, v in e.items()} for e in metric.get("evidence", [])]
                rows.append({
                    "metricId": metric["metricId"],
                    "name": redact_text(metric["name"]),
                    "unit": metric["unit"],
                    "group": {k: redact_text(str(v)) for k, v in group.get("group", {}).items()},
                    "value": group["displayValue"],
                    "exactNumerator": group["exactNumerator"],
                    "exactDenominator": group["exactDenominator"],
                    "numerator": group.get("numerator"),
                    "denominator": group.get("denominator"),
                    "audit": metric.get("audit", {}),
                    "evidence": evidence,
                    "parameters": parameters,
                    "analysisScope": analysis_scope,
                    "definitionOrder": metric.get("definitionOrder"),
                    "definition": _redact_definition(metric.get("definition", {})),
                })
    # The definition document is the source of display order. Keep that order
    # stable even when the calculation engine returns groups in another order.
    rows.sort(key=lambda row: (row["definitionOrder"] is None, row["definitionOrder"] or 0, row["metricId"], dumps(row["group"])))
    require(rows or run.get("filledReport"), "NO_REPORTABLE_RESULTS", "本次没有可生成报告的指标，不创建空白报告")
    return rows


def docx_bytes(run, rows, title):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.2)
    section.left_margin = section.right_margin = Cm(2.2)
    for name in ("Normal", "Title", "Heading 1", "Heading 2"):
        style = document.styles[name]
        style.font.name = "Microsoft YaHei"
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.color.rgb = RGBColor(0, 0, 0)
    document.styles["Normal"].font.size = Pt(10.5)
    document.styles["Normal"].paragraph_format.space_after = Pt(7)
    document.add_paragraph(title, "Title")
    document.add_paragraph("版面核验前为草稿；核验状态和检查人请见本地导出记录。")
    parameters = run["contract"]["parameters"]
    document.add_paragraph(f"统计范围：{_scope_label(parameters)}。数值须通过本地核验；待补充项保留在原文位置。")
    for name, value in parameters.items():
        document.add_paragraph("查询参数 " + name + "：" + (value if isinstance(value, str) else dumps(value)))
    document.add_paragraph("本报告由本地确定性计算引擎生成。数值按已发布指标口径计算，展示精度不影响底层精确结果。报告不包含患者明细。")
    filled = run.get("filledReport")
    if filled:
        document.add_heading("按指标说明回填的内容", level=1)
        document.add_paragraph(filled["scopeNote"])
        for paragraph in filled["paragraphs"]:
            document.add_paragraph(redact_text(paragraph["text"]))
        document.add_heading("待补充清单", level=1)
        for gap in filled["gaps"]:
            document.add_paragraph(redact_text(gap["name"] + "：" + str(gap.get("reason") or gap.get("code"))) + "（" + gap["locator"] + "）")
    document.add_heading("指标结果与审计附录" if filled else "指标结果", level=1)
    table = document.add_table(rows=1, cols=4)
    table.style = "Light Shading Accent 1"
    for cell, value in zip(table.rows[0].cells, ("指标", "分组", "结果", "单位")):
        cell.text = value
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, (row["name"], " / ".join(str(v) for v in row["group"].values()) or "总体", row["value"], row["unit"])):
            cell.text = value
        no_split = OxmlElement("w:cantSplit")
        cells[0]._tc.getparent().get_or_add_trPr().append(no_split)
    document.add_heading("数据与口径依据", level=1)
    metric_ids = set()
    for row in rows:
        if row["metricId"] in metric_ids:
            continue
        metric_ids.add(row["metricId"])
        document.add_heading(row["name"], level=2)
        document.add_paragraph(f"指标定义顺序：{row.get('definitionOrder') or '—'}；分析范围：{row.get('analysisScope', 'DATE_WINDOW')}。")
        document.add_paragraph("指标说明文件中的定义：" + dumps(row.get("definition", {})))
        audit = row["audit"]
        document.add_paragraph(f"输入 {audit.get('inputRows', 0)} 行，范围内 {audit.get('inWindowRows', 0)} 行，纳入 {audit.get('includedRows', 0)} 行；排除 {audit.get('excludedRows', 0)} 行，缺失 {audit.get('missingRows', 0)} 行，非法 {audit.get('invalidRows', 0)} 行。")
        for evidence in row["evidence"]:
            document.add_paragraph(evidence["quote"])
            p = document.add_paragraph("原文定位 " + evidence["locator"])
            for item in p.runs:
                item.font.size = Pt(9)
    document.add_heading("精确值与比率明细", level=1)
    for row in rows:
        document.add_paragraph(row["name"] + " / " + (" / ".join(row["group"].values()) or "总体") + "：精确值 " + row["exactNumerator"] + "/" + row["exactDenominator"] + "；分子 " + dumps(row["numerator"]) + "；分母 " + dumps(row["denominator"]))
    document.add_heading("追溯信息", level=1)
    document.add_paragraph("运行编号 " + run["id"])
    document.add_paragraph("合同摘要 " + run["contractHash"])
    document.add_paragraph("结果摘要 " + run["resultDigest"])
    footer = section.footer.paragraphs[0]
    footer.text = "Insight Agent  数据指标分析"
    footer.add_run(" · 第 ")
    for field_name, suffix in (("PAGE", " 页 / 共 "), ("NUMPAGES", " 页")):
        field = OxmlElement("w:fldSimple")
        field.set(qn("w:instr"), field_name)
        footer._p.append(field)
        footer.add_run(suffix)
    stream = io.BytesIO()
    document.save(stream)
    data = stream.getvalue()
    check = Document(io.BytesIO(data))
    actual_rows = [[c.text for c in r.cells] for r in check.tables[0].rows[1:]]
    expected_rows = [[r["name"], " / ".join(r["group"].values()) or "总体", r["value"], r["unit"]] for r in rows]
    text = "\n".join(p.text for p in check.paragraphs)
    if filled:
        require(all(redact_text(p["text"]) in text for p in filled["paragraphs"]), "REPORT_CONTENT_QA_FAILED", "回填正文存在遗漏")
    require(actual_rows == expected_rows and _scope_label(parameters) in text and all(e["quote"] in text and e["locator"] in text for r in rows for e in r["evidence"]) and all(r["exactNumerator"] + "/" + r["exactDenominator"] in text for r in rows) and all(str(v) in text for v in parameters.values() if isinstance(v, str)), "REPORT_CONTENT_QA_FAILED", "报告内容与运行结果不一致")
    return data


def spreadsheet_bytes(rows, filled=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "指标结果"
    sheet.append(["指标ID", "指标", "分组", "显示值", "单位", "精确分子", "精确分母", "比率分子", "比率分母", "窗口参数", "证据ID与定位", "指标定义顺序", "分析范围", "指标说明文件定义", "审计信息"])
    for result in rows:
        values = [result["metricId"], result["name"], dumps(result["group"]), result["value"], result["unit"], result["exactNumerator"], result["exactDenominator"], dumps(result["numerator"]), dumps(result["denominator"]), dumps(result["parameters"]), dumps([{k: e.get(k) for k in ("evidenceId", "documentSha256", "locator", "quoteSha256")} for e in result["evidence"]]), result.get("definitionOrder"), result.get("analysisScope"), dumps(result.get("definition", {})), dumps(result.get("audit", {}))]
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            cell.data_type = "s"  # Never allow formula injection or float coercion.
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="6757C8")
    for column in "ABCDEFGHIJKLMNO":
        sheet.column_dimensions[column].width = 24 if column != "B" else 44
    if filled:
        content = workbook.create_sheet("按说明回填正文")
        content.append(["原文位置", "回填内容"])
        content.append(["统计范围说明", filled["scopeNote"]])
        for paragraph in filled["paragraphs"]:
            content.append([paragraph["locator"], redact_text(paragraph["text"])])
        gaps = workbook.create_sheet("待补充清单")
        gaps.append(["待填位置ID", "原文位置", "指标", "原因代码", "具体原因"])
        for gap in filled["gaps"]:
            gaps.append([gap["slotId"], gap["locator"], redact_text(gap["name"]), gap.get("code"), redact_text(gap.get("reason") or gap.get("code") or "待补充")])
        for extra in (content, gaps):
            extra.freeze_panes = "A2"
            extra.column_dimensions["A"].width = 30
            extra.column_dimensions["B"].width = 90 if extra is content else 30
            extra.column_dimensions["E"].width = 90
            for row in extra:
                for cell in row:
                    if cell.value is not None:
                        cell.data_type = "s"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def export_run(store, run_id, format, title="数据指标分析报告", job=None):
    require(format in ("docx", "pdf", "xlsx", "csv", "json", "zip"), "EXPORT_FORMAT", "不支持此导出格式")
    run = store.get("run", run_id)
    rows = approved_values(run)
    if job:
        existing = next((a for a in store.list("artifact") if a.get("jobId") == job["id"] and a["format"] == format), None)
        if existing:
            return existing
    artifact_id = uid("artifact_")
    layout = "NOT_APPLICABLE"
    render_hash = None
    source_docx_hash = None
    source_docx_id = uid("artifact_") if format == "pdf" else None
    renderer = None
    if format in ("docx", "pdf"):
        data = docx_bytes(run, rows, title)
        source_docx_hash = store.blob(data)
        layout = "LAYOUT_UNVERIFIED"
        if converter_path():
            rendered = convert_document(data, ".docx", "pdf")
            render_hash = store.blob(rendered)
            layout = "RENDERED_REVIEW_REQUIRED"
            from .files import converter_version
            renderer = converter_version()
        if format == "pdf":
            require(render_hash, "CONVERTER_UNAVAILABLE", "PDF 导出需安装 LibreOffice")
            data = store.read_blob(render_hash)
    elif format == "xlsx":
        data = spreadsheet_bytes(rows, run.get("filledReport"))
    elif format == "csv":
        import csv
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["metricId", "name", "group", "value", "unit", "exactNumerator", "exactDenominator", "numerator", "denominator", "parameters", "evidence", "definitionOrder", "analysisScope", "definition", "audit", "outcome", "reason", "locator", "filledContent"])
        for row in rows:
            values = [row["metricId"], row["name"], dumps(row["group"]), row["value"], row["unit"], row["exactNumerator"], row["exactDenominator"], dumps(row["numerator"]), dumps(row["denominator"]), dumps(row["parameters"]), dumps(row["evidence"]), row.get("definitionOrder"), row.get("analysisScope"), dumps(row.get("definition", {})), dumps(row.get("audit", {}))]
            values += ["VALUE", "", "", ""]
            writer.writerow(["'" + str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in values])
        if run.get("filledReport"):
            filled = run["filledReport"]
            for gap in filled["gaps"]:
                values = [gap["slotId"], redact_text(gap["name"])] + [""] * 13 + ["SKIP", redact_text(gap.get("reason") or gap.get("code") or "待补充"), gap["locator"], ""]
                writer.writerow(["'" + str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in values])
            for paragraph in [{"locator": "scope", "text": filled["scopeNote"]}] + filled["paragraphs"]:
                values = [""] * 15 + ["CONTENT", "", paragraph["locator"], redact_text(paragraph["text"])]
                writer.writerow(["'" + str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in values])
        data = output.getvalue().encode("utf-8-sig")
    elif format == "json":
        data = dumps(export_summary(run, rows)).encode()
    else:
        stream = io.BytesIO()
        summary = export_summary(run, rows)
        contract = run["contract"]
        contract_summary = {k: contract[k] for k in ("schemaVersion", "baselineHash", "definitionManifestHash", "engineVersion", "operatorVersion", "parameters", "reviewPolicy")}
        contract_summary["rules"] = [{"metricId": c["id"], "status": c["status"], "ruleDigest": digest(c["rule"]) if c.get("rule") else None, "operator": c.get("rule", {}).get("expression", {}).get("operator"), "entityType": c.get("rule", {}).get("entityType")} for c in contract["candidates"]]
        processing = [{"metricId": r["metricId"], "definitionOrder": r.get("definitionOrder"), "definition": _redact_definition(r.get("definition", {})), "outcome": r["outcome"], "reasonCode": r.get("reasonCode"), "audit": r.get("audit", {}), "groups": [{"group": {k: redact_text(str(v)) for k, v in g.get("group", {}).items()}, "outcome": g["outcome"], "reasonCode": g.get("reasonCode")} for g in r.get("groups", [])]} for r in run["results"]]
        members = {
            "README.txt": "分析包只包含汇总、参数、规则摘要、处理清单与证据引用；不包含原始工作簿、患者行数据、模型配置或完整工作空间。证据文字经脱敏，原文 hash 供本机核对。共享前仍应核对自定义名称与引用内容。\n".encode("utf-8"),
            "summary.json": dumps(summary).encode("utf-8"),
            "contract-summary.json": dumps(contract_summary).encode("utf-8"),
            "processing-list.json": dumps(processing).encode("utf-8"),
            "audit.json": dumps(summary["review"]).encode("utf-8"),
            "results.xlsx": spreadsheet_bytes(rows, run.get("filledReport")),
        }
        manifest = {"schemaVersion": "analysis-package/1.0", "appVersion": VERSION, "runId": run_id, "scope": "aggregate-only", "files": [{"name": name, "sha256": sha(body), "bytes": len(body)} for name, body in members.items()]}
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, body in members.items():
                archive.writestr(name, body)
            archive.writestr("manifest.json", dumps(manifest))
        data = stream.getvalue()
    hash = store.blob(data)
    file_name = artifact_id + "." + format
    path = store.home / "exports" / file_name
    with store.transaction() as db:
        if job:
            lease = db.execute("SELECT lease,cancel,status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            require(lease and lease[0] == job["lease"] and lease[2] == "RUNNING", "LEASE_LOST", "导出租约已失效")
            require(not lease[1], "CANCELLED", "导出已取消，未发布产物")
        provenance = {"templateId": "builtin", "templateVersion": "1.0", "fontFamily": "Microsoft YaHei", "renderer": renderer, "appVersion": VERSION}
        artifact = store.put("artifact", {"id": artifact_id, "runId": run_id, "jobId": job["id"] if job else None, "sessionId": run["sessionId"], "format": format, "fileName": file_name, "sha256": hash, "renderHash": render_hash, "sourceDocxHash": source_docx_hash, "sourceDocxArtifactId": source_docx_id, "provenance": provenance, "contentQA": "PASS", "layoutQA": layout, "status": "DRAFT" if layout != "NOT_APPLICABLE" else "READY", "createdAt": now()}, db)
        if source_docx_id:
            source = {**artifact, "id": source_docx_id, "format": "docx", "fileName": source_docx_id + ".docx", "sha256": source_docx_hash, "sourceDocxArtifactId": None}
            store.put("artifact", source, db)
    # Managed blob is authoritative; this convenience copy can be regenerated.
    path.write_bytes(data)
    return artifact


def export_summary(run, rows):
    result = {"schemaVersion": "aggregate-export/1.0", "runId": run["id"], "contractHash": run["contractHash"], "resultDigest": run["resultDigest"], "definitionManifestHash": run["contract"]["definitionManifestHash"], "engineVersion": run["contract"]["engineVersion"], "analysisScope": _analysis_scope(run["contract"]["parameters"]), "parameters": run["contract"]["parameters"], "definitionOrder": [{"metricId": r["metricId"], "definitionOrder": r.get("definitionOrder"), "definition": _redact_definition(r.get("definition", {}))} for r in run["results"]], "results": rows, "skips": [{"metricId": r["metricId"], "definitionOrder": r.get("definitionOrder"), "definition": _redact_definition(r.get("definition", {})), "reasonCode": r.get("reasonCode"), "reason": redact_text(r.get("reason", ""))} for r in run["results"] if r["outcome"] == "SKIP"], "review": {k: run["review"].get(k) for k in ("policy", "status", "resultHash", "contractHash", "mode")}}
    if run.get("filledReport"):
        result["filledReport"] = _redact_definition(run["filledReport"])
    return result


def confirm_layout(store, artifact_id, actor):
    require(actor.strip(), "REVIEW_REQUIRED", "请填写页面检查人")
    with store.transaction() as db:
        artifact = store.get("artifact", artifact_id, db)
        require(artifact["contentQA"] == "PASS" and artifact.get("renderHash"), "LAYOUT_UNVERIFIED", "缺少渲染预览，不能标记正式报告")
        artifact.update(layoutQA="PASS", status="READY", layoutReviewer=actor, reviewedAt=now())
        return store.put("artifact", artifact, db)
