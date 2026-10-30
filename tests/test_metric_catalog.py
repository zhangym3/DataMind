"""Standalone Word reading: scope, evidence, batching and durable recovery."""
import copy
import json

import httpx
import pytest
from docx import Document

from insight_agent.core import AppError, sha
from insight_agent.models import Gateway, save_profile
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_acceptance import Vault


class Unavailable:
    def call(self, *args):
        raise AppError('MODEL_UNAVAILABLE', '合成模型失败')


@pytest.fixture
def uploaded(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session('指标说明独立解析')
    document = Document()
    document.add_paragraph('统计口径：手术事件，按科室分组。')
    for index in range(1, 8):
        document.add_paragraph(f'指标{index}：入室至麻醉超过{index * 10}分钟的台数；排除条件：移植手术。')
    path = tmp_path / '指标说明.docx'
    document.save(path)
    service.import_paths([str(path)], session['id'])
    assert run_once(service)
    # Word-only uploads automatically start catalog reading; retry remains available.
    assert run_once(service, Unavailable())
    definition = service.session_metrics(session['id'])
    assert definition['sessionId'] == session['id']
    return service, session, definition


class CatalogGateway:
    def __init__(self, fail_call=None, mutate=None):
        self.calls = []
        self.fail_call = fail_call
        self.mutate = mutate

    def call(self, stage, payload, schema, budget_id):
        assert stage == 'metric_catalog'
        self.calls.append(copy.deepcopy(payload))
        if len(self.calls) == self.fail_call:
            raise AppError('MODEL_UNAVAILABLE', '合成的批次失败')
        if not payload['items']:
            result = {'items': [{'name': f'指标{i}', 'category': '时间监测', 'locators': [n['locator']]}
                                for i, n in enumerate(payload['nodes'][1:], 1)],
                      'contextLocators': [payload['nodes'][0]['locator']], 'nonMetricLocators': []}
        else:
            result = {'items': [{
                'id': i['id'], 'unit': '台', 'dimensions': ['科室'], 'timeWindow': '',
                'formula': '符合条件的手术事件数', 'conditions': ['入室至麻醉严格超过原文阈值'],
                'exclusions': ['移植手术'], 'unresolved': ['统计日期按哪个事件归属？'],
                'locators': [payload['nodes'][0]['locator'], *i['locators']],
            } for i in payload['items']]}
        if self.mutate:
            self.mutate(result, payload)
        return result


def parse(service, session, gateway):
    queued = service.parse_session_metrics(session['id'])
    assert run_once(service, gateway)
    job = next(j for j in service.store.jobs() if j['id'] == queued['jobId'])
    return job, service.session_metric_catalog(session['id'])


def test_word_only_batches_persist_exact_evidence_and_cache(uploaded):
    service, session, definition = uploaded
    gateway = CatalogGateway()
    queued = service.parse_session_metrics(session['id'])
    assert service.parse_session_metrics(session['id'])['jobId'] == queued['jobId']
    assert run_once(service, gateway)
    parsed = service.session_metric_catalog(session['id'])
    assert parsed['status'] == 'READY' and len(parsed['items']) == 7
    assert [len(c['items']) for c in gateway.calls] == [0, 4, 3]
    assert all(i['status'] == 'NEEDS_CLARIFICATION' for i in parsed['items'])
    assert parsed['items'][-1]['unit'] == '台'  # Verifies later batches persist too.
    nodes = {n['locator']: n for n in definition['nodes']}
    for item in parsed['items']:
        assert item['unresolved'] and item['exclusions'] == ['移植手术']
        for evidence in item['sourceEvidence']:
            assert evidence['documentSha256'] == definition['documentHash']
            assert evidence['quote'] == nodes[evidence['locator']]['text']
            assert evidence['quoteSha256'] == sha(evidence['quote'].encode())
    assert set(parsed['coverage'][0]['candidateIds']) == {i['id'] for i in parsed['items']}
    assert service.parse_session_metrics(session['id']) == {'catalogId': parsed['id'], 'status': 'READY'}
    assert service.store.get('definition', definition['id']) == definition
    assert service.store.get('session', session['id'])['state'] == 'DRAFT'
    assert not service.store.list('run')
    assert service.session_detail(session['id'])['metricDefinition']['catalogSummary'] == {
        'status': 'READY', 'count': 7, 'completed': 7}


def test_failed_batch_resumes_only_pending_after_service_restart(uploaded):
    service, session, _ = uploaded
    failed, partial = parse(service, session, CatalogGateway(fail_call=3))
    assert failed['status'] == 'FAILED' and partial['status'] == 'PARTIAL'
    assert [i['status'] for i in partial['items']].count('PENDING') == 3
    completed = copy.deepcopy(partial['items'][:4])
    service = AppService(service.store.home)
    gateway = CatalogGateway()
    job, ready = parse(service, session, gateway)
    assert job['status'] == 'SUCCEEDED' and ready['status'] == 'READY'
    assert len(gateway.calls) == 1 and len(gateway.calls[0]['items']) == 3
    assert ready['items'][:4] == completed


@pytest.mark.parametrize('fault,code', [
    ('coverage', 'DOCUMENT_COVERAGE_INCOMPLETE'),
    ('unknown', 'EVIDENCE_INVALID'),
    ('duplicate', 'INVALID_REFERENCE'),
    ('schema', 'MODEL_SCHEMA_INVALID'),
])
def test_invalid_output_never_marks_catalog_complete(uploaded, fault, code):
    service, session, definition = uploaded

    def mutate(result, payload):
        if fault == 'coverage' and not payload['items']:
            result['contextLocators'] = []
        if payload['items']:
            if fault == 'unknown':
                result['items'][0]['locators'] = ['other-document/p:1']
            elif fault == 'duplicate':
                result['items'][1]['id'] = result['items'][0]['id']
            elif fault == 'schema':
                result['items'][0]['unit'] = 42

    job, parsed = parse(service, session, CatalogGateway(mutate=mutate))
    assert job['status'] == 'FAILED' and job['error']['code'] == code
    assert parsed is None if fault == 'coverage' else parsed['status'] == 'PARTIAL'
    if parsed:
        assert all(i['status'] == 'PENDING' for i in parsed['items'])
    assert service.store.get('definition', definition['id']) == definition


def test_real_gateway_projects_nodes_and_records_model_receipts(uploaded):
    service, session, definition = uploaded
    vault = Vault()
    save_profile(service.store, {'name': 'Synthetic', 'provider': 'test', 'protocol': 'chat',
                                'baseUrl': 'https://example.test/v1', 'model': 'fixture'}, 'synthetic-key', vault)
    fixture = CatalogGateway()

    def respond(request):
        body = json.loads(request.content)
        prompt = json.loads(body['messages'][-1]['content'])
        assert 'synthetic-key' not in request.content.decode()
        payload = prompt['data']
        assert all(set(n) == {'locator', 'text'} for n in payload['nodes'])
        assert payload['nodes'][0]['text'] == definition['nodes'][0]['text']
        result = fixture.call(prompt['stage'], payload, None, None)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(result, ensure_ascii=False)}}]})

    job, parsed = parse(service, session, Gateway(service.store, vault, httpx.MockTransport(respond)))
    assert job['status'] == 'SUCCEEDED', job
    receipts = service.store.list('model_call')
    assert set(parsed['modelCallIds']) == {c['id'] for c in receipts} and len(receipts) == 3


