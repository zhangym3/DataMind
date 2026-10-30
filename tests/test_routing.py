"""Role/capability regressions with synthetic, deliberately misleading files."""
import pytest
from docx import Document
from openpyxl import Workbook

from insight_agent.core import AppError
from insight_agent.definitions import create_draft
from insight_agent.files import import_file
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_integration import prepared, submit  # noqa: F401


def rejected(code, fn):
    with pytest.raises(AppError) as caught:
        fn()
    assert caught.value.code == code


@pytest.mark.parametrize('paragraphs,role,status', [
    (['手术质量月度分析报告', '本月完成指标100台，环比增加5%。'], 'OTHER', 'EXCLUDED'),
    (['统计指标情况', '指标运行正常，报告仅作历史展示。'], 'UNKNOWN', 'NEEDS_CONFIRMATION'),
    (['统计口径：以手术事件计数。', '分母：窗口内全部手术台次。'], 'METRIC_DEFINITION', 'READY'),
    (['月度运营报告', '本期结果：100。同比增加。', '统计口径见附录。'], 'UNKNOWN', 'NEEDS_CONFIRMATION'),
    (['使用说明', '点击打开文件，再选择目录。'], 'UNKNOWN', 'NEEDS_CONFIRMATION'),
])
def test_document_role_uses_content_not_filename(tmp_path, paragraphs, role, status):
    service = AppService(tmp_path / 'ws')
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    path = tmp_path / '明确指标定义.docx'
    document.save(path)
    attachment = import_file(service.store, path, 'library')
    assert attachment['route']['role'] == role
    assert attachment['status'] == status
    nodes = {n['locator']: n['quoteSha256'] for n in service.preview(attachment['id'])['nodes']}
    assert all(nodes[e['locator']] == e['quoteSha256'] for e in attachment['route']['evidence'])
    if status != 'READY':
        rejected('ROUTE_UNRESOLVED', lambda: create_draft(service.store, attachment['id'], '禁止误接入'))


@pytest.mark.parametrize('with_rules', [False, True])
def test_word_records_are_not_executable_or_auto_definitions(tmp_path, with_rules):
    service = AppService(tmp_path / 'ws')
    document = Document()
    if with_rules:
        document.add_paragraph('统计口径：以手术事件计数。')
    table = document.add_table(rows=0, cols=2)
    for values in [('手术编号', '手术日期'), ('synthetic-a', '2026-10-01'), ('synthetic-b', '2026-10-02')]:
        for cell, value in zip(table.add_row().cells, values):
            cell.text = value
    path = tmp_path / '指标说明.docx'
    document.save(path)
    attachment = import_file(service.store, path, 'library')
    assert attachment['route']['role'] == ('MIXED' if with_rules else 'DATA_FILE')
    assert attachment['status'] != 'READY'
    code = 'MIXED_UNSUPPORTED' if with_rules else 'FORMAT_UNSUPPORTED'
    rejected(code, lambda: service.route(attachment['id'], 'METRIC_DEFINITION'))
    service.route(attachment['id'], 'OTHER')
    rejected(code, lambda: service.route(attachment['id'], 'METRIC_DEFINITION'))
    assert not service.store.list('definition')


@pytest.mark.parametrize('kind,role,status', [
    ('rules', 'METRIC_DEFINITION', 'UNSUPPORTED_ROLE_FORMAT'),
    ('mixed', 'MIXED', 'NEEDS_CONFIRMATION'),
    ('report', 'OTHER', 'EXCLUDED'),
    ('data', 'DATA_FILE', 'READY'),
])
def test_excel_roles_and_unsupported_formats_cannot_be_bypassed(tmp_path, kind, role, status):
    service = AppService(tmp_path / 'ws')
    wb = Workbook()
    sheet = wb.active
    for row in ([['指标', '计算公式'], ['台次', 'COUNT(*)']] if kind in ('rules', 'mixed') else
                [['指标', '本期值', '同比'], ['台次', '100', '10%']] if kind == 'report' else
                [['surgery_id', 'surgery_date'], ['synthetic-a', '2026-10-01']]):
        sheet.append(row)
    if kind == 'mixed':
        sheet = wb.create_sheet('明细')
        sheet.append(['surgery_id', 'surgery_date'])
        sheet.append(['synthetic-a', '2026-10-01'])
    path = tmp_path / '报告.xlsx'
    wb.save(path)
    attachment = import_file(service.store, path, 'library')
    assert attachment['route']['role'] == role and attachment['status'] == status
    if kind in ('rules', 'mixed'):
        code = 'MIXED_UNSUPPORTED' if kind == 'mixed' else 'FORMAT_UNSUPPORTED'
        rejected(code, lambda: service.route(attachment['id'], 'DATA_FILE'))
        service.route(attachment['id'], 'OTHER')
        rejected(code, lambda: service.route(attachment['id'], 'DATA_FILE'))


