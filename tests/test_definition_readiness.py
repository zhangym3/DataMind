"""Missing analysis prerequisites must point to a recoverable document step."""
import pytest
from docx import Document

from insight_agent.core import AppError
from insight_agent.service import AppService
from insight_agent.worker import run_once


@pytest.fixture
def uploaded(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session()
    document = Document()
    document.add_paragraph('统计口径：以手术事件计数。')
    document.add_paragraph('分母：全部手术台次。')
    path = tmp_path / '指标说明.docx'
    document.save(path)
    # Reproduce the existing legacy state: a linked document with an unbound
    # draft, regardless of how new uploads are attached in the current UI.
    service.import_paths([str(path)])
    assert run_once(service)
    definition = service.store.list('definition')[0]
    with service.store.transaction() as db:
        session['attachmentIds'] = [definition['attachmentId']]
        session['definitionId'] = definition['id']
        session = service.store.put('session', session, db)
    return service, session, definition


def rejected_analysis(service, session, expected_action, definition_id=None):
    request = service.submit_user_turn(session['id'], session['revision'], 'analyze', '', 'ANALYZE', {'scope': 'ALL_DATA'})
    assert run_once(service)
    job = next(job for job in service.store.jobs() if job['id'] == request['jobId'])
    assert job['status'] == 'FAILED', job
    assert job['error']['code'] == 'DEFINITION_UNAVAILABLE', job
    assert job['error']['details']['nextAction'] == expected_action
    if definition_id:
        assert job['error']['details']['definitionId'] == definition_id
    assert service.store.get('turn', request['turnId'])['state'] == 'REJECTED'
    assert service.store.get('session', session['id'])['state'] == 'DRAFT'
    assert not service.store.list('run')
    return job


def test_analysis_without_definition_explains_upload_step(tmp_path):
    service = AppService(tmp_path / 'workspace')
    rejected_analysis(service, service.create_session(), 'UPLOAD_DEFINITION')


def test_failed_extraction_has_linked_error_and_can_be_requeued(uploaded):
    service, session, definition = uploaded

    class FailingGateway:
        def call(self, *args):
            raise AppError('OUTBOUND_POLICY', '模型请求包含未批准的嵌套字段')

    assert run_once(service, FailingGateway())
    job = rejected_analysis(service, session, 'RETRY_EXTRACTION', definition['id'])
    assert job['error']['details']['extractionErrorCode'] == 'OUTBOUND_POLICY'
    assert '未批准的嵌套字段' in job['error']['message']
    activities = service.session_activities(session['id'])
    extract = next(item for item in activities['jobs'] if item['kind'] == 'extract')
    assert extract['definitionId'] == definition['id'] and extract['error']['code'] == 'OUTBOUND_POLICY'
    assert all('payload' not in item for item in activities['jobs'])
    assert str(service.store.home) not in str(activities)
    retry = service.ensure_definition_extract(definition['id'])
    assert retry == service.ensure_definition_extract(definition['id'])
    assert service.store.get('definition', definition['id'])['status'] == 'DRAFT'


def test_pending_extraction_explains_wait_step(uploaded):
    service, session, definition = uploaded
    # Hold the extraction lease while a submitted analysis is evaluated.
    extracting = service.store.claim()
    assert extracting['kind'] == 'extract'
    rejected_analysis(service, session, 'WAIT_EXTRACTION', definition['id'])


