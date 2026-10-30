"""Local preparation, durable intent, explicit history and immutable result queries."""
import copy

import pytest

from insight_agent.conversation import LOCAL_COMMANDS, query_result
from insight_agent.core import AppError
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_integration import prepared, submit  # noqa: F401


@pytest.mark.parametrize('command', ['帮助', '查看附件', '查看数据', '查看口径', '查看设置'])
def test_preparation_commands_without_model_do_not_lock(tmp_path, command):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()
    response = service.submit_user_turn(session['id'], session['revision'], command, command)
    run_once(service)
    turn = service.store.get('turn', response['turnId'])
    assert turn['state'] == 'COMPLETED' and turn['resolvedAction'] == LOCAL_COMMANDS[command]
    assert service.store.list('intent')[0]['method'] == 'LOCAL_COMMAND'
    assert service.store.list('history_context')[0]['historyTokens'] == 0
    assert service.store.get('session', session['id'])['state'] == 'DRAFT'
    assert not service.store.list('run')
    assert service.cancel(response['jobId'])['status'] == 'ALREADY_COMMITTED'


def test_explicit_history_inheritance_and_query_do_not_recompute(prepared):
    service, session, _, parameters = prepared
    submit(service, session, parameters)
    run_once(service)
    source = service.store.list('run')[0]
    session = service.store.get('session', session['id'])
    submit(service, session, {'date_start': '2026-10-02'}, 'ANALYZE', source['id'], 'followup')
    run_once(service)
    current = service.store.list('run')[0]
    assert current['committed'], service.store.jobs()[0]
    assert current['sourceRunId'] is None and current['historySourceRunId'] == source['id']
    assert current['contract']['parameters'] == parameters | {'date_start': '2026-10-02'}
    history = service.store.get('history_context', current['historyContextId'])
    assert history['inheritedParameters'] == {'date_end': parameters['date_end']}
    assert history['notInherited'] == {'date_start': 'EXPLICIT_PARAMETER'}
    session = service.store.get('session', session['id'])
    submit(service, session, {}, 'RESULT_QUERY', source['id'], 'query')
    run_once(service)
    assert len(service.store.list('run')) == 2
    query = service.store.list('result_query')[0]
    assert query['rows'][0]['displayValue'] == '2' and query['sourceResultDigest'] == source['resultDigest']
    assert service.store.list('message')[0]['queryId'] == query['id']


def test_foreign_or_missing_result_reference_rejected_and_turn_terminal(prepared):
    service, session, _, parameters = prepared
    submit(service, session, parameters)
    run_once(service)
    source = service.store.list('run')[0]
    other = service.create_session()
    response = submit(service, other, {}, 'RESULT_QUERY', source['id'], 'foreign')
    run_once(service)
    assert service.store.get('turn', response['turnId'])['state'] == 'REJECTED'
    assert service.store.jobs()[0]['error']['code'] == 'SCOPE_FORBIDDEN'
    response = submit(service, other, {}, 'RESULT_QUERY', None, 'missing')
    run_once(service)
    assert service.store.jobs()[0]['error']['code'] == 'AMBIGUOUS_RESULT_REFERENCE'
    assert not service.session_detail(other['id'])['messages']


def test_result_sort_uses_exact_fractions_and_cannot_invent_dimensions():
    source = {'id': 'r', 'resultDigest': 'hash', 'mode': 'ANALYZE', 'results': [
        {'metricId': 'm', 'name': 'metric', 'unit': '元', 'groups': [
            {'group': {'dept': 'a'}, 'outcome': 'VALUE', 'displayValue': '1.00', 'exactNumerator': '10000000000000001', 'exactDenominator': '10000000000000000'},
            {'group': {'dept': 'b'}, 'outcome': 'VALUE', 'displayValue': '1.00', 'exactNumerator': '1', 'exactDenominator': '1'},
            {'group': {'dept': 'c'}, 'outcome': 'SKIP', 'reasonCode': 'ZERO_DENOMINATOR'}]}]}
    before = copy.deepcopy(source)
    query, _ = query_result(source, {'sortBy': 'value', 'limit': 1})
    assert query['rows'][0]['group'] == {'dept': 'b'} and query['total'] == 3
    query, _ = query_result(source, {'groupFilters': {'dept': 'a'}})
    assert query['total'] == 1 and source == before
    with pytest.raises(AppError, match='筛选字段'):
        query_result(source, {'groupFilters': {'patient': 'x'}})
    with pytest.raises(AppError, match='新日期'):
        query_result(source, {'date_start': '2026-10-01'})


