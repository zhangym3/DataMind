"""A single analysis request waits for its own validated document preparation."""
import pytest

from insight_agent.core import AppError
from insight_agent.store import DeferredJob
from insight_agent.worker import process_job, run_once
from test_metric_catalog import CatalogGateway
from test_report_plan import FillGateway, uploaded_fill as _uploaded_fill


@pytest.fixture
def uploaded_fill(tmp_path):
    return _uploaded_fill.__wrapped__(tmp_path)


class TimeoutGateway:
    def call(self, *args):
        raise AppError('MODEL_TIMEOUT', '旧的合成超时。')


def job_by_id(service, job_id):
    return next(j for j in service.store.jobs() if j['id'] == job_id)


def submit(service, sid, request_id='prepare-once', action='ANALYZE'):
    session = service.store.get('session', sid)
    return service.submit_user_turn(sid, session['revision'], request_id, '', action, {'scope': 'ALL_DATA'})


def waiting_on_active(service, sid):
    extracting = service.store.claim()
    assert extracting['kind'] == 'extract'
    submitted = submit(service, sid)
    assert not run_once(service)
    pending = job_by_id(service, submitted['jobId'])
    assert pending['status'] == 'QUEUED' and pending['payload']['waitForJobId'] == extracting['id']
    assert service.store.get('turn', submitted['turnId'])['state'] == 'PENDING'
    assert service.store.claim() is None  # No busy loop or blocking the worker.
    return submitted, extracting


@pytest.mark.parametrize('action,text', [('ANALYZE', ''), ('AUTO', '请统计本次材料的手术量')])
def test_analysis_submitted_behind_queued_extraction_uses_its_binding_revision(uploaded_fill, action, text):
    service, sid = uploaded_fill
    session = service.store.get('session', sid)
    submitted = service.submit_user_turn(sid, session['revision'], 'queued-first', text, action, {'scope': 'ALL_DATA'})
    assert job_by_id(service, submitted['jobId'])['payload']['waitForJobId']
    assert run_once(service, FillGateway())

    class IntentGateway:
        def call(self, stage, *args):
            assert stage == 'intent'
            return {'action': 'ANALYZE', 'parameters': {}}

    assert run_once(service, IntentGateway())
    completed = job_by_id(service, submitted['jobId'])
    assert completed['status'] == 'SUCCEEDED', completed
    assert len(service.store.list('run')) == 1


@pytest.mark.parametrize('action', ['ANALYZE', 'PLAN'])
def test_one_click_after_old_timeout_and_ready_catalog_prepares_then_continues(uploaded_fill, action):
    service, sid = uploaded_fill
    assert run_once(service, TimeoutGateway())
    service.parse_session_metrics(sid)
    assert run_once(service, CatalogGateway())
    assert service.session_metric_catalog(sid)['status'] == 'READY'
    assert service.session_metrics(sid)['status'] == 'DRAFT'
    submitted = submit(service, sid, action=action)
    # The same request is still idempotent while preparation is pending.
    assert submit(service, sid, action=action) == submitted
    assert run_once(service)
    assert job_by_id(service, submitted['jobId'])['status'] == 'QUEUED'
    turn = service.store.get('turn', submitted['turnId'])
    original_revision = turn['sessionRevision']
    gateway = FillGateway()
    assert run_once(service, gateway)
    assert service.session_metrics(sid)['status'] == 'PUBLISHED'
    assert service.store.get('turn', submitted['turnId'])['sessionRevision'] == original_revision + 1
    assert run_once(service)
    completed = job_by_id(service, submitted['jobId'])
    assert completed['status'] == 'SUCCEEDED', completed
    run = service.store.get('run', completed['result']['runId'])
    assert run['committed'] and run['mode'] == action
    if action == 'ANALYZE':
        assert [r['groups'][0]['displayValue'] for r in run['results'] if r['outcome'] == 'VALUE'] == ['2', '1', '1']
    assert len(service.store.list('run')) == 1
    assert not run_once(service)
    events = service.session_activities(sid)['events']
    assert [e['type'] for e in events if e['type'].startswith('turn.prepar')] == ['turn.preparing', 'turn.prepared']
    assert len([j for j in service.store.jobs() if j['kind'] == 'extract']) == 2


