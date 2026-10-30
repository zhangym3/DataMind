"""Transport failures remain actionable model errors, including during extraction."""
import json

import httpx
import pytest
from docx import Document

from insight_agent.core import AppError
from insight_agent.definitions import create_draft
from insight_agent.files import import_file
from insight_agent.models import Gateway, save_profile
from insight_agent.worker import run_once
from test_provenance import configured


@pytest.mark.parametrize('stage,host,model,protocol,disabled', [
    ('metric_catalog', 'api.deepseek.com', 'deepseek-flash', 'chat', True),
    ('definition', 'api.deepseek.com', 'deepseek-v4-pro', 'chat', True),
    ('fill_plan', 'api.deepseek.com', 'deepseek-v4-flash', 'chat', True),
    ('metric_catalog', 'api.deepseek.com', 'deepseek-v4-flash-vision-exp', 'chat', True),
    ('connection', 'api.deepseek.com', 'deepseek-flash', 'chat', False),
    ('metric_catalog', 'other.test', 'deepseek-flash', 'chat', False),
    ('metric_catalog', 'api.deepseek.com.other.test', 'deepseek-flash', 'chat', False),
    ('metric_catalog', 'api.deepseek.com', 'deepseek-reasoner', 'chat', False),
    ('metric_catalog', 'api.deepseek.com', 'deepseek-flash', 'responses', False),
    ('metric_catalog', 'api.deepseek.com', 'deepseek-flash', 'anthropic', False),
])
def test_bounded_extraction_disables_supported_deepseek_thinking_without_changing_profile(
        tmp_path, stage, host, model, protocol, disabled):
    service, vault, data = configured(tmp_path)
    save_profile(service.store, data | {'baseUrl': f'https://{host}/v1', 'protocol': protocol,
                                       'model': model, 'default': True}, 'synthetic-key', vault)
    before = service.store.get('model', data['id'])
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if protocol == 'responses':
            response = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
        elif protocol == 'anthropic':
            response = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"ok":true}'}]}
        else:
            response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
        return httpx.Response(200, json=response)

    payload = {'instruction': 'Return JSON.'}
    if stage != 'connection':
        payload['nodes'] = [{'locator': 'p:1', 'text': '总手术量X台'}]
    if stage == 'metric_catalog':
        payload.update(items=[], targetLocators=['p:1'])
    elif stage == 'fill_plan':
        payload['slots'] = [{'id': 's:1', 'locator': 'p:1', 'startOffset': 4, 'endOffset': 5}]
    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    assert gateway.call(stage, payload, {'type': 'object'}, 'bounded-extraction') == {'ok': True}
    assert requests[0].get('thinking') == ({'type': 'disabled'} if disabled else None)
    assert service.store.get('model', data['id']) == before
    assert service.store.list('model_call')[0]['requestOptions'] == (
        {'thinking': {'type': 'disabled'}} if disabled else {})


@pytest.mark.parametrize('failure,code,message', [
    (httpx.ReadTimeout, 'MODEL_TIMEOUT', '超时'),
    (httpx.ConnectError, 'MODEL_UNAVAILABLE', '连接'),
    (httpx.RemoteProtocolError, 'MODEL_UNAVAILABLE', '连接'),
])
def test_exhausted_transport_retries_are_actionable(tmp_path, monkeypatch, failure, code, message):
    service, vault, _ = configured(tmp_path)
    monkeypatch.setattr('insight_agent.models.retry_delay', lambda *args: 0)
    requests = []

    def respond(request):
        requests.append(request)
        raise failure('private upstream diagnostic', request=request)

    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    with pytest.raises(AppError) as error:
        gateway.call('connection', {'instruction': 'Return JSON.'}, {'type': 'object'}, 'transport-budget')

    assert len(requests) == 3
    assert error.value.code == code and message in error.value.message
    assert 'private upstream diagnostic' not in str(error.value)
    calls = service.store.list('model_call')
    assert len(calls) == 3
    assert all(call['status'] == 'FAILED' and call['errorCode'] == code for call in calls)