@pytest.mark.parametrize('change,code', [('removed', 'SCOPE_FORBIDDEN'), ('amended', 'REVISION_CONFLICT'),
                                      ('cancelled', 'CANCELLED')])
def test_scope_and_cancel_checked_after_model_call(uploaded, change, code):
    service, session, definition = uploaded

    def mutate(result, payload):
        if payload['items']:
            if change == 'cancelled':
                job = next(j for j in service.store.jobs() if j['kind'] == 'metric_catalog')
                service.cancel(job['id'])
            else:
                with service.store.transaction() as db:
                    if change == 'removed':
                        current = service.store.get('session', session['id'], db)
                        service.store.put('session', current | {'definitionId': None, 'attachmentIds': []}, db)
                    else:
                        current = service.store.get('definition', definition['id'], db)
                        current['nodes'][0]['text'] += '新增说明'
                        service.store.put('definition', current, db)

    queued = service.parse_session_metrics(session['id'])
    assert run_once(service, CatalogGateway(mutate=mutate))
    job = next(j for j in service.store.jobs() if j['id'] == queued['jobId'])
    assert job['status'] in ('FAILED', 'CANCELLED') and job['error']['code'] == code
    record = service.store.get('metric_catalog', definition['id'])
    assert record['status'] == 'PARTIAL' and all(i['status'] == 'PENDING' for i in record['items'])


