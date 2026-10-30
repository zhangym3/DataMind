"""An uploaded legacy Word draft must not be mistaken for a missing file."""
import pytest
from docx import Document

from insight_agent.core import AppError
from insight_agent.service import AppService
from insight_agent.worker import run_once


@pytest.fixture
def legacy_upload(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session()
    document = Document()
    document.add_paragraph('统计口径：以手术事件计数。')
    document.add_paragraph('分母：全部手术台次。')
    path = tmp_path / '指标说明.docx'
    document.save(path)
    service.import_paths([str(path)], session['id'])
    assert run_once(service)

    class FailingGateway:
        def call(self, *args):
            raise AppError('OUTBOUND_POLICY', '模型请求包含未批准的嵌套字段')

    assert run_once(service, FailingGateway())
    definition = service.store.list('definition')[0]
    with service.store.transaction() as db:
        definition.pop('sessionId')
        definition = service.store.put('definition', definition, db)
        session = service.store.get('session', session['id'], db)
        session['definitionId'] = None
        session = service.store.put('session', session, db)
    return service, session, definition


def test_legacy_upload_recovers_original_draft_and_error_idempotently(legacy_upload):
    service, session, original = legacy_upload
    jobs = service.store.jobs()
    detail = service.session_detail(session['id'])
    recovered = service.session_metrics(session['id'])
    assert detail['metricDefinition']['id'] == original['id']
    assert detail['session'] == service.store.get('session', session['id'])
    assert detail['session']['definitionId'] == original['id']
    assert detail['session']['attachmentIds'] == session['attachmentIds']
    assert recovered['sessionId'] == session['id']
    assert recovered['status'] == 'DRAFT'
    assert recovered['nodes'] == original['nodes']
    assert recovered['candidates'] == original['candidates']
    assert service.store.jobs() == jobs  # Reading never calls or queues a model.
    assert len(service.store.list('definition')) == 1
    with pytest.raises(AppError) as error:
        service.require_analysis_definition(detail['session'])
    assert error.value.details['nextAction'] == 'RETRY_EXTRACTION'
    assert error.value.details['extractionErrorCode'] == 'OUTBOUND_POLICY'
    assert error.value.details['definitionId'] == original['id']
    assert service.store.get('session', session['id']) == detail['session']
    events = service.session_activities(session['id'])['events']
    assert sum(item['type'] == 'session.metrics.recovered' for item in events) == 1


def test_recovered_upload_can_retry_without_reuploading(legacy_upload):
    service, session, original = legacy_upload
    first = service.dispatch('session.metrics.retry', {'session_id': session['id']})
    second = service.dispatch('session.metrics.retry', {'session_id': session['id']})
    assert first == second
    job = next(j for j in service.store.jobs() if j['id'] == first['jobId'])
    assert job['status'] == 'QUEUED' and job['scope'] == original['id']
    assert job['payload'] == {'definitionId': original['id']}
    assert len(service.store.list('attachment')) == 1


@pytest.mark.parametrize('invalid_scope', ['library', 'another-session'])
def test_recovery_never_adopts_another_scopes_document(legacy_upload, invalid_scope):
    service, session, original = legacy_upload
    with service.store.transaction() as db:
        attachment = service.store.get('attachment', original['attachmentId'], db)
        service.store.put('attachment', attachment | {'scope': invalid_scope}, db)
    assert service.session_metrics(session['id']) is None
    assert service.store.get('session', session['id']) == session
    assert service.store.get('definition', original['id']) == original


@pytest.mark.parametrize('condition', ['locked', 'published', 'other-owner', 'ambiguous'])
def test_recovery_preserves_frozen_or_ambiguous_records(legacy_upload, condition):
    service, session, original = legacy_upload
    with service.store.transaction() as db:
        if condition == 'locked':
            service.store.put('session', session | {'state': 'LOCKED'}, db)
        elif condition == 'published':
            service.store.put('definition', original | {'status': 'PUBLISHED'}, db)
        elif condition == 'other-owner':
            service.store.put('definition', original | {'sessionId': 'another-session'}, db)
        else:
            duplicate = dict(original)
            duplicate.pop('id')
            service.store.put('definition', duplicate, db)
    before_session = service.store.get('session', session['id'])
    before_definitions = service.store.list('definition')
    assert service.session_metrics(session['id']) is None
    assert service.store.get('session', session['id']) == before_session
    assert service.store.list('definition') == before_definitions