def test_remote_disconnect_can_recover_on_retry(tmp_path, monkeypatch):
    service, vault, _ = configured(tmp_path)
    monkeypatch.setattr('insight_agent.models.retry_delay', lambda *args: 0)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.RemoteProtocolError('connection closed', request=request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})

    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    result = gateway.call('connection', {'instruction': 'Return JSON.'}, {'type': 'object'}, 'recovery-budget')

    assert result == {'ok': True} and len(requests) == 2
    calls = sorted(service.store.list('model_call'), key=lambda call: call['attempt'])
    assert calls[0]['errorCode'] == 'MODEL_UNAVAILABLE'
    assert calls[1]['status'] == 'SUCCEEDED'


@pytest.mark.parametrize('stage,count,attempts', [
    ('metric_catalog', 0, 3), ('metric_catalog', 1, 3), ('metric_catalog', 2, 1),
    ('catalog_directory', 1, 3), ('catalog_directory', 2, 1),
    ('fill_plan', 1, 3), ('fill_plan', 2, 1),
])
def test_multi_item_timeout_returns_to_parser_before_identical_retries(tmp_path, monkeypatch, stage, count, attempts):
    service, vault, _ = configured(tmp_path)
    monkeypatch.setattr('insight_agent.models.retry_delay', lambda *args: 0)
    requests = []

    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout('private timeout diagnostic', request=request)

    payload = {'nodes': [{'locator': 'p:1', 'text': '总手术量X台'}], 'instruction': 'Return JSON.'}
    if stage == 'catalog_directory':
        stage = 'metric_catalog'
        payload['targetLocators'] = [f'p:{i}' for i in range(count)]
        count = 0
    if stage == 'metric_catalog':
        payload['items'] = [{'id': f'metric_{i}', 'name': '手术量', 'category': '', 'locators': ['p:1']}
                            for i in range(count)]
    else:
        payload['slots'] = [{'id': f'slot_{i}', 'locator': 'p:1', 'startOffset': 4, 'endOffset': 5}
                            for i in range(count)]
    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    with pytest.raises(AppError) as error:
        gateway.call(stage, payload, {'type': 'object'}, 'batch-timeout')
    assert error.value.code == 'MODEL_TIMEOUT'
    assert 'private timeout diagnostic' not in str(error.value)
    assert len(requests) == attempts
    receipts = service.store.list('model_call')
    assert len(receipts) == attempts
    assert all(r['errorCode'] == 'MODEL_TIMEOUT' and r['status'] == 'FAILED' for r in receipts)


def test_extraction_preserves_input_after_timeouts_and_remote_disconnect(tmp_path, monkeypatch):
    service, vault, _ = configured(tmp_path)
    monkeypatch.setattr('insight_agent.models.retry_delay', lambda *args: 0)
    path = tmp_path / '指标说明.docx'
    document = Document()
    document.add_paragraph('统计口径：按手术事件统计数量。')
    document.add_paragraph('分母：全部手术台次。')
    document.save(path)
    attachment = import_file(service.store, path, 'library')
    draft = create_draft(service.store, attachment['id'], '断线测试')
    queued = service.queue('extract', draft['id'], {'definitionId': draft['id']})
    requests = []

    def respond(request):
        requests.append(request)
        failure = httpx.ReadTimeout if len(requests) < 3 else httpx.RemoteProtocolError
        raise failure('connection interrupted', request=request)

    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    assert run_once(service, gateway)
    job = next(item for item in service.store.jobs() if item['id'] == queued['jobId'])
    assert job['status'] == 'FAILED' and job['error']['code'] == 'MODEL_UNAVAILABLE'
    assert len(requests) == 3
    unchanged = service.store.get('definition', draft['id'])
    assert unchanged['status'] == 'DRAFT' and unchanged['nodes'] == draft['nodes']
    assert unchanged['candidates'] == []
    assert service.store.read_blob(attachment['sha256']) == path.read_bytes()