def test_no_document_and_foreign_session_are_rejected(uploaded):
    service, _, definition = uploaded
    other = service.create_session('其他对话')
    assert service.session_metric_catalog(other['id']) is None
    with pytest.raises(AppError, match='请先上传'):
        service.parse_session_metrics(other['id'])
    with service.store.transaction() as db:
        service.store.put('session', other | {'definitionId': definition['id']}, db)
    with pytest.raises(AppError) as error:
        service.session_metric_catalog(other['id'])
    assert error.value.code == 'SCOPE_FORBIDDEN'


@pytest.mark.parametrize('command', ['解析指标说明', '提取指标', '/parse-metrics', '请解析指标说明'])
def test_local_parse_command_queues_without_intent_model_or_session_lock(uploaded, command):
    service, session, _ = uploaded
    before = len([j for j in service.store.jobs() if j['kind'] == 'metric_catalog'])
    session = service.store.get('session', session['id'])
    response = service.submit_user_turn(session['id'], session['revision'], command, command)
    assert run_once(service, Unavailable())
    turn = service.store.get('turn', response['turnId'])
    assert turn['state'] == 'COMPLETED' and turn['resolvedAction'] == 'METRIC_SPEC_PARSE'
    assert len([j for j in service.store.jobs() if j['kind'] == 'metric_catalog']) == before + 1
    assert service.store.get('session', session['id'])['state'] == 'DRAFT'
    assert not service.store.list('run')


@pytest.mark.parametrize('changed', ['prompt', 'detail_prompt', 'schema', 'source'])
def test_changed_extraction_inputs_invalidate_cache(uploaded, monkeypatch, changed):
    service, session, definition = uploaded
    _, ready = parse(service, session, CatalogGateway())
    assert ready['status'] == 'READY'
    if changed == 'source':
        with service.store.transaction() as db:
            definition['nodes'][0]['text'] += '口径补充'
            service.store.put('definition', definition, db)
    else:
        field = {'prompt': 'WORD_SPEC_CATALOG_INSTRUCTION', 'detail_prompt': 'DETAIL_INSTRUCTION',
                 'schema': 'SCHEMA_HASH'}[changed]
        monkeypatch.setattr('insight_agent.metric_catalog.' + field, 'changed')
    assert service.session_metric_catalog(session['id']) is None
    assert 'jobId' in service.parse_session_metrics(session['id'])


def test_locked_published_definition_is_not_changed_by_reading(uploaded):
    service, session, definition = uploaded
    with service.store.transaction() as db:
        definition.update(status='PUBLISHED', manifest={'frozen': True}, manifestHash='immutable')
        definition = service.store.put('definition', definition, db)
        current = service.store.get('session', session['id'], db)
        service.store.put('session', current | {'state': 'LOCKED', 'baselineHash': 'frozen'}, db)
    job, parsed = parse(service, session, CatalogGateway())
    assert job['status'] == 'SUCCEEDED' and parsed['status'] == 'READY'
    assert service.store.get('definition', definition['id']) == definition
    assert service.store.get('session', session['id'])['baselineHash'] == 'frozen'
    assert not service.store.list('run')


def test_archived_session_cannot_start_reading(uploaded):
    service, session, _ = uploaded
    jobs_before = service.store.jobs()
    with service.store.transaction() as db:
        current = service.store.get('session', session['id'], db)
        service.store.put('session', current | {'archived': True}, db)
    with pytest.raises(AppError) as error:
        service.parse_session_metrics(session['id'])
    assert error.value.code == 'SESSION_ARCHIVED'
    assert service.store.jobs() == jobs_before


def test_pending_catalog_protects_definition_and_deletion_removes_catalog(uploaded):
    service, session, definition = uploaded
    queued = service.parse_session_metrics(session['id'])
    with service.store.transaction() as db:
        current = service.store.get('session', session['id'], db)
        service.store.put('session', current | {'definitionId': None}, db)
    with pytest.raises(AppError) as error:
        service.delete_definition(definition['id'], definition['revision'], '确认删除')
    assert error.value.code == 'DEFINITION_BUSY'
    service.cancel(queued['jobId'])
    with service.store.transaction() as db:
        service.store.put('metric_catalog', {'id': definition['id'], 'definitionId': definition['id'],
                                           'sessionId': session['id'], 'status': 'PARTIAL'}, db)
    service.delete_definition(definition['id'], definition['revision'], '确认删除')
    assert not service.store.list('metric_catalog')