def test_committed_local_reply_survives_worker_crash(prepared):
    service, session, _, _ = prepared
    response = service.submit_user_turn(session['id'], session['revision'], 'help', '帮助')
    job = service.store.claim()
    service.process_turn(response['turnId'], job)
    service.store.expire(job['id'], job['lease'])
    assert service.store.jobs()[0]['status'] == 'SUCCEEDED'
    assert service.store.get('turn', response['turnId'])['state'] == 'COMPLETED'
    before = len(service.store.list('message'))
    service.process_turn(response['turnId'], job)
    assert len(service.store.list('message')) == before


def test_cancel_pending_and_recover_sets_turn_terminal(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()
    response = service.submit_user_turn(session['id'], session['revision'], 'one', '帮助')
    service.cancel(response['jobId'])
    assert service.store.get('turn', response['turnId'])['state'] == 'CANCELLED'
    response = service.submit_user_turn(session['id'], session['revision'], 'two', '帮助')
    service.store.claim()
    service.store.recover(False)
    assert service.store.get('turn', response['turnId'])['state'] == 'CANCELLED'


def test_mapping_revisions_are_immutable_and_bound_to_runs(prepared):
    service, session, definition, parameters = prepared
    first = service.store.get('mapping_revision', session['mappingRevisionId'])
    mappings = copy.deepcopy(session['mappings'])
    next(iter(mappings.values()))['coverage']['evidence'] = '第二次明确核对完整性'
    session = service.update_session(session['id'], session['revision'], {'mappings': mappings}, {'actor': '审核人', 'reason': '更新全量范围依据'})
    second = service.store.get('mapping_revision', session['mappingRevisionId'])
    assert second['parentId'] == first['id'] and second['mappingHash'] != first['mappingHash']
    assert service.store.get('mapping_revision', first['id']) == first
    assert second['fields'][0]['valueValidation'] == 'DEFERRED_UNTIL_RUN'
    submit(service, session, parameters)
    run_once(service)
    contract = service.store.list('run')[0]['contract']
    assert contract['mappingRevisionId'] == second['id'] and contract['mappingRevisionDigest'] == second['digest']


def test_recover_committed_reply_with_auto_resume_disabled(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()
    response = service.submit_user_turn(session['id'], session['revision'], 'query', '帮助')
    job = service.store.claim()
    service.process_turn(response['turnId'], job)
    service.store.recover(False)
    assert service.store.jobs()[0]['status'] == 'SUCCEEDED'
    assert service.store.get('turn', response['turnId'])['state'] == 'COMPLETED'


def test_clear_conversation_removes_messages_and_draft_but_preserves_data_and_runs(prepared):
    service, session, _, parameters = prepared
    submit(service, session, parameters)
    run_once(service)
    run_ids = {run['id'] for run in service.store.list('run')}
    session = service.store.get('session', session['id'])
    response = service.submit_user_turn(session['id'], session['revision'], 'help', '帮助')
    run_once(service)
    session = service.store.get('session', session['id'])
    session = service.update_session(session['id'], session['revision'], {'draft': '未发送的问题'})
    attachment_ids = list(session['attachmentIds'])
    assert service.session_detail(session['id'])['messages']

    cleared = service.clear_conversation(session['id'], session['revision'])

    detail = service.session_detail(session['id'])
    assert cleared['draft'] == ''
    assert detail['messages'] == []
    assert detail['session']['draft'] == ''
    assert detail['session']['attachmentIds'] == attachment_ids
    assert {run['id'] for run in detail['runs']} == run_ids
    assert any(event['type'] == 'conversation.cleared' for event in detail['activities']['events'])
    assert service.store.get('turn', response['turnId'])['state'] == 'COMPLETED'


def test_clear_conversation_rejects_active_jobs_and_stale_revision(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session()
    response = service.submit_user_turn(session['id'], session['revision'], 'pending', '帮助')
    with pytest.raises(AppError) as error:
        service.clear_conversation(session['id'], session['revision'])
    assert error.value.code == 'SESSION_BUSY'
    service.cancel(response['jobId'])

    session = service.store.get('session', session['id'])
    updated = service.update_session(session['id'], session['revision'], {'draft': '内容'})
    with pytest.raises(AppError) as error:
        service.clear_conversation(session['id'], session['revision'])
    assert error.value.code == 'REVISION_CONFLICT'
    assert updated['draft'] == '内容'


def test_data_conversation_can_be_deleted_without_archive(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session('待删除数据对话')

    result = service.dispatch('session.delete', {
        'session_id': session['id'],
        'revision': session['revision'],
        'confirmation': '确认删除',
    })

    assert result == {'deletedSessionId': session['id']}
    with pytest.raises(AppError) as error:
        service.store.get('session', session['id'])
    assert error.value.code == 'NOT_FOUND'


def test_data_conversation_delete_rejects_identifier_as_confirmation(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session('编号不能作为删除确认')

    with pytest.raises(AppError) as error:
        service.dispatch('session.delete', {
            'session_id': session['id'],
            'revision': session['revision'],
            'confirmation': session['id'],
        })

    assert error.value.code == 'DELETE_CONFIRMATION'
    assert service.store.get('session', session['id'])['name'] == '编号不能作为删除确认'


def test_natural_language_catalog_query_is_local_and_does_not_need_model(prepared):
    service, session, _, _ = prepared
    response = service.submit_user_turn(session['id'], session['revision'], 'natural-catalog', '有哪些数据表和字段？', 'AUTO')
    run_once(service)
    turn = service.store.get('turn', response['turnId'])
    assert turn['state'] == 'COMPLETED' and turn['resolvedAction'] == 'DATASET_INSPECT'
    assert '手术事件' in service.store.list('message')[0]['text']


def test_natural_language_result_query_uses_latest_run_and_local_exact_sort(prepared):
    service, session, _, parameters = prepared
    submit(service, session, parameters)
    run_once(service)
    session = service.store.get('session', session['id'])
    source = service.store.list('run')[0]

    class QueryIntent:
        def call(self, stage, payload, schema, budget_id):
            assert stage == 'intent' and source['results'][0]['metricId'] in [m['id'] for m in payload['resultCatalog']]
            return {'action': 'RESULT_QUERY', 'parameters': {'sortBy': 'value', 'direction': 'desc', 'limit': 1}}

    response = service.submit_user_turn(session['id'], session['revision'], 'natural-query', '把结果按数值从高到低显示第一组', 'AUTO')
    run_once(service, QueryIntent())
    turn = service.store.get('turn', response['turnId'])
    query = service.store.list('result_query')[0]
    history = service.store.get('history_context', service.store.list('message')[0]['historyContextId'])
    assert turn['resolvedAction'] == 'RESULT_QUERY' and query['rows'][0]['displayValue'] == '2'
    assert history['automaticReference'] is True and history['sourceRunId'] == source['id']


def test_natural_language_summary_and_material_question_are_source_bound(prepared):
    service, session, _, parameters = prepared
    submit(service, session, parameters)
    run_once(service)
    session = service.store.get('session', session['id'])
    source = service.store.list('run')[0]
    definition = service.store.get('definition', session['definitionId'])
    locator = definition['nodes'][0]['locator']

    class ConversationGateway:
        def call(self, stage, payload, schema, budget_id):
            if stage == 'intent':
                return {'action': 'SUMMARY', 'parameters': {}}
            assert stage == 'explain'
            return {'explanation': '结果已按批准口径完成，具体数值由应用附加。', 'metricIds': [source['results'][0]['metricId']]}

    response = service.submit_user_turn(session['id'], session['revision'], 'natural-summary', '请总结本次分析', 'AUTO')
    run_once(service, ConversationGateway())
    message = service.store.list('message')[0]
    assert service.store.get('turn', response['turnId'])['resolvedAction'] == 'SUMMARY'
    assert message['sourceRunId'] == source['id'] and '结果已按批准口径' in message['text']

    session = service.store.get('session', session['id'])
    class MaterialGateway:
        def call(self, stage, payload, schema, budget_id):
            if stage == 'intent':
                return {'action': 'MATERIAL_QUERY', 'parameters': {}}
            assert stage == 'material'
            return {'answer': '指标说明包含延迟规则。', 'references': [locator]}

    response = service.submit_user_turn(session['id'], session['revision'], 'material-question', '指标说明中如何定义延迟？', 'AUTO')
    run_once(service, MaterialGateway())
    assert service.store.get('turn', response['turnId'])['resolvedAction'] == 'MATERIAL_QUERY'


def test_material_answer_rejects_reference_outside_current_session(prepared):
    service, session, _, _ = prepared

    class MaterialGateway:
        def call(self, stage, payload, schema, budget_id):
            if stage == 'intent':
                return {'action': 'MATERIAL_QUERY', 'parameters': {}}
            return {'answer': '不可信回答', 'references': ['other-session-file.xlsx']}

    response = service.submit_user_turn(session['id'], session['revision'], 'bad-material-ref', '请解释指标说明里的延迟规则', 'AUTO')
    run_once(service, MaterialGateway())
    assert service.store.get('turn', response['turnId'])['state'] == 'REJECTED'
    assert service.store.jobs()[0]['error']['code'] == 'MATERIAL_REFERENCE_INVALID'


@pytest.mark.parametrize('explicit', [False, True])
def test_model_cannot_guess_dates_or_override_explicit_parameters(prepared, explicit):
    service, session, _, parameters = prepared
    class Proposal:
        def call(self, *args):
            return {'action': 'ANALYZE', 'parameters': {'date_start': '2000-01-01', 'date_end': '2000-02-01'}}
    response = service.submit_user_turn(session['id'], session['revision'], 'relative', '分析本月', parameters=parameters if explicit else {})
    run_once(service, Proposal())
    if explicit:
        run = service.store.list('run')[0]
        assert run['committed'] and run['contract']['parameters'] == parameters
        context = service.run_context(session['id'], run['id'])
        assert context['intent']['method'] == 'MODEL_PROPOSAL' and context['mapping_revision']['mappingHash']
    else:
        assert service.store.get('turn', response['turnId'])['state'] == 'REJECTED'
        assert service.store.jobs()[0]['error']['code'] == 'AMBIGUOUS_TIME_WINDOW'
        assert service.store.get('session', session['id'])['state'] == 'DRAFT'


def test_unknown_attachment_must_be_resolved_before_first_lock(prepared, tmp_path):
    from docx import Document
    service, session, _, parameters = prepared
    document = Document()
    document.add_paragraph('仅供参考的普通说明。')
    path = tmp_path / '未知文档.docx'
    document.save(path)
    service.import_paths([str(path)], session['id'])
    run_once(service)
    session = service.store.get('session', session['id'])
    submit(service, session, parameters)
    run_once(service)
    assert service.store.jobs()[0]['error']['code'] == 'ROUTE_UNRESOLVED'
    assert service.store.get('session', session['id'])['state'] == 'DRAFT'
