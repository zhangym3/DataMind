"""Versioned document fill plans. Models classify slots; only local code reads values.

The old metric AST remains supported. A fill plan is a separate, closed contract,
not a less strictly validated AST. Unsupported semantics remain visible gaps.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from jsonschema import Draft202012Validator

from .core import AppError, checkpoint, digest, now, require, sha
from .files import snapshot_for

VERSION = "document-fill/1.1"
POLICY = "local-source-policy/1.1"
# Do not treat a threshold, formula, fixed department name or 'X' in a word as a slot.
PLACEHOLDER = re.compile(r"[XxＸｘ]+年[XxＸｘ]+月[XxＸｘ]+日|(?<![A-Za-z0-9])[XxＸｘ]+(?![A-Za-z0-9])")
INSTRUCTION = (
    "说明是报告填报模板。逐个识别 slots 的待填指标，不能输出指标数值、代码或完整规则。"
    "阅读全部 nodes 关联章节、公式、例外和前文日期；slot 给出原文字符位置，同一段的不同位置必须分别理解。"
    "name 是该位置完整指标名（不含占位符），包括实际/计划、首台/连台和时点等限制。"
    "仅简单实际总手术量用 SURGERY_COUNT，简单实际急诊量用 EMERGENCY_COUNT；按材料手术日期逐日统计。"
    "入室至麻醉/麻醉至开刀严格超过给定分钟的患者人数可用 DURATION_PATIENTS，保留阈值。"
    "首台/连台子集、计划快照、比率、平均最晚时间、接台时点等使用 UNRESOLVED，具体说明尚需的规则或数据。"
    "READ_SUMMARY 仅表示查找口径相同的已有汇总值；不能从历史报告猜值。"
    "room 仅在该位置明确指定第一/第二/第三手术室时填写。只有 DURATION_PATIENTS 可以填 start/end/minutes；"
    "UNRESOLVED 即使原文包含时差，也必须 start=空字符串、end=空字符串、minutes=0，已知阈值可写在 reason。"
    "items 恰好覆盖给定 slots，每项只返回 schema 的8个字段。日期、科室文字在本地另行处理。"
)
REPAIR_INSTRUCTION = "上一批未通过本地语义核验：{error}。请纠正本批各项并仍严格返回八字段。仅 DURATION_PATIENTS 可携带时差，其余方法 start/end 为空、minutes=0。不能把简单可执行项改为待补充来绕过错误。"
METHODS = ["SURGERY_COUNT", "EMERGENCY_COUNT", "DURATION_PATIENTS", "READ_SUMMARY", "UNRESOLVED"]
ITEM_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["slot", "name", "method", "room", "start", "end", "minutes", "reason"],
               "properties": {"slot": {"type": "string", "maxLength": 80},
                              "name": {"type": "string", "minLength": 1, "maxLength": 120},
                              "method": {"enum": METHODS},
                              "room": {"enum": ["", "第一手术室", "第二手术室", "第三手术室"]},
                              "start": {"enum": ["", "entry", "anesthesia"]},
                              "end": {"enum": ["", "anesthesia", "incision"]},
                              "minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
                              "reason": {"type": "string", "maxLength": 300}}}
OUTPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
                 "properties": {"items": {"type": "array", "maxItems": 4, "items": ITEM_SCHEMA}}}


def template(nodes):
    paragraphs, slots = [], []
    heading = temporal = lead = ""
    for n in nodes:
        if not PLACEHOLDER.search(n["text"]) and len(n["text"]) < 30:
            heading, lead = n["text"], ""
        if re.match(r"[〔【\[]?[XxＸｘ]+年", n["text"]):
            temporal, lead = n["text"], ""
        room_line = bool(re.search(r"第[一二三]手术室", n["text"])) and not any(t in n["text"] for t in ("急诊", "平均", "患者", "接台", "点"))
        paragraph = {"locator": n["locator"], "text": n["text"], "slots": []}
        for m in PLACEHOLDER.finditer(n["text"]):
            after = n["text"][m.end():]
            kind = "DATE" if "年" in m.group() else "TEXT" if after.startswith(("科", "点")) or (len(m.group()) > 1 and not after.startswith(("台", "人", "%", "％"))) else "NUMBER"
            slot = {"id": "slot_" + digest([n["locator"], m.start(), m.end()])[:16],
                    "locator": n["locator"], "startOffset": m.start(), "endOffset": m.end(),
                    "placeholder": m.group(), "kind": kind, "context": n["text"],
                    "semanticContext": "\n".join(dict.fromkeys(v for v in (heading, temporal, lead if room_line else "", n["text"]) if v))}
            slots.append(slot)
            paragraph["slots"].append(slot["id"])
        paragraphs.append(paragraph)
        if paragraph["slots"] and not room_line:
            lead = n["text"]
    require(len(slots) <= 200, "FILE_LIMIT", "说明文档的待填位置超过上限")
    return {"version": VERSION, "paragraphs": paragraphs, "slots": slots}


def clause(slot):
    """Bind operator checks to this slot, rather than another number in its paragraph."""
    before = slot["context"][:slot["startOffset"]]
    after = slot["context"][slot["endOffset"]:]
    return re.split(r"[，,；;。\n]", before)[-1] + slot["placeholder"] + re.split(r"[，,；;。\n]", after)[0]


def expected_unit(slot):
    after = slot["context"][slot["endOffset"]:]
    if after.startswith(("台", "人", "%", "％")):
        return "%" if after[0] == "％" else after[0]
    local = clause(slot)
    if "率" in local:
        return "%"
    if "人数" in local or "患者" in local:
        return "人"
    if "台" in local or "手术量" in local:
        return "台"
    return None


def validate_intent(slot, item):
    """A valid JSON object is not sufficient evidence for an executable operator."""
    require(not list(Draft202012Validator(ITEM_SCHEMA).iter_errors(item)) and item["slot"] == slot["id"],
            "MODEL_SCHEMA_INVALID", "待填项结构或位置无效")
    require(not PLACEHOLDER.search(item["name"]), "MODEL_SCHEMA_INVALID", "指标名称不能仍包含占位符")
    local, context = clause(slot), slot["semanticContext"]
    room = next((r for r in ITEM_SCHEMA["properties"]["room"]["enum"] if r and r in local), "")
    require(item["room"] == room, "MODEL_SEMANTIC_INVALID", "待填位置的手术室维度与原文不一致")
    method = item["method"]
    if method != "DURATION_PATIENTS":
        require(not item["start"] and not item["end"] and item["minutes"] == 0,
                "MODEL_SEMANTIC_INVALID", "非时差指标包含时差条件")
    if method == "UNRESOLVED":
        require(item["reason"].strip(), "MODEL_SEMANTIC_INVALID", "待补充项必须说明具体原因")
    if method in ("SURGERY_COUNT", "EMERGENCY_COUNT", "DURATION_PATIENTS"):
        require(not any(t in context for t in ("拟做", "计划", "首台", "连台", "接台", "平均", "率", "退单")),
                "MODEL_SEMANTIC_INVALID", "复合口径不能按简单计数执行")
    if method in ("SURGERY_COUNT", "EMERGENCY_COUNT"):
        require(expected_unit(slot) == "台" and "手术" in context and ("急诊" in local) == (method == "EMERGENCY_COUNT"),
                "MODEL_SEMANTIC_INVALID", "总手术量与急诊量的取数方式不符合该位置原文")
    if method == "DURATION_PATIENTS":
        pair = ("entry", "anesthesia") if re.search(r"入室|进入手术室|进入术间", local) else ("anesthesia", "incision") if "麻醉" in local and re.search(r"开刀|手术开始", local) else None
        require(expected_unit(slot) == "人" and (item["start"], item["end"]) == pair
                and re.search(r"(?:[＞>]|超过|大于)\s*" + str(item["minutes"]) + r"\s*分钟", local),
                "MODEL_SEMANTIC_INVALID", "时差方向、人数或严格大于阈值与该位置原文不一致")


def progress(store, definition_id, budget_id, phase, **data):
    checkpoint()
    with store.transaction() as db:
        store.event(db, definition_id, "definition.progress", {"jobId": budget_id, "definitionId": definition_id,
                    "phase": phase, **data})


def extract_plan(store, draft, gateway, budget_id):
    """Persist successful small batches before proceeding; never publish partial JSON."""
    from .models import Gateway, public_profile, redact_text
    from .skills import runtime_manifest
    nodes = [{"locator": n["locator"], "text": n["text"]} for n in draft["nodes"]]
    require(not draft.get("amendments"), "REPORT_AMENDMENT_UNSUPPORTED", "回填文档的补充条款请合入 Word 后重新上传，以免正文与规则不一致")
    require(all(redact_text(n["text"]) == n["text"] for n in nodes), "SENSITIVE_DEFINITION", "指标说明含敏感标识，请先脱敏后在线解析")
    plan = template(nodes)
    profile = public_profile(gateway.profile()) if isinstance(gateway, Gateway) else {"id": "test-adapter"}
    if isinstance(gateway, Gateway):
        gateway.frozen_profile = profile
    fingerprint = digest({"document": draft["documentHash"], "nodes": nodes, "runtime": runtime_manifest(),
                          "profile": profile, "schema": OUTPUT_SCHEMA, "instruction": INSTRUCTION, "version": VERSION})
    attempt_id = "fill_" + digest([draft["id"], fingerprint])[:32]
    previous = next((a for a in store.list("fill_attempt") if a["id"] == attempt_id), None)
    attempt = previous or {"id": attempt_id, "definitionId": draft["id"], "fingerprint": fingerprint,
                           "budgetId": budget_id, "batches": {}, "profile": profile}
    checkpoint()
    # Persist before the first network call: a failed first batch must not reset budgets.
    with store.transaction() as db:
        current = store.get("definition", draft["id"], db)
        require(current["revision"] == draft["revision"] and current["status"] == "DRAFT", "REVISION_CONFLICT", "说明在解析期间发生变化")
        store.put("fill_attempt", attempt, db)
    # A resumed generation retains its cumulative budget, even under a new job lease.
    model_budget = attempt["budgetId"]
    all_slots = plan["slots"]
    pending = [s for s in all_slots if s["kind"] == "NUMBER"]
    progress(store, draft["id"], budget_id, "CATALOG", slots=len(all_slots), total=len(pending), completed=0,
             nodeCount=len(nodes), unit="slots")
    parsed = {}

    def batch(items, correction=None):
        checkpoint()
        batch_id = digest([s["id"] for s in items])
        if batch_id in attempt["batches"]:
            parsed.update({v["slot"]: v for v in attempt["batches"][batch_id]})
            progress(store, draft["id"], budget_id, "RESUMED", completed=len(parsed), total=len(pending),
                     batchSize=len(items), unit="slots")
            return
        progress(store, draft["id"], budget_id, "EXTRACTING", completed=len(parsed), total=len(pending), batchSize=len(items),
                 batchLocators=list(dict.fromkeys(s["locator"] for s in items)), unit="slots")
        try:
            instruction = INSTRUCTION + (REPAIR_INSTRUCTION.format(error=correction) if correction else "")
            response = gateway.call("fill_plan", {"instruction": instruction, "nodes": nodes,
                "slots": [{k: s[k] for k in ("id", "locator", "startOffset", "endOffset")} for s in items]}, OUTPUT_SCHEMA, model_budget)
            require(not list(Draft202012Validator(OUTPUT_SCHEMA).iter_errors(response)), "MODEL_SCHEMA_INVALID", "待填项结构不完整")
            values = response["items"]
            require(len(values) == len(items) and {v["slot"] for v in values} == {s["id"] for s in items},
                    "DOCUMENT_COVERAGE_INCOMPLETE", "当前批次的待填位置未完整对账")
            for s in items:
                validate_intent(s, next(v for v in values if v["slot"] == s["id"]))
        except AppError as exc:
            if exc.code in ("MODEL_OUTPUT_TRUNCATED", "MODEL_TIMEOUT") and len(items) > 1:
                progress(store, draft["id"], budget_id, "SPLITTING", completed=len(parsed), total=len(pending),
                         batchSize=len(items), errorCode=exc.code)
                middle = len(items) // 2
                batch(items[:middle])
                batch(items[middle:])
                return
            if exc.code == "MODEL_SEMANTIC_INVALID" and correction is None:
                progress(store, draft["id"], budget_id, "REPAIRING", completed=len(parsed), total=len(pending),
                         batchSize=len(items), errorCode=exc.code, message=exc.message)
                batch(items, correction=exc.message)
                return
            raise
        attempt["batches"][batch_id] = values
        checkpoint()
        with store.transaction() as db:
            # Guard both optimistic document revision and current source binding.
            current = store.get("definition", draft["id"], db)
            require(current["revision"] == draft["revision"] and current["status"] == "DRAFT", "REVISION_CONFLICT", "说明在解析期间发生变化")
            store.put("fill_attempt", attempt, db)
        parsed.update({v["slot"]: v for v in values})
        progress(store, draft["id"], budget_id, "BATCH_DONE", completed=len(parsed), total=len(pending),
                 batchSize=len(items), names=[v["name"] for v in values], unit="slots")

    for i in range(0, len(pending), 4):
        batch(pending[i:i + 4])
    candidates = []
    for slot in all_slots:
        item = parsed.get(slot["id"], {"slot": slot["id"], "name": "统计日期" if slot["kind"] == "DATE" else "科室或时间文字",
                    "method": "UNRESOLVED", "room": next((r for r in ITEM_SCHEMA["properties"]["room"]["enum"] if r and r in clause(slot)), ""), "start": "", "end": "", "minutes": 0,
                    "reason": "日期须随统计数据确认，不以系统昨日代替" if slot["kind"] == "DATE" else "缺少明确的科室/时间文字取值规则"})
        validate_intent(slot, item)
        slot["plan"] = item
        candidates.append({"id": slot["id"], "name": item["name"], "status": "REPORT", "plan": item,
                           "locators": [slot["locator"]]})
    coverage = [{"locator": n["locator"], "disposition": "CANDIDATE" if any(s["locator"] == n["locator"] for s in all_slots) else "CONTEXT",
                 "candidateIds": [s["id"] for s in all_slots if s["locator"] == n["locator"]],
                 "reason": "待填位置逐项登记；其余原文作为章节、口径或规则保留"} for n in nodes]
    progress(store, draft["id"], budget_id, "VALIDATING", completed=len(parsed), total=len(pending))
    checkpoint()
    with store.transaction() as db:
        current = store.get("definition", draft["id"], db)
        require(current["status"] == "DRAFT", "IMMUTABLE_VERSION", "已发布版本不可重新解析")
        current.update(reportPlan=plan, candidates=candidates, coverage=coverage,
                       reviewPolicy="DETERMINISTIC_APPROVED", extractionMode=VERSION)
        current.setdefault("provenanceHistory", []).append({"kind": "COMPACT_FILL_PLAN", "at": now(),
            "fingerprint": fingerprint, "attemptId": attempt_id, "budgetId": model_budget, "runtime": runtime_manifest()})
        result = store.put("definition", current, db, expected=draft["revision"])
    progress(store, draft["id"], budget_id, "READY", completed=len(all_slots), total=len(all_slots),
             numericCount=len(pending), unit="all_slots")
    return result


ALIASES = {
    "date": ("operation_date", "手术日期"),
    "surgery_id": ("surgery_event_id", "手术唯一编号", "手术编号", "手术ID"),
    "patient_id": ("patient_id", "患者唯一编号", "患者ID"),
    "room": ("operating_room", "手术室"),
    "type": ("surgery_type", "手术类型"),
    "entry": ("entry_at", "入室时间"),
    "anesthesia": ("anesthesia_start_at", "麻醉开始时间"),
    "incision": ("incision_at", "手术开始时间", "开刀时间"),
}


def column(headers, field):
    found = [h for h in ALIASES[field] if h in headers]
    require(len(found) == 1, "FIELD_UNRESOLVED", "缺少或有歧义的字段：" + "/".join(ALIASES[field]))
    return found[0]


def day(value):
    if isinstance(value, dict):
        value = value.get("value")
    value = str(value or "").strip()
    try:
        return date.fromisoformat(value[:10].replace("/", "-")).isoformat()
    except ValueError:
        raise AppError("INVALID_DATE", "手术日期缺失或无法核验，不猜测日期") from None


def timestamp(value):
    if isinstance(value, dict):
        value = value.get("value")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        require("T" in str(value) or " " in str(value), "INVALID_TIME", "时差计算需要完整日期时间，不能仅凭时分补日期")
        return parsed
    except (ValueError, TypeError):
        raise AppError("INVALID_TIME", "时差计算需要完整日期时间，不能猜测跨日") from None


ACTUAL_FIELDS = ("手术状态", "surgery_status", "手术开始时间", "开刀时间", "incision_at", "手术结束时间", "surgery_end_at")


def actual_operation(values, headers):
    """An entity id establishes identity, not evidence that surgery occurred."""
    states = [str(values[h]).strip() for h in ACTUAL_FIELDS[:2] if h in headers and values.get(h) is not None]
    require(len(states) <= 1, "ACTUAL_STATUS_UNCONFIRMED", "存在多个手术状态字段，需确认实际手术的判定来源")
    events = [values[h] for h in ACTUAL_FIELDS[2:] if h in headers and values.get(h) not in (None, "")]
    for event in events:
        timestamp(event)
    if states:
        if states[0] in ("已完成", "完成", "已实施", "已执行"):
            return True
        if states[0] in ("计划", "已计划", "拟做", "取消", "已取消", "未实施", "未开始"):
            require(not events, "DATA_CONFLICT", "未实施状态与实际手术时间冲突")
            return False
        raise AppError("ACTUAL_STATUS_UNCONFIRMED", "手术状态值未核验，不能将计划记录当作实际手术量")
    require(events, "ACTUAL_STATUS_UNCONFIRMED", "缺少已实施状态或完整实际手术时间，无法证明该记录属于实际手术")
    return True


def prepare_sources(store, contract):
    """Evaluate every frozen file, locally. Filenames never establish source roles."""
    sources = []
    for f in contract["files"]:
        checkpoint()
        attachment = store.get("attachment", f["attachmentId"])
        require(attachment["sha256"] == f["sha256"] and attachment["snapshotHash"] == f["snapshotHash"]
                and attachment["route"] == f["route"], "CONTRACT_MISMATCH", "冻结材料发生变化")
        if f["route"]["role"] != "DATA_FILE":
            continue
        store.read_blob(f["sha256"])
        snapshot = snapshot_for(store, attachment)
        require(not snapshot.get("executionBlocked"), "FORMAT_UNSUPPORTED", "该材料需重新导入后分析")
        for sheet in snapshot.get("sheets", []):
            headers = sheet["headers"]
            role = "CANCELLATION" if "退单时间" in headers else "SUMMARY" if {"指标", "数值", "单位", "统计日期"} <= set(headers) else "DETAIL" if any(h in headers for h in ALIASES["entry"] + ALIASES["surgery_id"]) else "UNRESOLVED"
            sources.append({"attachmentId": f["attachmentId"], "sha256": f["sha256"], "snapshotHash": f["snapshotHash"],
                            "sheet": sheet["name"], "fileName": attachment["name"], "headers": headers,
                            "role": role, "rowCount": sheet["rowCount"], "rows": sheet["rows"]})
    return sources


def evidence(draft_plan, slot, contract):
    p = next(p for p in draft_plan["paragraphs"] if p["locator"] == slot["locator"])
    return [{"evidenceId": slot["id"], "documentSha256": contract["reportDocumentHash"],
             "locator": slot["locator"], "quote": p["text"], "quoteSha256": sha(p["text"].encode())}]


def _summary(slot, sources, document_plan):
    """Read existing daily scalar values only after exact semantic matching.

    No fuzzy labels, summing summary rows, historical report inference or unit conversion.
    """
    values, refs = {}, []
    unit = expected_unit(slot)
    position_required = sum(s["locator"] == slot["locator"] and s["kind"] == "NUMBER"
                            for s in document_plan["slots"]) > 1
    for source in sources:
        if source["role"] != "SUMMARY":
            continue
        for row in source["rows"]:
            v = row["values"]
            if v.get("待填位置") and v["待填位置"] != slot["id"]:
                continue
            if v.get("指标") != slot["plan"]["name"]:
                continue
            require(not position_required or v.get("待填位置") == slot["id"],
                    "SUMMARY_POSITION_UNCONFIRMED", "同段有多个待填数值，汇总值须明确对应的待填位置编号")
            require(unit is not None, "SUMMARY_UNIT_UNRESOLVED", "说明未明确该位置的单位，不能核验已有汇总值")
            require(str(v.get("单位", "")) == unit, "SUMMARY_UNIT_CONFLICT", "汇总值单位与说明不一致")
            require(str(v.get("手术室", "")) == slot["plan"]["room"], "SUMMARY_DIMENSION_CONFLICT", "汇总值手术室维度不一致")
            # An explicit basis is mandatory; a matching label alone is not proof.
            require(v.get("口径") == slot["semanticContext"], "SUMMARY_BASIS_UNCONFIRMED", "汇总值未提供与说明章节、时点及该段完全一致的口径")
            d = day(v.get("统计日期"))
            try:
                value = Decimal(str(v.get("数值")))
                require(value.is_finite() and value >= 0, "SUMMARY_VALUE_INVALID", "汇总值不是有效非负数")
            except InvalidOperation:
                raise AppError("SUMMARY_VALUE_INVALID", "汇总值不是有效数值") from None
            require(d not in values or values[d] == value, "SUMMARY_VALUE_CONFLICT", "同一日期和维度有冲突的汇总值")
            values[d] = value
            refs.append({"attachmentId": source["attachmentId"], "snapshotHash": source["snapshotHash"],
                         "sheet": source["sheet"], "row": row["row"], "column": "数值",
                         "slotId": slot["id"], "basisHash": digest(slot["semanticContext"])})
    return values, refs, unit


def calculate_slot(slot, sources, contract):
    """Closed operators, strict identities, daily attribution, fail closed on bad rows."""
    plan = slot["plan"]
    validate_intent(slot, plan)
    source_audit = [{k: s[k] for k in ("attachmentId", "fileName", "sheet", "role", "rowCount")} |
                    {"decision": "EXCLUDED" if s["role"] == "CANCELLATION" else "PENDING",
                     "reason": "退单记录不能作为已完成手术明细" if s["role"] == "CANCELLATION" else "待核验指标所需字段与口径"} for s in sources]
    base = {"metricId": slot["id"], "name": plan["name"], "definition": {"fillPlan": plan, "sourcePolicy": POLICY},
            "evidence": evidence(contract["reportPlan"], slot, contract),
            "provenance": {"sourcePolicy": POLICY, "sourceAssessment": source_audit}}
    if slot["id"] in contract.get("metricExclusions", {}):
        return base | {"outcome": "SKIP", "reasonCode": "METRIC_DELETED", "reason": "指标已删除，不参与计算"}
    if slot["kind"] != "NUMBER":
        return base | {"outcome": "SKIP", "reasonCode": "NEEDS_CLARIFICATION", "reason": plan["reason"]}
    try:
        values, refs, unit = _summary(slot, sources, contract["reportPlan"])
        if values:
            method, audit = "READ_SUMMARY", {"inputRows": len(refs), "includedRows": len(values)}
            used = {(r["attachmentId"], r["sheet"]) for r in refs}
            for assessment in source_audit:
                included = (assessment["attachmentId"], assessment["sheet"]) in used
                assessment.update(decision="INCLUDED" if included else "EXCLUDED",
                                  reason="已核验口径、日期、维度与单位的汇总值" if included else "本项直接读取已核验的汇总来源，不叠加明细或其他来源")
        else:
            method = plan["method"]
            require(method != "UNRESOLVED", "NEEDS_CLARIFICATION", plan["reason"] or "说明未明确该位置的计算口径")
            require(method != "READ_SUMMARY", "NEEDS_DATA", "未找到口径、单位、日期和维度均一致的汇总值")
            require(not any(s["role"] == "UNRESOLVED" and s["rowCount"] for s in sources),
                    "SOURCE_ROLE_UNRESOLVED", "有数据材料用途未确定，无法证明本项明细覆盖完整")
            eligible = [s for s in sources if s["role"] == "DETAIL"]
            require(eligible, "NEEDS_DATA", "缺少可核验的手术明细来源")
            identity = "patient_id" if method == "DURATION_PATIENTS" else "surgery_id"
            # Never equate an inpatient encounter number or row index with a surgery/patient id.
            for s, assessment in zip(sources, source_audit):
                if s["role"] == "DETAIL":
                    assessment.update(decision="INCLUDED", reason="纳入明细并按明确实体键逐日去重；不按文件行数计数")
                elif s["role"] != "CANCELLATION":
                    assessment.update(decision="EXCLUDED", reason="未匹配口径一致的汇总值，不与明细加总")
            for s in eligible:
                try:
                    column(s["headers"], identity)
                except AppError:
                    for assessment in source_audit:
                        if assessment["role"] == "DETAIL":
                            assessment.update(decision="BLOCKED", reason="缺少或有歧义的明确实体键，不能将行数或住院号当作业务实体数")
                    raise
            seen, day_keys, refs = {}, defaultdict(set), []
            input_rows = duplicates = 0
            for s in eligible:
                fields = {f: column(s["headers"], f) for f in {"date", identity} | ({"room"} if plan["room"] else set()) | ({"type"} if method == "EMERGENCY_COUNT" else set()) | ({plan["start"], plan["end"]} if method == "DURATION_PATIENTS" else set())}
                if method != "DURATION_PATIENTS":
                    fields.update({"actual:" + h: h for h in ACTUAL_FIELDS if h in s["headers"]})
                for row in s["rows"]:
                    checkpoint()
                    input_rows += 1
                    v = row["values"]
                    d = day(v.get(fields["date"]))
                    raw_key = v.get(fields[identity])
                    require(raw_key is not None and str(raw_key).strip(), "ENTITY_KEY_MISSING", "存在缺失实体键，未输出不完整计数")
                    key = str(raw_key).strip()
                    signature = {f: v.get(col) for f, col in fields.items()}
                    # Patients may undergo multiple operations; validate surgery rows independently.
                    record_key = key if identity == "surgery_id" else (d, digest(v))
                    require(record_key not in seen or seen[record_key] == signature, "DATA_CONFLICT", "同一手术实体的日期或参与计算字段存在冲突")
                    if record_key in seen:
                        duplicates += 1
                        continue
                    seen[record_key] = signature
                    day_keys[d]  # Keep a validated zero result for days with no qualifying rows.
                    if plan["room"] and str(v.get(fields["room"], "")) != plan["room"]:
                        continue
                    if method != "DURATION_PATIENTS" and not actual_operation(v, s["headers"]):
                        continue
                    include = True
                    if method == "EMERGENCY_COUNT":
                        category = str(v.get(fields["type"], ""))
                        require(category in ("急诊", "急诊手术", "择期", "择期手术"), "CATEGORY_UNRESOLVED", "手术类型含未核验的分类值")
                        include = category in ("急诊", "急诊手术")
                    if method == "DURATION_PATIENTS":
                        try:
                            seconds = (timestamp(v.get(fields[plan["end"]])) - timestamp(v.get(fields[plan["start"]]))).total_seconds()
                        except TypeError:
                            raise AppError("INVALID_TIME", "时间字段混用有时区与无时区值") from None
                        require(seconds >= 0, "INVALID_TIME", "时间先后异常，不自动推测跨午夜")
                        include = seconds > plan["minutes"] * 60
                    if include:
                        day_keys[d].add(key)
                refs.append({"attachmentId": s["attachmentId"], "snapshotHash": s["snapshotHash"], "sheet": s["sheet"], "fields": fields, "rowCount": s["rowCount"]})
            require(input_rows > 0, "NEEDS_DATA", "明细来源为空，不能据此声明业务数量为零")
            values = {d: Decimal(len(keys)) for d, keys in day_keys.items()}
            unit = "人" if identity == "patient_id" else "台"
            audit = {"inputRows": input_rows, "includedRows": sum(len(k) for k in day_keys.values()), "duplicateRows": duplicates}
        return base | {"outcome": "VALUE", "unit": unit, "method": method, "audit": audit,
                       "provenance": {"sourcePolicy": POLICY, "sources": refs, "sourceAssessment": source_audit},
                       "groups": [{"group": {"统计日期": d}, "outcome": "VALUE", "displayValue": format(v, "f"),
                                   "exactNumerator": format(v, "f"), "exactDenominator": "1", "numerator": None, "denominator": None} for d, v in sorted(values.items())]}
    except AppError as exc:
        if exc.code not in {"NEEDS_CLARIFICATION", "NEEDS_DATA", "FIELD_UNRESOLVED", "ENTITY_KEY_MISSING",
                            "DATA_CONFLICT", "CATEGORY_UNRESOLVED", "ACTUAL_STATUS_UNCONFIRMED", "INVALID_DATE", "INVALID_TIME",
                            "SOURCE_ROLE_UNRESOLVED", "SUMMARY_UNIT_UNRESOLVED", "SUMMARY_UNIT_CONFLICT",
                            "SUMMARY_DIMENSION_CONFLICT", "SUMMARY_BASIS_UNCONFIRMED", "SUMMARY_POSITION_UNCONFIRMED",
                            "SUMMARY_VALUE_INVALID", "SUMMARY_VALUE_CONFLICT"}:
            raise
        return base | {"outcome": "SKIP", "reasonCode": exc.code, "reason": exc.message}


def render_report(plan, results):
    """Each original slot has exactly one immutable result or explicit gap."""
    by_id = {r["metricId"]: r for r in results}
    require(len(results) == len(by_id) == len(plan["slots"]) and set(by_id) == {s["id"] for s in plan["slots"]},
            "REPORT_COVERAGE_INCOMPLETE", "报告结果与待填位置没有完整对账")
    slots = {s["id"]: s for s in plan["slots"]}
    paragraphs, gaps, bindings = [], [], []
    for paragraph in plan["paragraphs"]:
        text = paragraph["text"]
        for id in sorted(paragraph["slots"], key=lambda id: slots[id]["startOffset"], reverse=True):
            s, r = slots[id], by_id[id]
            if r["outcome"] == "VALUE":
                filled = "；".join(" / ".join(g["group"].values()) + "：" + g["displayValue"] for g in r["groups"])
                if len(r["groups"]) > 1:
                    filled = "【" + filled + "】"
            else:
                filled = "【待补充：" + r.get("reason", r.get("reasonCode", "缺少取数依据")) + "】"
                gaps.append({"slotId": id, "locator": s["locator"], "name": r["name"], "code": r.get("reasonCode"), "reason": r.get("reason")})
            text = text[:s["startOffset"]] + filled + text[s["endOffset"]:]
            bindings.append({"slotId": id, "outcome": r["outcome"], "locator": s["locator"], "text": filled})
        paragraphs.append({"locator": paragraph["locator"], "text": text, "original": paragraph["text"]})
    return {"version": VERSION, "paragraphs": paragraphs, "bindings": bindings, "gaps": gaps,
            "scopeNote": "依据本次全部材料取数；数值按材料中的统计日期展示。原文的前1日、当日仅为模板要求，未确认的日期不替换为系统昨日。",
            "contentDigest": digest([paragraphs, bindings])}