def test_route_changes_are_recorded_and_blocked_during_queued_analysis(prepared):
    service, session, _, parameters = prepared
    attachment_id = session['attachmentIds'][0]
    submit(service, session, parameters)
    rejected('SESSION_BUSY', lambda: service.route(attachment_id, 'OTHER'))
    run_once(service)
    rejected('SESSION_LOCKED', lambda: service.route(attachment_id, 'OTHER'))


def test_manual_route_changes_revision_and_preserves_detection(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()
    document = Document()
    document.add_paragraph('经维护人确认的待补充说明。')
    path = tmp_path / '待核对.docx'
    document.save(path)
    service.import_paths([str(path)], session['id'])
    run_once(service)
    session = service.store.get('session', session['id'])
    attachment = service.route(session['attachmentIds'][0], 'METRIC_DEFINITION')
    assert attachment['detectedRoute']['role'] == 'UNKNOWN'
    assert attachment['routeHistory'][0]['route']['role'] == 'UNKNOWN'
    assert service.store.get('session', session['id'])['revision'] > session['revision']
    assert len(service.store.list('definition')) == 1


def test_import_queue_returns_per_file_type_detection(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()

    workbook = Workbook()
    workbook.active.append(['surgery_id', 'surgery_date', 'status'])
    workbook.active.append(['synthetic-a', '2026-10-01', '完成'])
    data_path = tmp_path / '任意文件名.xlsx'
    workbook.save(data_path)

    document = Document()
    document.add_paragraph('统计口径：以手术事件计数。')
    document.add_paragraph('分母：窗口内全部手术台次。')
    definition_path = tmp_path / '也可以是其他名字.docx'
    document.save(definition_path)

    job_id = service.import_paths([str(data_path), str(definition_path)], session['id'])['jobId']
    assert run_once(service)
    job = next(item for item in service.store.jobs() if item['id'] == job_id)
    assert job['status'] == 'SUCCEEDED'
    assert len(job['result']['acceptedDetails']) == 2
    by_name = {item['name']: item for item in job['result']['acceptedDetails']}
    assert by_name[data_path.name]['role'] == 'DATA_FILE'
    assert by_name[data_path.name]['confidence'] == 'HIGH'
    assert by_name[definition_path.name]['role'] == 'METRIC_DEFINITION'
    assert by_name[definition_path.name]['evidenceCount'] >= 1


def test_import_progress_events_are_file_scoped_without_local_paths(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()
    path = tmp_path / '待识别.xlsx'
    workbook = Workbook()
    workbook.active.append(['surgery_id', 'surgery_date', 'status'])
    workbook.active.append(['synthetic-a', '2026-10-01', '完成'])
    workbook.save(path)

    job_id = service.import_paths([str(path)], session['id'])['jobId']
    before = service.session_activities(session['id'])
    started = next(item for item in before['events'] if item['type'] == 'import.started')
    assert started['data']['jobId'] == job_id
    assert started['data']['files'] == [{'index': 1, 'name': path.name, 'size': path.stat().st_size}]
    assert str(tmp_path) not in str(started)

    assert run_once(service)
    progress = [item for item in service.session_activities(session['id'])['events'] if item['type'] == 'import.progress']
    assert progress
    assert {item['data']['jobId'] for item in progress} == {job_id}
    assert progress[-1]['data']['phase'] == 'COMPLETED'


def test_metric_import_auto_creates_versions_and_extract_jobs(tmp_path):
    service = AppService(tmp_path / 'ws')

    paths = []
    for folder, text in (('first', '统计口径：以手术事件计数。'), ('second', '统计口径：以手术事件计数，排除取消事件。')):
        directory = tmp_path / folder
        directory.mkdir()
        document = Document()
        document.add_paragraph(text)
        document.add_paragraph('分母：窗口内全部手术台次。')
        path = directory / '指标说明.docx'
        document.save(path)
        paths.append(path)

    first_job = service.import_paths([str(paths[0])])['jobId']
    assert run_once(service)
    first = next(d for d in service.store.list('definition') if d['attachmentId'])
    assert first['versionLabel'] == 'v1'
    assert first['status'] == 'DRAFT'
    assert any(j['kind'] == 'extract' and j['scope'] == first['id'] and j['status'] == 'QUEUED' for j in service.store.jobs())

    second_job = service.import_paths([str(paths[1])])['jobId']
    assert run_once(service)
    second = next(d for d in service.store.list('definition') if d['id'] != first['id'])
    assert second['familyId'] == first['familyId']
    assert second['versionLabel'] == 'v2'
    assert next(j for j in service.store.jobs() if j['id'] == first_job)['status'] == 'SUCCEEDED'
    assert next(j for j in service.store.jobs() if j['id'] == second_job)['status'] == 'SUCCEEDED'


def test_metric_import_replay_is_idempotent(tmp_path):
    service = AppService(tmp_path / 'ws')
    document = Document()
    document.add_paragraph('统计口径：以手术事件计数。')
    document.add_paragraph('分母：窗口内全部手术台次。')
    path = tmp_path / '指标说明.docx'
    document.save(path)

    service.import_paths([str(path)])
    assert run_once(service)
    service.import_paths([str(path)])
    assert run_once(service)
    assert len(service.store.list('definition')) == 1
    assert len([j for j in service.store.jobs() if j['kind'] == 'extract']) == 1


def test_session_accepts_many_data_files_but_only_one_metric_definition(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()

    data_paths = []
    for number in range(3):
        workbook = Workbook()
        workbook.active.append(['surgery_id', 'surgery_date', 'status'])
        workbook.active.append([f'synthetic-{number}', '2026-10-01', '完成'])
        path = tmp_path / f'数据-{number}.xlsx'
        workbook.save(path)
        data_paths.append(str(path))

    definition_paths = []
    for number in range(2):
        document = Document()
        document.add_paragraph(f'统计口径：以手术事件计数。版本 {number}')
        document.add_paragraph('分母：窗口内全部手术台次。')
        path = tmp_path / f'指标-{number}.docx'
        document.save(path)
        definition_paths.append(str(path))

    job_id = service.import_paths(data_paths + definition_paths, session['id'])['jobId']
    assert run_once(service)
    job = next(item for item in service.store.jobs() if item['id'] == job_id)
    assert job['status'] == 'SUCCEEDED'
    assert len(job['result']['accepted']) == 4
    assert len(job['result']['failures']) == 1
    assert job['result']['failures'][0]['code'] == 'METRIC_DEFINITION_LIMIT'

    current = service.store.get('session', session['id'])
    assert len(current['attachmentIds']) == 4
    attachments = [service.store.get('attachment', item) for item in current['attachmentIds']]
    assert sum(item['route']['role'] == 'DATA_FILE' for item in attachments) == 3
    assert sum(item['route']['role'] == 'METRIC_DEFINITION' for item in attachments) == 1
    assert len(service.store.list('definition')) == 1


def test_manual_metric_confirmation_respects_session_limit(tmp_path):
    service = AppService(tmp_path / 'ws')
    session = service.create_session()

    first = Document()
    first.add_paragraph('统计口径：以手术事件计数。')
    first.add_paragraph('分母：窗口内全部手术台次。')
    first_path = tmp_path / '已识别指标.docx'
    first.save(first_path)

    second = Document()
    second.add_paragraph('请由维护人确认本文件的用途。')
    second_path = tmp_path / '待确认.docx'
    second.save(second_path)

    service.import_paths([str(first_path)], session['id'])
    assert run_once(service)
    service.import_paths([str(second_path)], session['id'])
    assert run_once(service)
    current = service.store.get('session', session['id'])
    second_attachment = next(
        service.store.get('attachment', item)
        for item in current['attachmentIds']
        if service.store.get('attachment', item)['name'] == second_path.name
    )
    rejected('METRIC_DEFINITION_LIMIT', lambda: service.route(second_attachment['id'], 'METRIC_DEFINITION'))
