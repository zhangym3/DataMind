"""Profile management and a bounded, data-minimizing online model gateway."""
from __future__ import annotations

import json
import re
import time
import random
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from .core import AppError, LIMITS, digest, dumps, now, require, uid
from .skills import SYSTEM_PROMPT, runtime_manifest

PRESETS = {
    "OpenAI": {"baseUrl": "https://api.openai.com/v1", "protocol": "responses"},
    "DeepSeek": {"baseUrl": "https://api.deepseek.com/v1", "protocol": "chat"},
    "通义千问": {"baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1", "protocol": "chat"},
    "智谱": {"baseUrl": "https://open.bigmodel.cn/api/paas/v4", "protocol": "chat"},
    "Moonshot": {"baseUrl": "https://api.moonshot.cn/v1", "protocol": "chat"},
    "Anthropic": {"baseUrl": "https://api.anthropic.com/v1", "protocol": "anthropic"},
    "自定义兼容服务": {"baseUrl": "", "protocol": "chat"},
}

# Official Chat API: max_tokens is at most 384K (393216), including thinking.
# https://api-docs.deepseek.com/api/create-chat-completion
DEEPSEEK_V4_MODELS = {"deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}


class Credentials:
    """Only the Windows credential manager backend is permitted in production."""
    service = "DataMind.InsightAgent"

    def __init__(self):
        import sys
        require(sys.platform == "win32", "CREDENTIAL_STORE_UNAVAILABLE", "正式版需要 Windows 凭据管理器")
        from keyring.backends.Windows import WinVaultKeyring
        self.backend = WinVaultKeyring()

    def set(self, reference, secret):
        require(isinstance(secret, str) and 1 <= len(secret) <= 4096, "INVALID_KEY", "Key 长度无效")
        self.backend.set_password(self.service, reference, secret)

    def get(self, reference):
        return self.backend.get_password(self.service, reference)

    def delete(self, reference):
        if self.get(reference):
            self.backend.delete_password(self.service, reference)


def save_profile(store, data, secret=None, credentials=None):
    allowed = {"id", "name", "provider", "protocol", "baseUrl", "model", "enabled", "default", "temperature", "maxOutputTokens", "timeoutSeconds"}
    require(set(data) <= allowed, "INVALID_PROFILE", "模型设置包含未知字段")
    profile = dict(data)
    require(0 < len(profile.get("name", "")) <= 80 and 0 < len(profile.get("model", "")) <= 160, "INVALID_PROFILE", "请填写配置名称和模型名称")
    require(profile.get("protocol") in ("chat", "responses", "anthropic"), "INVALID_PROFILE", "模型协议不受支持")
    url = urlsplit(profile.get("baseUrl", ""))
    require(url.scheme == "https" and url.hostname and not url.username and not url.password and not url.query and not url.fragment, "INVALID_ENDPOINT", "模型地址必须是无内嵌凭据的 HTTPS 地址")
    profile["baseUrl"] = profile["baseUrl"].rstrip("/")
    profile["id"] = profile.get("id") or uid("model_")
    old = next((p for p in store.list("model") if p["id"] == profile["id"]), None)
    same_target = old and all(old.get(k) == profile.get(k) for k in ("baseUrl", "provider", "protocol"))
    profile["credentialRef"] = old["credentialRef"] if same_target else uid("credential_")
    profile.setdefault("enabled", True)
    profile.setdefault("default", not store.list("model"))
    profile.setdefault("temperature", "0")
    require(re.fullmatch(r"[01](\.\d+)?|2(\.0+)?", profile["temperature"]), "INVALID_PROFILE", "温度必须为 0 至 2")
    profile["maxOutputTokens"] = min(max(int(profile.get("maxOutputTokens", 4096)), 256), LIMITS["output_tokens"])
    profile["timeoutSeconds"] = min(max(int(profile.get("timeoutSeconds", 120)), 10), 120)
    profile["check"] = None
    vault = credentials or Credentials()
    if secret:
        vault.set(profile["credentialRef"], secret)
    profile["hasKey"] = bool(vault.get(profile["credentialRef"]))
    with store.transaction() as db:
        if profile["default"]:
            for other in store.list("model", db):
                if other["id"] != profile["id"] and other.get("default"):
                    other["default"] = False
                    store.put("model", other, db)
        result = store.put("model", profile, db)
        store.event(db, "settings", "model.saved", {"profileId": result["id"]})
    if old and not same_target:
        vault.delete(old["credentialRef"])
    return public_profile(result)


def temporary_profile(data, credential_ref):
    """Build a non-persistent profile for testing the current settings form."""
    allowed = {"id", "name", "provider", "protocol", "baseUrl", "model", "enabled", "default", "temperature", "maxOutputTokens", "timeoutSeconds"}
    require(set(data) <= allowed, "INVALID_PROFILE", "模型设置包含未知字段")
    profile = dict(data)
    require(0 < len(profile.get("name", "")) <= 80 and 0 < len(profile.get("model", "")) <= 160, "INVALID_PROFILE", "请填写配置名称和模型名称")
    require(profile.get("protocol") in ("chat", "responses", "anthropic"), "INVALID_PROFILE", "模型协议不受支持")
    url = urlsplit(profile.get("baseUrl", ""))
    require(url.scheme == "https" and url.hostname and not url.username and not url.password and not url.query and not url.fragment, "INVALID_ENDPOINT", "模型地址必须是无内嵌凭据的 HTTPS 地址")
    profile["baseUrl"] = profile["baseUrl"].rstrip("/")
    profile["id"] = profile.get("id") or uid("model_test_")
    profile["credentialRef"] = credential_ref
    profile["enabled"] = True
    profile["default"] = False
    profile["temperature"] = str(profile.get("temperature", "0"))
    require(re.fullmatch(r"[01](\.\d+)?|2(\.0+)?", profile["temperature"]), "INVALID_PROFILE", "温度必须为 0 至 2")
    profile["maxOutputTokens"] = min(max(int(profile.get("maxOutputTokens", 4096)), 256), LIMITS["output_tokens"])
    profile["timeoutSeconds"] = min(max(int(profile.get("timeoutSeconds", 120)), 10), 120)
    profile["hasKey"] = True
    profile["temporary"] = True
    # Store.put normally supplies revisions. Temporary profiles never enter the store.
    profile["revision"] = 1
    return profile


def public_profile(profile):
    return {k: v for k, v in profile.items() if k != "credentialRef"}


def cleanup_temporary_credentials(store, credentials=None):
    """Delete temporary model-test credentials left by terminal jobs or recovery."""
    references = {
        job["payload"].get("temporaryCredentialRef")
        for job in store.jobs()
        if job["kind"] == "model_test" and job["status"] in ("SUCCEEDED", "FAILED", "CANCELLED")
        and isinstance(job["payload"].get("temporaryCredentialRef"), str)
        and job["payload"]["temporaryCredentialRef"].startswith("credential_test_")
    }
    if not references:
        return 0
    vault = credentials or Credentials()
    for reference in references:
        vault.delete(reference)
    return len(references)


def redact_text(text):
    text = re.sub(r"(?i)(?:sk-|api[_ -]?key[=: ]+)[a-z0-9_\-]{8,}", "[凭据已移除]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dXx](?!\w)", "[标识已移除]", text)
    text = re.sub(r"(?i)(患者姓名|姓名|住院号|病案号|身份证号|电话)\s*[:：=]\s*[^\s,，;；]+", r"\1：[已移除]", text)
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s\"<>，；]+", "[本机路径已移除]", text)
    text = re.sub(r"(患者姓名|姓名|住院号|病案号|身份证号|电话)\s+[^\s,，;；]+", r"\1 [已移除]", text)
    return text


