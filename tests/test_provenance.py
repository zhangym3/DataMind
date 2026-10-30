"""Trust boundaries: model destination, receipts, definition lineage and restarts."""
import copy
import json

import httpx
import pytest

from test_acceptance import Vault, complete, expect
from test_integration import prepared, submit
from insight_agent.agent import execute_run
from insight_agent.core import digest, dumps
from insight_agent.definitions import create_draft, extract, publish, save_candidates
from insight_agent.engine import validate_rule
from insight_agent.files import import_file
from insight_agent.models import Gateway, retry_delay, save_profile
from insight_agent.service import AppService
from insight_agent.skills import invoke, registry, runtime_manifest
from insight_agent.worker import run_once


def configured(tmp_path):
    service = AppService(tmp_path / 'workspace')
    vault = Vault()
    data = {'name': 'synthetic', 'provider': 'test', 'protocol': 'chat', 'baseUrl': 'https://example.test/v1', 'model': 'fixture'}
    profile = save_profile(service.store, data, 'synthetic-secret-only', vault)
    return service, vault, data | {'id': profile['id']}


@pytest.mark.parametrize('key,value', [('baseUrl', 'https://different.test/v1'), ('protocol', 'responses'), ('provider', 'different')])
def test_changed_target_never_reuses_previous_credential(tmp_path, key, value):
    service, vault, data = configured(tmp_path)
    original = service.store.get('model', data['id'])['credentialRef']
    same = save_profile(service.store, data | {'model': 'other-model'}, credentials=vault)
    assert same['hasKey'] and vault.get(original)
    changed = save_profile(service.store, data | {key: value}, credentials=vault)
    assert not changed['hasKey'] and vault.get(original) is None
    expect('MODEL_KEY_MISSING', lambda: Gateway(service.store, vault).test(changed['id']))