def test_active_extraction_is_reused_and_wait_survives_restart(uploaded_fill):
    service, sid = uploaded_fill
    submitted, extracting = waiting_on_active(service, sid)
    service.store.recover()
    assert run_once(service, FillGateway())
    assert run_once(service)
    assert job_by_id(service, submitted['jobId'])['status'] == 'SUCCEEDED'
    assert [j['id'] for j in service.store.jobs() if j['kind'] == 'extract'] == [extracting['id']]


@pytest.mark.parametrize('cancel_dependency', [False, True])
def test_current_preparation_failure_is_reported_once_without_retry_loop(uploaded_fill, cancel_dependency):
    service, sid = uploaded_fill
    assert run_once(service, TimeoutGateway())
    submitted = submit(service, sid)
    assert run_once(service)
    turn = service.store.get('turn', submitted['turnId'])
    dependency_id = turn['preparationJobId']
    if cancel_dependency:
        service.cancel(dependency_id)
    else:
        class CurrentFailure:
            def call(self, *args):
                raise AppError('MODEL_UNAVAILABLE', '本次合成服务不可用。')
        assert run_once(service, CurrentFailure())
    assert run_once(service)
    failed = job_by_id(service, submitted['jobId'])
    assert failed['status'] == 'FAILED' and failed['error']['code'] == 'DEFINITION_UNAVAILABLE'
    assert failed['error']['details']['preparationJobId'] == dependency_id
    assert failed['error']['details']['extractionErrorCode'] == ('CANCELLED' if cancel_dependency else 'MODEL_UNAVAILABLE')
    assert '旧的合成超时' not in failed['error']['message']
    assert '。。' not in failed['error']['message']
    assert not run_once(service)
    assert not service.store.list('run')
    assert len([j for j in service.store.jobs() if j['kind'] == 'extract']) == 2


def test_cancelled_waiting_turn_never_creates_a_run(uploaded_fill):
    service, sid = uploaded_fill
    submitted, extracting = waiting_on_active(service, sid)
    service.cancel(submitted['jobId'])
    service.store.finish(extracting, result=process_job(service, extracting, FillGateway()))
    assert not run_once(service)
    assert job_by_id(service, submitted['jobId'])['status'] == 'CANCELLED'
    assert service.store.get('turn', submitted['turnId'])['state'] == 'CANCELLED'
    assert not service.store.list('run')


@pytest.mark.parametrize('change', ['name', 'attachmentIds'])
def test_user_revision_or_document_change_cannot_be_accepted_by_auto_binding(uploaded_fill, change):
    service, sid = uploaded_fill
    submitted, extracting = waiting_on_active(service, sid)
    with service.store.transaction() as db:
        session = service.store.get('session', sid, db)
        session[change] = '外部变更' if change == 'name' else []
        service.store.put('session', session, db)
    service.store.recover()
    assert run_once(service, FillGateway())
    assert run_once(service)
    failed = job_by_id(service, submitted['jobId'])
    assert failed['error']['code'] == 'REVISION_CONFLICT'
    assert service.store.get('turn', submitted['turnId'])['sessionRevision'] < service.store.get('session', sid)['revision']
    assert not service.store.list('run')


def test_crash_after_publication_recovers_without_repeating_model_work(uploaded_fill):
    service, sid = uploaded_fill
    submitted, extracting = waiting_on_active(service, sid)
    result = process_job(service, extracting, FillGateway())
    assert not isinstance(result, DeferredJob)
    # Publication committed, but the extraction job's success was not yet saved.
    assert job_by_id(service, extracting['id'])['status'] == 'RUNNING'
    service.store.recover()
    assert run_once(service, TimeoutGateway())  # A model call here would fail.
    assert job_by_id(service, extracting['id'])['status'] == 'SUCCEEDED'
    assert run_once(service)
    assert job_by_id(service, submitted['jobId'])['status'] == 'SUCCEEDED'
    assert len(service.store.list('run')) == 1


def test_no_data_does_not_start_a_new_model_attempt(uploaded_fill):
    service, sid = uploaded_fill
    assert run_once(service, TimeoutGateway())
    with service.store.transaction() as db:
        session = service.store.get('session', sid, db)
        session['attachmentIds'] = [service.session_metrics(sid)['attachmentId']]
        service.store.put('session', session, db)
    submitted = submit(service, sid)
    assert run_once(service)
    assert job_by_id(service, submitted['jobId'])['error']['code'] == 'DATA_REQUIRED'
    assert len([j for j in service.store.jobs() if j['kind'] == 'extract']) == 1