STAGE_KEYS = {
    "connection": {"instruction"},
    "intent": {"question", "metricCatalog", "allowedActions", "resultCatalog", "materialCatalog"},
    "definition": {"nodes", "domain", "ruleSchema", "instruction"},
    "metric_catalog": {"nodes", "items", "targetLocators", "instruction"},
    "fill_plan": {"nodes", "slots", "instruction"},
    "review": {"rules", "results", "instruction"},
    "explain": {"question", "results", "instruction"},
    "material": {"question", "materials", "instruction"},
    "mapping": {"standardFields", "headers", "instruction"},
}


def retry_delay(attempt, retry_after=None):
    """Honor short server waits; long waits become a retryable terminal error."""
    delay = min(0.25 * 2 ** attempt, 2) + random.uniform(0, 0.15)
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        require(seconds <= 5, "MODEL_RATE_LIMIT", "模型服务要求较长等待，请稍后重试")
        delay = max(delay, seconds)
    return delay


def outbound(stage, payload):
    require(stage in STAGE_KEYS and set(payload) <= STAGE_KEYS[stage], "OUTBOUND_POLICY", "模型请求不符合当前阶段字段白名单")
    def closed(value, keys):
        require(isinstance(value, dict) and set(value) <= set(keys), "OUTBOUND_POLICY", "模型请求包含未批准的嵌套字段")
    if stage == "intent":
        for metric in payload.get("metricCatalog", []):
            closed(metric, ("id", "name"))
        for result in payload.get("resultCatalog", []):
            closed(result, ("id", "name", "unit", "groupFields"))
            require(isinstance(result.get("groupFields", []), list) and all(isinstance(v, str) for v in result.get("groupFields", [])), "OUTBOUND_POLICY", "结果目录结构无效")
        for material in payload.get("materialCatalog", []):
            closed(material, ("id", "name", "kind", "role", "status"))
    if stage == "material":
        for material in payload.get("materials", {}).get("attachments", []):
            closed(material, ("id", "name", "kind", "role", "status"))
        definition = payload.get("materials", {}).get("definition")
        if definition:
            closed(definition, ("id", "name", "status", "nodes", "metrics"))
            for node in definition.get("nodes", []):
                closed(node, ("locator", "text"))
            for metric in definition.get("metrics", []):
                closed(metric, ("id", "name", "status"))
        for table in payload.get("materials", {}).get("tables", []):
            closed(table, ("fileName", "sheet", "rowCount", "headers"))
    if stage in ("definition", "metric_catalog"):
        for node in payload.get("nodes", []):
            closed(node, ("locator", "text", "documentSha256", "quoteSha256", "replaces"))
            require(all(isinstance(v, str) or k == "replaces" and isinstance(v, list) for k, v in node.items()), "OUTBOUND_POLICY", "定义节点结构无效")
    if stage == "metric_catalog":
        require(isinstance(payload.get("items"), list) and len(payload["items"]) <= 4,
                "OUTBOUND_POLICY", "指标目录批次无效")
        if "targetLocators" in payload:
            refs = payload["targetLocators"]
            require(not payload["items"] and isinstance(refs, list) and 1 <= len(refs) <= 200
                    and all(isinstance(ref, str) for ref in refs), "OUTBOUND_POLICY", "指标目录原文批次无效")
        for item in payload["items"]:
            closed(item, ("id", "name", "category", "locators"))
            require(set(item) == {"id", "name", "category", "locators"}
                    and all(isinstance(item[k], str) for k in ("id", "name", "category"))
                    and isinstance(item["locators"], list) and all(isinstance(v, str) for v in item["locators"]),
                    "OUTBOUND_POLICY", "指标目录结构无效")
    if stage == "fill_plan":
        require(isinstance(payload.get("nodes"), list) and isinstance(payload.get("slots"), list)
                and 1 <= len(payload["slots"]) <= 4, "OUTBOUND_POLICY", "待填位置批次结构无效")
        for node in payload["nodes"]:
            closed(node, ("locator", "text"))
            require(set(node) == {"locator", "text"} and all(isinstance(v, str) for v in node.values()), "OUTBOUND_POLICY", "说明节点结构无效")
        for slot in payload["slots"]:
            closed(slot, ("id", "locator", "startOffset", "endOffset"))
            require(set(slot) == {"id", "locator", "startOffset", "endOffset"} and isinstance(slot["id"], str)
                    and isinstance(slot["locator"], str) and type(slot["startOffset"]) is int and type(slot["endOffset"]) is int
                    and 0 <= slot["startOffset"] < slot["endOffset"], "OUTBOUND_POLICY", "待填位置范围无效")
    if stage in ("review", "explain"):
        metric_keys = ("metricId", "name", "outcome", "reasonCode", "values") if stage == "explain" else ("metricId", "outcome", "audit", "groups")
        for metric in payload.get("results", []):
            closed(metric, metric_keys)
            for value in metric.get("values", []):
                closed(value, ("displayValue", "unit", "outcome"))
            for group in metric.get("groups", []):
                closed(group, ("outcome", "reasonCode", "displayValue", "exactNumerator", "exactDenominator", "numerator", "denominator", "scaleFactor"))
                for key in ("numerator", "denominator"):
                    if key in group:
                        closed(group[key], ("exactNumerator", "exactDenominator"))
            audit = metric.get("audit")
            audit_keys = {"inputRows", "inWindowRows", "outsideWindowRows", "missingRows", "invalidRows", "excludedRows", "includedRows", "analysisScope"}
            require(audit is None or isinstance(audit, dict) and set(audit) <= audit_keys and all((key == "analysisScope" and value in ("ALL_DATA", "DATE_WINDOW")) or (key != "analysisScope" and type(value) is int and value >= 0) for key, value in audit.items()), "OUTBOUND_POLICY", "只允许向复核阶段发送计数审计摘要")
        if stage == "review":
            from .engine import SCHEMA
            for rule in payload.get("rules", []):
                require(not list(Draft202012Validator(SCHEMA).iter_errors(rule)), "OUTBOUND_POLICY", "复核规则结构无效")
    # All callers construct this DTO from aggregate metadata; never accept a workbook
    # snapshot, filesystem path, arbitrary history, or native clipboard payload.
    forbidden = {"rows", "source_rows", "apiKey", "credentialRef", "path", "filePath", "rawData", "sources", "_source"}
    def clean(value, key=""):
        require(key not in forbidden, "OUTBOUND_POLICY", "禁止向模型发送原始明细、路径或凭据")
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(x) for x in value]
        if isinstance(value, str):
            if key in ("displayValue", "exactNumerator", "exactDenominator", "scaleFactor"):
                require(re.fullmatch(r"-?\d+(?:\.\d+)?", value), "OUTBOUND_POLICY", "数值字段必须为精确数字字符串")
                return value
            return redact_text(value)
        return value
    # JSON Schema itself contains "values" properties. It is a local static spec,
    # not user data; only this field is copied separately.
    schema = payload.get("ruleSchema")
    result = clean({k: v for k, v in payload.items() if k != "ruleSchema"})
    if schema is not None:
        from .engine import SCHEMA
        require(schema == SCHEMA, "OUTBOUND_POLICY", "只能发送内置规则结构")
        result["ruleSchema"] = schema
    require(len(dumps(result).encode()) <= 250000, "MODEL_INPUT_LIMIT", "模型请求过大，请拆分指标定义文档")
    return result