@pytest.mark.parametrize('hostile', [False, True])
def test_model_receipt_retains_usage_without_response_content(tmp_path, hostile):
    service, vault, data = configured(tmp_path)
    request_id = 'synthetic-secret-only' if hostile else 'req_fixture_123'
    response = {'model': request_id if hostile else 'fixture-1', 'usage': {'prompt_tokens': 17, 'completion_tokens': 4, 'total_tokens': 21, 'private': 'synthetic-secret-only', 'input_tokens': -1, 'output_tokens': True}, 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
    gateway = Gateway(service.store, vault, httpx.MockTransport(lambda _: httpx.Response(200, json=response, headers={'x-request-id': request_id})))
    gateway.test(data['id'])
    receipt = service.store.list('model_call')[0]
    assert receipt['status'] == 'SUCCEEDED' and receipt['usageSource'] == 'PROVIDER_RESPONSE'
    assert receipt['usage'] == {'prompt_tokens': 17, 'completion_tokens': 4, 'total_tokens': 21}
    assert receipt['requestId'] == (None if hostile else request_id)
    assert receipt['responseModel'] == (None if hostile else 'fixture-1')
    assert receipt['runtimeHash'] == runtime_manifest()['sha256']
    assert len(receipt['promptHash']) == 64
    assert all(b'synthetic-secret-only' not in path.read_bytes() for path in service.store.home.rglob('*') if path.is_file())


@pytest.mark.parametrize('response', [[], {'choices': []}, {'choices': [None]}])
def test_malformed_provider_envelope_is_explicit_terminal_error(tmp_path, response):
    service, vault, data = configured(tmp_path)
    gateway = Gateway(service.store, vault, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    expect('MODEL_SCHEMA_INVALID', lambda: gateway.test(data['id']))
    assert service.store.list('model_call')[0]['errorCode'] == 'MODEL_SCHEMA_INVALID'


def test_unregistered_skill_and_wrong_tool_cannot_execute():
    expect('SKILL_FORBIDDEN', lambda: invoke('attachment-skill', 'exec'))
    expect('SKILL_FORBIDDEN', lambda: invoke('surgery-metric-calc', 'extract'))


def test_retry_after_is_honored_and_long_wait_stops():
    assert retry_delay(0, '2') == 2
    assert 0.25 <= retry_delay(0, 'invalid') <= 0.4
    expect('MODEL_RATE_LIMIT', lambda: retry_delay(0, '60'))


def test_changed_runtime_cannot_resume_or_offline_recalculate(prepared):
    service, session, _, parameters = prepared
    submit(service, session, parameters)
    job = service.store.claim()
    run_id = service.process_turn(job['payload']['turnId'], job)
    run = service.store.get('run', run_id)
    run['runtime']['graphVersion'] = 'unknown-future-graph'
    with service.store.transaction() as db:
        service.store.put('run', run, db)
    expect('VERSION_MISMATCH', lambda: execute_run(service.store, run_id, job))
    assert not service.store.get('run', run_id)['committed']


def test_offline_recalculation_rejects_old_runtime(prepared):
    service, source = complete(prepared)
    source['runtime']['promptHash'] = '0' * 64
    with service.store.transaction() as db:
        service.store.put('run', source, db)
    session = service.store.get('session', source['sessionId'])
    submit(service, session, {}, 'RECALCULATE_FROZEN', source['id'], 'changed-runtime')
    run_once(service)
    assert len(service.store.list('run')) == 1
    assert service.store.jobs()[0]['error']['code'] == 'VERSION_MISMATCH'


def test_definition_model_trace_then_manual_revision_is_immutable(prepared):
    service, _, original, _ = prepared
    vault = Vault()
    save_profile(service.store, {'name': 'fixture', 'provider': 'test', 'protocol': 'chat', 'baseUrl': 'https://example.test/v1', 'model': 'fixture'}, 'synthetic-only', vault)
    draft = create_draft(service.store, original['attachmentId'], '新版本', original['id'])
    response = {'choices': [{'message': {'content': json.dumps({'candidates': original['candidates'], 'coverage': original['coverage']}, ensure_ascii=False)}}]}
    gateway = Gateway(service.store, vault, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    draft = extract(service.store, draft['id'], gateway, 'definition-budget')
    model_trace = copy.deepcopy(draft['provenanceHistory'][0])
    assert model_trace['kind'] == 'MODEL_EXTRACTION' and model_trace['calls'][0]['profileRevision'] >= 1
    assert model_trace['calls'][0]['status'] == 'SUCCEEDED'
    draft = save_candidates(service.store, draft['id'], draft['candidates'], draft['coverage'], draft['revision'])
    published = publish(service.store, draft['id'], draft['revision'], '合成审核人', True)
    manifest = published['manifest']
    assert manifest['provenanceHistory'][0] == model_trace
    assert manifest['provenanceHistory'][1]['kind'] == 'MANUAL_REVIEW'
    assert manifest['ruleDigests'] and manifest['evidenceDocuments'][0]['kind'] == 'WORD'
    expect('IMMUTABLE_VERSION', lambda: extract(service.store, draft['id'], gateway, 'another-budget'))
    assert 'synthetic-only' not in dumps(manifest)


def test_legacy_doc_extraction_projects_evidence_and_keeps_parser_metadata_local(tmp_path, monkeypatch):
    service, vault, _ = configured(tmp_path)
    monkeypatch.setattr('insight_agent.word_spec._legacy_text', lambda _: '统计口径：统计手术数量。\n分母：全部手术台次。')
    path = tmp_path / '指标说明.doc'
    path.write_bytes(bytes.fromhex('D0CF11E0A1B11AE1') + b'synthetic-doc')
    attachment = import_file(service.store, path, 'library')
    draft = create_draft(service.store, attachment['id'], '旧版文档指标')
    assert all(node['source'] == 'LEGACY_DOC_TEXT' for node in draft['nodes'])
    requests = []

    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])['data']
        requests.append(payload)
        registered = registry()['surgery-word-spec']
        assert digest(payload['instruction']) == registered['extractionPromptHash']
        assert all(set(node) == {'locator', 'text', 'quoteSha256', 'documentSha256'} for node in payload['nodes'])
        assert [node['text'] for node in payload['nodes']] == [node['text'] for node in draft['nodes']]
        assert all(node['documentSha256'] == attachment['sha256'] for node in payload['nodes'])
        response = {
            'candidates': [{'id': 'surgery_count', 'name': '手术数量', 'status': 'SKIP', 'reason': '尚需明确实体键与缺失政策', 'locators': [node['locator'] for node in draft['nodes']]}],
            'coverage': [{'locator': node['locator'], 'disposition': 'CANDIDATE', 'candidateIds': ['surgery_count'], 'reason': '已读取全文规则'} for node in draft['nodes']],
        }
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(response, ensure_ascii=False)}}]})

    gateway = Gateway(service.store, vault, httpx.MockTransport(respond))
    result = extract(service.store, draft['id'], gateway, 'legacy-extract')
    assert len(requests) == 1 and len(result['coverage']) == 2
    assert result['candidates'][0]['id'] == 'surgery_count'
    assert result['nodes'] == draft['nodes'] and result['status'] == 'DRAFT'
    assert result['provenanceHistory'][-1]['calls'][0]['status'] == 'SUCCEEDED'
    provenance = result['provenanceHistory'][-1]
    assert provenance['extractionPromptHash'] == digest(requests[0]['instruction'])
    assert provenance['extractionPromptVersion'] == registry()['surgery-word-spec']['extractionPromptVersion']


def test_extraction_prompt_change_invalidates_runtime_identity(monkeypatch):
    import insight_agent.skills as skills

    before = skills.runtime_manifest()
    before_skills = {entry['id']: entry for entry in before['skills']}
    monkeypatch.setattr(skills, 'WORD_SPEC_EXTRACTION_INSTRUCTION', skills.WORD_SPEC_EXTRACTION_INSTRUCTION + ' Updated extraction policy.')
    after = skills.runtime_manifest()
    after_skills = {entry['id']: entry for entry in after['skills']}

    assert before['sha256'] != after['sha256']
    assert before_skills['surgery-word-spec']['sha256'] != after_skills['surgery-word-spec']['sha256']
    assert before_skills['surgery-word-spec']['extractionPromptHash'] != after_skills['surgery-word-spec']['extractionPromptHash']
    assert all(before_skills[name] == after_skills[name] for name in before_skills if name != 'surgery-word-spec')


def test_canonical_field_cannot_change_entity_or_unit(prepared):
    _, _, definition, _ = prepared
    rule = copy.deepcopy(definition['candidates'][0]['rule'])
    rule['requiredFields'][0]['unit'] = 'day'
    expect('OPERATOR_TYPE_MISMATCH', lambda: validate_rule(rule))
