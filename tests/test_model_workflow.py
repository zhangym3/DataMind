"""Protocol integration only: simulated responses are not provider acceptance."""
import json

import httpx
import pytest

from insight_agent.definitions import create_draft, publish, save_candidates
from insight_agent.models import Gateway, save_profile
from insight_agent.worker import run_once
from test_acceptance import Vault
from test_integration import prepared, submit  # noqa: F401


def workflow(prepared, protocol='chat', review='PASS', explanation='该结果依据已批准的口径计算，详细依据可查看原文。'):
    service, session, original, parameters = prepared
    vault = Vault()
    save_profile(service.store, {'name': 'Synthetic', 'provider': 'test', 'protocol': protocol,
                               'baseUrl': 'https://example.test/v1', 'model': 'fixture'}, 'synthetic-key', vault)
    stages = []

    def respond(request):
        body = json.loads(request.content)
        prompt = json.loads(body['input'] if protocol == 'responses' else body['messages'][-1]['content'])
        stage = prompt['stage']
        stages.append(stage)
        assert 'synthetic-key' not in request.content.decode()
        if stage == 'definition':
            value = {'candidates': original['candidates'], 'coverage': original['coverage']}
        elif stage == 'intent':
            value = {'action': 'ANALYZE', 'parameters': parameters}
        elif stage == 'review':
            value = {'status': review, 'reason': '合成响应，仅验证协议调用和门禁。'}
            assert 'rows' not in json.dumps(prompt['data']['results'])
        else:
            assert stage == 'explain'
            value = {'explanation': explanation, 'metricIds': [original['candidates'][0]['id']]}
        raw = json.dumps(value, ensure_ascii=False)
        if protocol == 'responses':
            envelope = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': raw}]}]}
        elif protocol == 'anthropic':
            envelope = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': raw}]}
        else:
            envelope = {'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]}
        return httpx.Response(200, json=envelope)

    def execute():
        return run_once(service, Gateway(service.store, vault, httpx.MockTransport(respond)))

    draft = create_draft(service.store, original['attachmentId'], '合成在线提取', original['id'])
    service.queue('extract', draft['id'], {'definitionId': draft['id']})
    execute()
    assert service.store.jobs()[0]['status'] == 'SUCCEEDED', service.store.jobs()[0]
    draft = service.store.get('definition', draft['id'])
    assert draft['status'] == 'DRAFT' and draft['provenanceHistory'][-1]['kind'] == 'MODEL_EXTRACTION'
    draft = save_candidates(service.store, draft['id'], draft['candidates'], draft['coverage'], draft['revision'], 'AI_REQUIRED')
    published = publish(service.store, draft['id'], draft['revision'], '合成测试审核人', True)
    session = service.update_session(session['id'], session['revision'], {'definitionId': published['id']})
    # Binding a different version clears mappings; confirm sources for its rules.
    assert session['mappings'] == {} and session['mappingRevisionId'] is None
    mapping = service.suggest_mapping(session['id'])
    for value in mapping.values():
        value['coverage'] = {'start': parameters['date_start'], 'end': parameters['date_end'],
                             'complete': True, 'evidence': '独立合成全量事件，已核对新版本字段'}
    session = service.update_session(session['id'], session['revision'], {'mappings': mapping})
    service.submit_user_turn(session['id'], session['revision'], 'auto', '分析 2026-10-01 至 2026-11-01 的指标', 'AUTO')
    execute()
    return service, stages, execute


@pytest.mark.parametrize('protocol', ['chat', 'responses', 'anthropic'])
def test_simulated_model_full_workflow_and_offline_review_reuse(prepared, protocol):
    service, stages, execute = workflow(prepared, protocol)
    run = service.store.list('run')[0]
    assert run['committed'] and run['results'][0]['groups'][0]['displayValue'] == '2', service.store.jobs()[0]
    assert run['review']['status'] == 'PASS' and run['contract']['reviewPolicy'] == 'AI_REQUIRED'
    session = service.store.get('session', run['sessionId'])
    submit(service, session, {}, 'EXPLAIN', run['id'], 'explain')
    execute()
    assert service.store.jobs()[0]['status'] == 'SUCCEEDED', service.store.jobs()[0]
    assert service.store.list('message')[0]['sourceRunId'] == run['id']
    session = service.store.get('session', session['id'])
    submit(service, session, {}, 'REGENERATE', run['id'], 'regenerate')
    user_count = sum(m['role'] == 'user' for m in service.store.list('message'))
    execute()
    assert service.store.jobs()[0]['status'] == 'SUCCEEDED'
    assert sum(m['role'] == 'user' for m in service.store.list('message')) == user_count
    assert stages == ['definition', 'intent', 'review', 'explain', 'explain']
    service.save_settings({'modelEnabled': False})
    session = service.store.get('session', session['id'])
    submit(service, session, {}, 'RECALCULATE_FROZEN', run['id'], 'offline')
    execute()
    recalculated = service.store.list('run')[0]
    assert recalculated['committed'] and recalculated['review']['mode'] == 'REUSED_PRIOR_REVIEW'
    assert recalculated['resultDigest'] == run['resultDigest']
    assert len(stages) == 5


def test_failed_online_review_never_publishes_numbers(prepared):
    service, stages, _ = workflow(prepared, review='FAIL')
    run = service.store.list('run')[0]
    assert not run['committed'] and run['status'] == 'FAILED'
    assert run['technicalFailure']['code'] == 'AI_REVIEW_FAILED'
    assert 'results' not in run
    assert not any(m['role'] == 'assistant' for m in service.store.list('message'))
    assert stages == ['definition', 'intent', 'review']


def test_explanation_with_new_number_is_rejected(prepared):
    service, _, execute = workflow(prepared, explanation='该结果比上月增加了999台。')
    run = service.store.list('run')[0]
    session = service.store.get('session', run['sessionId'])
    submit(service, session, {}, 'EXPLAIN', run['id'], 'explain')
    before = len(service.store.list('message'))
    execute()
    job = service.store.jobs()[0]
    assert job['status'] == 'FAILED' and job['error']['code'] == 'EXPLANATION_UNSUPPORTED_FACT'
    assert len(service.store.list('message')) == before


def test_same_clock_tick_keeps_message_order_and_queue_fifo(tmp_path, monkeypatch):
    from insight_agent.store import Store
    store = Store(tmp_path / 'ws')
    monkeypatch.setattr('insight_agent.store.now', lambda: '2026-10-29T00:00:00+00:00')
    ids = iter(['job_z_first', 'job_a_second'])
    monkeypatch.setattr('insight_agent.store.uid', lambda prefix='': next(ids))
    with store.transaction() as db:
        store.put('message', {'id': 'message_a_first', 'role': 'user'}, db)
        store.put('message', {'id': 'message_z_second', 'role': 'assistant'}, db)
        first = store.enqueue(db, 'test', 'test', {})
        second = store.enqueue(db, 'test', 'test', {})
    assert [m['role'] for m in reversed(store.list('message'))] == ['user', 'assistant']
    assert [j['id'] for j in store.jobs()] == [second, first]
    assert store.claim()['id'] == first
