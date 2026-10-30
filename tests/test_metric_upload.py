"""Upload routing, stale failure recovery and bounded detail timeout recovery."""
import copy

import pytest
from docx import Document
from openpyxl import Workbook

from insight_agent.core import AppError
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_metric_catalog import CatalogGateway, Unavailable


@pytest.fixture
def word_upload(tmp_path):
    service = AppService(tmp_path / 'workspace')
    session = service.create_session()
    doc = Document()
    doc.add_paragraph('统计口径：按手术事件统计。')
    for index in range(7):
        doc.add_paragraph(f'指标{index + 1}：入室至麻醉超过20分钟的台数；排除条件：移植手术。')
    word = tmp_path / '指标说明.docx'
    doc.save(word)
    return service, session['id'], word


def test_word_only_upload_automatically_parses_without_rule_generation(word_upload):
    service, sid, word = word_upload
    service.import_paths([str(word)], sid)
    assert run_once(service)
    jobs = service.store.jobs()
    assert [j['kind'] for j in jobs if j['status'] == 'QUEUED'] == ['metric_catalog']
    result = next(j for j in jobs if j['kind'] == 'import')['result']['acceptedDetails'][0]
    assert result['catalogJobId'] and result['extractJobId'] is None
    assert run_once(service, CatalogGateway())
    assert service.session_metric_catalog(sid)['status'] == 'READY'
    assert not any(j['kind'] == 'extract' for j in service.store.jobs())
    assert service.session_metrics(sid)['status'] == 'DRAFT'


def test_reupload_restarts_failed_catalog_and_reuses_ready_result(word_upload):
    service, sid, word = word_upload
    service.import_paths([str(word)], sid)
    assert run_once(service)
    assert run_once(service, Unavailable())
    definition_id = service.session_metrics(sid)['id']
    service.import_paths([str(word)], sid)
    assert run_once(service)
    catalogs = [j for j in service.store.jobs() if j['kind'] == 'metric_catalog']
    assert len(catalogs) == 2 and sum(j['status'] == 'QUEUED' for j in catalogs) == 1
    assert service.session_metrics(sid)['id'] == definition_id
    assert run_once(service, CatalogGateway())
    service.import_paths([str(word)], sid)
    assert run_once(service)
    assert len([j for j in service.store.jobs() if j['kind'] == 'metric_catalog']) == 2
    assert len(service.store.list('definition')) == 1


def test_reupload_reuses_active_catalog(word_upload):
    service, sid, word = word_upload
    service.import_paths([str(word)], sid)
    assert run_once(service)
    held = service.store.claim()
    assert held['kind'] == 'metric_catalog'
    service.import_paths([str(word)], sid)
    assert run_once(service)
    assert len([j for j in service.store.jobs() if j['kind'] == 'metric_catalog']) == 1


@pytest.mark.parametrize('together,reverse', [(True, False), (True, True), (False, False)])
def test_excel_prepares_rules_regardless_of_import_order(word_upload, tmp_path, together, reverse):
    service, sid, word = word_upload
    book = Workbook()
    book.active.append(['手术唯一编号', '手术日期', '入室时间', '麻醉开始时间'])
    book.active.append(['synthetic-1', '2026-10-01', '2026-10-01T08:00', '2026-10-01T08:25'])
    excel = tmp_path / '数据.xlsx'
    book.save(excel)
    if together:
        paths = [str(word), str(excel)]
        service.import_paths(list(reversed(paths)) if reverse else paths, sid)
    else:
        service.import_paths([str(word)], sid)
        assert run_once(service)
        assert run_once(service, CatalogGateway())
        service.import_paths([str(excel)], sid)
    assert run_once(service)
    queued = [j for j in service.store.jobs() if j['status'] == 'QUEUED']
    assert len(queued) == 1 and queued[0]['kind'] == 'extract'
    assert service.ensure_definition_extract(service.session_metrics(sid)['id']) == queued[0]['id']


@pytest.mark.parametrize('code', ['MODEL_TIMEOUT', 'MODEL_OUTPUT_TRUNCATED'])
def test_detail_batches_shrink_and_persist_before_a_single_item_failure(word_upload, code):
    service, sid, word = word_upload
    service.import_paths([str(word)], sid)
    assert run_once(service)

    class Shrinking(CatalogGateway):
        def call(self, stage, payload, schema, budget_id):
            if payload['items'] and (len(payload['items']) > 1 or payload['items'][0]['id'] == 'metric_003'):
                self.calls.append(copy.deepcopy(payload))
                raise AppError(code, '合成响应失败')
            return super().call(stage, payload, schema, budget_id)

    gateway = Shrinking()
    assert run_once(service, gateway)
    record = service.session_metric_catalog(sid)
    assert record['status'] == 'PARTIAL'
    assert [i['id'] for i in record['items'] if i['status'] != 'PENDING'] == ['metric_001', 'metric_002']
    assert [len(c['items']) for c in gateway.calls] == [0, 4, 2, 1, 1, 2, 1]
    service.retry_session_metrics(sid)
    resumed = CatalogGateway()
    assert run_once(service, resumed)
    assert [len(c['items']) for c in resumed.calls] == [4, 1]
    assert not {i['id'] for c in resumed.calls for i in c['items']} & {'metric_001', 'metric_002'}
    assert service.session_metric_catalog(sid)['status'] == 'READY'


@pytest.mark.parametrize('code', ['MODEL_TIMEOUT', 'MODEL_OUTPUT_TRUNCATED'])
def test_directory_batches_shrink_save_and_resume_without_repeating_completed_nodes(word_upload, code):
    service, sid, word = word_upload
    service.import_paths([str(word)], sid)
    assert run_once(service)

    class DirectoryGateway(CatalogGateway):
        def __init__(self, fail):
            super().__init__()
            self.fail = fail
            self.targets = []

        def call(self, stage, payload, schema, budget_id):
            if payload['items']:
                return super().call(stage, payload, schema, budget_id)
            refs = payload['targetLocators']
            self.targets.append(refs)
            if self.fail and (len(refs) > 2 or payload['nodes'][4]['locator'] in refs):
                raise AppError(code, '合成目录失败')
            result = super().call(stage, payload, schema, budget_id)
            result['items'] = [i for i in result['items'] if set(i['locators']) & set(refs)]
            result['contextLocators'] = [r for r in result['contextLocators'] if r in refs]
            return result

    failed = DirectoryGateway(fail=True)
    assert run_once(service, failed)
    partial = service.session_metric_catalog(sid)
    assert partial['status'] == 'PARTIAL'
    assert len(partial['directoryLocators']) == 4 and len(partial['items']) == 3
    saved = set(partial['directoryLocators'])
    service.retry_session_metrics(sid)
    resumed = DirectoryGateway(fail=False)
    assert run_once(service, resumed)
    assert len(resumed.targets) == 1 and not set(resumed.targets[0]) & saved
    ready = service.session_metric_catalog(sid)
    assert ready['status'] == 'READY' and len(ready['items']) == 7
    assert len(ready['directoryLocators']) == 8
    assert all(c['disposition'] != 'PENDING' for c in ready['coverage'])