class Gateway:
    def __init__(self, store, credentials=None, transport=None, check_cancel=None):
        self.store = store
        self.credentials = credentials
        self.transport = transport
        self.check_cancel = check_cancel or (lambda: None)
        self.frozen_profile = None
        self.frozen_limits = None

    def profile(self, profile_id=None):
        profiles = self.store.list("model")
        profile = next((p for p in profiles if p["id"] == profile_id), None) if profile_id else next((p for p in profiles if p.get("enabled") and p.get("default")), None)
        require(profile is not None and profile.get("enabled"), "MODEL_NOT_CONFIGURED", "请在设置中配置并启用默认在线模型")
        return profile

    def call(self, stage, payload, output_schema, budget_id, profile_id=None, profile=None):
        settings = self.store.list("settings")
        settings = settings[0] if settings else {}
        require(settings.get("modelEnabled", True), "MODEL_DISABLED", "在线模型已在设置中关闭；可进行冻结重算或本地计算")
        if profile is not None:
            require(profile.get("temporary") is True, "INVALID_PROFILE", "临时模型配置标识无效")
        elif self.frozen_profile:
            current = self.profile(self.frozen_profile["id"])
            require(current["revision"] == self.frozen_profile["revision"], "MODEL_CONFIG_CHANGED", "模型配置与冻结合同不符，请新建运行")
            profile = self.frozen_profile | {"credentialRef": current["credentialRef"]}
        else:
            profile = self.profile(profile_id)
        limits = self.frozen_limits or {"model_calls": settings.get("modelCalls", LIMITS["model_calls"]), "model_tokens": settings.get("modelTokens", LIMITS["model_tokens"]), "run_seconds": settings.get("runSeconds", LIMITS["run_seconds"])}
        secret = (self.credentials or Credentials()).get(profile["credentialRef"])
        require(secret, "MODEL_KEY_MISSING", "模型 Key 尚未配置，请在设置中填写")
        sanitized = outbound(stage, payload)
        system = SYSTEM_PROMPT
        user = dumps({"stage": stage, "data": sanitized, "outputSchema": output_schema})
        protocol = profile["protocol"]
        headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
        body = {"model": profile["model"]}
        output_tokens = min(profile["maxOutputTokens"], LIMITS["output_tokens"])
        official_deepseek = protocol == "chat" and urlsplit(profile["baseUrl"]).hostname == "api.deepseek.com" \
            and profile["model"] in DEEPSEEK_V4_MODELS
        if official_deepseek:
            output_tokens = min(output_tokens, 393216)
        if protocol == "chat":
            endpoint = "/chat/completions"
            body.update(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], response_format={"type": "json_object"}, max_tokens=output_tokens, temperature=float(profile["temperature"]))
            # These official DeepSeek models enable high-effort thinking by
            # default. It can consume the entire bounded JSON output allowance
            # before returning content. Use their documented non-thinking mode
            # for extraction, without changing the user's saved model profile.
            if official_deepseek and stage in ("definition", "metric_catalog", "fill_plan"):
                body["thinking"] = {"type": "disabled"}
        elif protocol == "responses":
            endpoint = "/responses"
            body.update(instructions=system, input=user, text={"format": {"type": "json_object"}}, max_output_tokens=output_tokens, store=False)
        else:
            endpoint = "/messages"
            headers = {"x-api-key": secret, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
            body.update(system=system, messages=[{"role": "user", "content": user}], max_tokens=output_tokens, temperature=float(profile["temperature"]))
        last_code = "MODEL_UNAVAILABLE"
        output_key = "max_output_tokens" if protocol == "responses" else "max_tokens"
        input_reserve = len(user.encode("utf-8")) + len(system.encode("utf-8"))
        def pause_retry(delay):
            while delay > 0:
                self.check_cancel()
                step = min(delay, 0.25)
                time.sleep(step)
                delay -= step
        for attempt in range(3):
            self.check_cancel()
            with self.store.transaction() as db:
                existing = next((x for x in self.store.list("budget", db) if x["id"] == budget_id), None)
                budget = existing or {"id": budget_id, "calls": 0, "reservedTokens": 0, "started": time.time(), "callLimit": limits["model_calls"], "tokenLimit": limits["model_tokens"], "secondsLimit": limits["run_seconds"], "profileId": profile["id"], "profileRevision": profile["revision"]}
                require(budget["profileId"] == profile["id"] and budget["profileRevision"] == profile["revision"], "MODEL_CONFIG_CHANGED", "任务运行期间模型配置发生变化，请新建任务")
                available_output = budget["tokenLimit"] - budget["reservedTokens"] - input_reserve
                require(budget["calls"] < budget["callLimit"] and available_output >= 256 and time.time() - budget["started"] <= budget["secondsLimit"], "BUDGET_EXCEEDED", "当前任务已达到模型请求预算")
                effective_output = min(output_tokens, available_output)
                body[output_key] = effective_output
                reserve = input_reserve + effective_output
                budget["calls"] += 1
                budget["reservedTokens"] += reserve
                self.store.put("budget", budget, db)
                self.store.event(db, budget_id, "model.request", {"stage": stage, "profileId": profile["id"], "payloadHash": digest(sanitized), "attempt": attempt + 1})
            request_started = time.monotonic()
            receipt = {"budgetId": budget_id, "stage": stage, "profileId": profile["id"], "profileRevision": profile["revision"], "attempt": budget["calls"], "status": "FAILED", "usage": None, "usageSource": "NOT_RETURNED", "requestId": None, "responseModel": None}
            receipt.update(configuredOutputTokens=profile["maxOutputTokens"], effectiveOutputTokens=effective_output)
            request_options = {"thinking": body["thinking"]} if "thinking" in body else {}
            receipt.update(runtimeHash=runtime_manifest()["sha256"], requestOptions=request_options,
                           promptHash=digest({"system": system, "stage": stage, "instruction": sanitized.get("instruction"),
                                              "outputSchema": output_schema, "requestOptions": request_options,
                                              "effectiveOutputTokens": effective_output}))
            def safe_identifier(value):
                return value[:160] if isinstance(value, str) and secret not in value and re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,160}", value) else None
            try:
                with httpx.Client(transport=self.transport, timeout=httpx.Timeout(profile["timeoutSeconds"], connect=10), follow_redirects=False, trust_env=False) as client:
                    with client.stream("POST", profile["baseUrl"] + endpoint, headers=headers, json=body) as response:
                        receipt["httpStatus"] = response.status_code
                        receipt["requestId"] = safe_identifier(response.headers.get("x-request-id") or response.headers.get("request-id"))
                        if response.status_code in (401, 403):
                            raise AppError("MODEL_AUTH_FAILED", "模型认证失败，请检查 Key 和权限")
                        if response.status_code in (429, 500, 502, 503, 504):
                            last_code = "MODEL_RATE_LIMIT" if response.status_code == 429 else "MODEL_UNAVAILABLE"
                            receipt["errorCode"] = last_code
                            if attempt < 2:
                                pause_retry(retry_delay(attempt, response.headers.get("retry-after")))
                            continue
                        require(response.status_code == 200, "MODEL_PROTOCOL_ERROR", "模型协议或模型名称不匹配", {"status": response.status_code})
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            self.check_cancel()
                            require(time.monotonic() - request_started <= profile["timeoutSeconds"], "MODEL_TIMEOUT", "模型请求总时间超限")
                            content.extend(chunk)
                            require(len(content) <= LIMITS["model_response_bytes"], "MODEL_OUTPUT_LIMIT", "模型响应大小超限")
                        response_data = json.loads(content)
                        require(isinstance(response_data, dict), "MODEL_SCHEMA_INVALID", "模型响应必须为 JSON 对象")
                        receipt["responseModel"] = safe_identifier(response_data.get("model"))
                        usage = response_data.get("usage")
                        if isinstance(usage, dict):
                            receipt["usage"] = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens", "cache_creation_input_tokens", "cache_read_input_tokens") and type(v) is int and v >= 0}
                            receipt["usageSource"] = "PROVIDER_RESPONSE"
                            for key in ("completion_tokens_details", "output_tokens_details"):
                                details = usage.get(key)
                                if isinstance(details, dict):
                                    receipt[key] = {k: v for k, v in details.items() if k in ("reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens") and type(v) is int and v >= 0}
                self.check_cancel()
                if protocol == "chat":
                    choice = response_data["choices"][0]
                    receipt["finishReason"] = safe_identifier(choice.get("finish_reason"))
                    raw = choice["message"]["content"]
                    receipt["outputCharacters"] = len(raw) if isinstance(raw, str) else 0
                    require(choice.get("finish_reason") != "content_filter", "MODEL_OUTPUT_FILTERED", "模型服务过滤了当前输出")
                    require(choice.get("finish_reason") in ("stop", None), "MODEL_OUTPUT_TRUNCATED", "模型输出不完整，请增加输出预算或缩小任务")
                elif protocol == "responses":
                    receipt["finishReason"] = safe_identifier(response_data.get("status"))
                    receipt["stopReason"] = safe_identifier((response_data.get("incomplete_details") or {}).get("reason"))
                    require(response_data.get("status") == "completed", "MODEL_OUTPUT_TRUNCATED", "模型输出未完成")
                    raw = "".join(c.get("text", "") for item in response_data["output"] for c in item.get("content", []) if c.get("type") == "output_text")
                else:
                    receipt["finishReason"] = safe_identifier(response_data.get("stop_reason"))
                    require(response_data.get("stop_reason") == "end_turn", "MODEL_OUTPUT_TRUNCATED", "模型输出未完成")
                    raw = "".join(c["text"] for c in response_data["content"] if c["type"] == "text")
                receipt["outputCharacters"] = len(raw)
                value = json.loads(raw)
                require(not list(Draft202012Validator(output_schema).iter_errors(value)), "MODEL_SCHEMA_INVALID", "模型返回不符合约定结构")
                receipt["status"] = "SUCCEEDED"
                return value
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_code = "MODEL_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "MODEL_UNAVAILABLE"
                receipt["errorCode"] = last_code
                # The parser can shrink a timed-out detail batch. Do that
                # before spending two more attempts on the identical request.
                batch_items = sanitized.get("items" if stage == "metric_catalog" else "slots", [])
                if stage == "metric_catalog" and not batch_items:
                    batch_items = sanitized.get("targetLocators", [])
                if last_code == "MODEL_TIMEOUT" and stage in ("metric_catalog", "fill_plan") and len(batch_items) > 1:
                    raise AppError("MODEL_TIMEOUT", "当前解析批次响应超时，正在缩小批次") from None
                if attempt < 2:
                    pause_retry(retry_delay(attempt))
            except AppError as exc:
                receipt["errorCode"] = exc.code
                raise
            except (KeyError, ValueError, TypeError, IndexError, AttributeError):
                receipt["errorCode"] = "MODEL_SCHEMA_INVALID"
                raise AppError("MODEL_SCHEMA_INVALID", "模型返回不是有效结构化 JSON") from None
            finally:
                receipt["elapsedSeconds"] = round(time.monotonic() - request_started, 3)
                with self.store.transaction() as db:
                    self.store.put("model_call", receipt, db)
                    # Settle reservations from provider usage. Missing usage stays
                    # conservatively charged, including interrupted requests.
                    usage = receipt.get("usage") or {}
                    actual = usage.get("total_tokens")
                    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
                    completion = usage.get("completion_tokens", usage.get("output_tokens"))
                    if prompt is not None and completion is not None:
                        components = prompt + completion
                        # Messages reports cached input separately; other protocols
                        # already include cached input in their input token count.
                        if protocol == "anthropic":
                            components += usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                        actual = max(actual or 0, components)
                    saved = self.store.get("budget", budget_id, db)
                    if actual is not None:
                        saved["reservedTokens"] = max(0, saved["reservedTokens"] - reserve) + actual
                        saved["actualTokens"] = saved.get("actualTokens", 0) + actual
                    else:
                        saved["unknownTokens"] = saved.get("unknownTokens", 0) + reserve
                    self.store.put("budget", saved, db)
        message = {
            "MODEL_TIMEOUT": f"模型响应超时（单次上限 {profile['timeoutSeconds']} 秒），自动重试后仍未完成。请稍后重试或在设置中切换响应更快的模型。",
            "MODEL_UNAVAILABLE": "模型服务连接失败、连接中断或暂时不可用，自动重试后仍未恢复。请检查网络及模型服务后重试。",
        }.get(last_code, "在线模型暂时不可用，请检查网络或稍后重试")
        raise AppError(last_code, message)

    def test(self, profile_id=None, profile=None):
        schema = {"type": "object", "properties": {"ok": {"const": True}}, "required": ["ok"], "additionalProperties": False}
        self.call("connection", {"instruction": "输出 {\"ok\":true}。这是连接与 JSON 能力测试。"}, schema, uid("check_"), profile_id, profile)
        if profile is not None:
            return {"status": "PASS", "message": "当前配置连接及 JSON 返回测试通过；配置尚未保存"}
        with self.store.transaction() as db:
            profile = self.store.get("model", profile_id, db)
            profile["check"] = {"status": "PASS", "at": now(), "capabilities": ["https", "json"]}
            self.store.put("model", profile, db)
        return {"status": "PASS", "message": "连接及 JSON 返回测试通过；业务能力仍需实际任务验证"}
