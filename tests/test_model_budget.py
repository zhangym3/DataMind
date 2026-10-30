"""Provider receipts settle cumulative budgets across protocol and failure paths."""
import json

import httpx
import pytest

from insight_agent.core import AppError
from insight_agent.models import Gateway, save_profile, temporary_profile
from test_provenance import configured


@pytest.mark.parametrize("protocol,usage,expected", [
    ("chat", {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21}, 21),
    ("chat", {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 1}, 21),
    ("responses", {"input_tokens": 17, "output_tokens": 4}, 21),
    ("anthropic", {"input_tokens": 17, "output_tokens": 4, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 2}, 33),
])
def test_provider_usage_settles_reservation_and_accumulates(tmp_path, protocol, usage, expected):
    service, vault, data = configured(tmp_path)
    save_profile(service.store, data | {"protocol": protocol, "default": True}, "budget-fixture", vault)
    if protocol == "chat":
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    elif protocol == "responses":
        response = {"status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}]}
    else:
        response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}
    gateway = Gateway(service.store, vault, httpx.MockTransport(lambda _: httpx.Response(200, json=response | {"usage": usage})))
    for _ in range(2):
        gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "shared-budget")
    budget = service.store.get("budget", "shared-budget")
    assert budget["calls"] == 2 and budget["actualTokens"] == budget["reservedTokens"] == expected * 2
    assert not budget.get("unknownTokens")


def test_truncated_output_is_charged_and_unknown_usage_keeps_reservation(tmp_path):
    service, vault, _ = configured(tmp_path)
    responses = iter([
        {"usage": {"total_tokens": 25}, "choices": [{"finish_reason": "length", "message": {"content": "{"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
    ])
    gateway = Gateway(service.store, vault, httpx.MockTransport(lambda _: httpx.Response(200, json=next(responses))))
    with pytest.raises(AppError) as error:
        gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "shared-budget")
    assert error.value.code == "MODEL_OUTPUT_TRUNCATED"
    assert service.store.get("budget", "shared-budget")["actualTokens"] == 25
    gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "shared-budget")
    budget = service.store.get("budget", "shared-budget")
    assert budget["calls"] == 2 and budget["unknownTokens"] > 0
    assert budget["reservedTokens"] == budget["actualTokens"] + budget["unknownTokens"]
    assert any(c.get("finishReason") == "length" and c["outputCharacters"] == 1 for c in service.store.list("model_call"))


def test_config_revision_cannot_spend_or_mix_an_existing_budget(tmp_path):
    service, vault, data = configured(tmp_path)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"usage": {"total_tokens": 10}, "choices": [{"message": {"content": "{}"}}]})
    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "shared-budget")
    save_profile(service.store, data | {"model": "new-model", "default": True}, credentials=vault)
    with pytest.raises(AppError) as error:
        gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "shared-budget")
    assert error.value.code == "MODEL_CONFIG_CHANGED" and len(calls) == 1
    gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "new-generation")
    assert service.store.get("budget", "shared-budget")["calls"] == 1
    assert service.store.get("budget", "new-generation")["actualTokens"] == 10


def model_response(protocol):
    if protocol == "responses":
        return {"status": "completed", "output": [{"content": [{"type": "output_text", "text": "{}"}]}]}
    if protocol == "anthropic":
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}
    return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stage", ["metric_catalog", "fill_plan"])
def test_extraction_sends_configured_output_above_old_stage_caps(tmp_path, protocol, stage):
    service, vault, data = configured(tmp_path)
    save_profile(service.store, data | {"protocol": protocol, "default": True, "maxOutputTokens": 65536},
                 "budget-fixture", vault)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=model_response(protocol))

    payload = {"nodes": [{"locator": "p:1", "text": "手术量X台"}], "instruction": "Return JSON."}
    if stage == "metric_catalog":
        payload.update(items=[], targetLocators=["p:1"])
    else:
        payload["slots"] = [{"id": "s:1", "locator": "p:1", "startOffset": 3, "endOffset": 4}]
    Gateway(service.store, vault, httpx.MockTransport(respond)).call(stage, payload, {"type": "object"}, "large-output")
    assert requests[0]["max_output_tokens" if protocol == "responses" else "max_tokens"] == 65536
    receipt = service.store.list("model_call")[0]
    assert receipt["configuredOutputTokens"] == receipt["effectiveOutputTokens"] == 65536


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_one_million_is_saved_and_bounded_by_remaining_budget(tmp_path, protocol):
    service, vault, data = configured(tmp_path)
    config = data | {"protocol": protocol, "default": True, "maxOutputTokens": 1000000}
    profile = save_profile(service.store, config, "budget-fixture", vault)
    assert profile["maxOutputTokens"] == temporary_profile(config, "form-test")["maxOutputTokens"] == 1000000
    service.save_settings({"modelTokens": 1000000})
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=model_response(protocol))  # No usage: retain the reservation.

    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "one-million")
    effective = requests[0]["max_output_tokens" if protocol == "responses" else "max_tokens"]
    assert 990000 < effective < 1000000
    budget = service.store.get("budget", "one-million")
    assert budget["reservedTokens"] == budget["unknownTokens"] == budget["tokenLimit"] == 1000000
    receipt = service.store.list("model_call")[0]
    assert receipt["configuredOutputTokens"] == 1000000 and receipt["effectiveOutputTokens"] == effective
    with pytest.raises(AppError) as error:
        gateway.call("connection", {"instruction": "Return JSON."}, {"type": "object"}, "one-million")
    assert error.value.code == "BUDGET_EXCEEDED" and len(requests) == 1


def test_one_million_respects_official_deepseek_provider_limit(tmp_path):
    service, vault, data = configured(tmp_path)
    profile = save_profile(service.store, data | {"baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-flash",
                                                "default": True, "maxOutputTokens": 1000000}, "budget-fixture", vault)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=model_response("chat") | {"usage": {"total_tokens": 25}})

    Gateway(service.store, vault, httpx.MockTransport(respond)).call(
        "metric_catalog", {"nodes": [{"locator": "p:1", "text": "手术量X台"}], "items": [],
                           "targetLocators": ["p:1"]}, {"type": "object"}, "provider-limit")
    assert requests[0]["max_tokens"] == 393216
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert service.store.get("model", profile["id"])["maxOutputTokens"] == 1000000
    receipt = service.store.list("model_call")[0]
    assert receipt["configuredOutputTokens"] == 1000000 and receipt["effectiveOutputTokens"] == 393216


def test_budget_with_less_than_minimum_output_stops_before_request(tmp_path):
    service, vault, data = configured(tmp_path)
    save_profile(service.store, data | {"default": True, "maxOutputTokens": 1000000}, credentials=vault)
    service.save_settings({"modelTokens": 1000})
    requests = []
    gateway = Gateway(service.store, vault, httpx.MockTransport(lambda request: requests.append(request)))
    with pytest.raises(AppError) as error:
        gateway.call("connection", {"instruction": "Return JSON. " * 1000}, {"type": "object"}, "no-output-left")
    assert error.value.code == "BUDGET_EXCEEDED" and not requests
    assert not service.store.list("model_call")
