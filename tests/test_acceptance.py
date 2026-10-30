"""Acceptance boundaries using synthetic data and independently stated results."""
import copy
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from fractions import Fraction

import httpx
import pytest
from docx import Document
from openpyxl import load_workbook

from test_integration import prepared, submit
from insight_agent.core import AppError, digest, dumps, sha
from insight_agent.data import join_rows
from insight_agent.definitions import amend, create_draft, metadata, publish, save_candidates
from insight_agent.files import parse_docx, parse_xls
from insight_agent.maintenance import backup, cleanup_preview, migrate, verify_references, verify_restore
from insight_agent.models import Gateway, outbound, save_profile, temporary_profile
from insight_agent.reports import export_run, spreadsheet_bytes
from insight_agent.service import AppService
from insight_agent.worker import run_once


class Vault:
    def __init__(self): self.values = {}
    def set(self, ref, secret): self.values[ref] = secret
    def get(self, ref): return self.values.get(ref)
    def delete(self, ref): self.values.pop(ref, None)


def expect(code, fn):
    with pytest.raises(AppError) as error: fn()
    assert error.value.code == code


def complete(prepared):
    service, session, definition, parameters = prepared
    submit(service, session, parameters)
    run_once(service)
    run = service.store.list('run')[0]
    assert run['committed'], service.store.jobs()[0]
    return service, run


def test_diagnostics_contains_timings_not_business_content(prepared):
    from insight_agent.maintenance import diagnostics
    service, run = complete(prepared)
    data = diagnostics(service.store)['data']
    assert data['databaseIntegrity'] == 'ok'
    assert data['runs'][0]['runId'] == run['id']
    assert {s['phase'] for s in data['runs'][0]['stages']} >= {'PREPARING', 'CALCULATING', 'AUDITING', 'COMMITTING'}
    assert all(len(h) == 64 for h in data['requestIdHashes'])
    serialized = dumps(data)
    assert 'request-1' not in serialized and '合成手术' not in serialized
    assert 'entry_at' not in serialized and 'credentialRef' not in serialized


@pytest.mark.parametrize('extent', ['missing', 'underreported'])
def test_xlsx_untrusted_dimensions_scan_and_enforce_limit(monkeypatch, extent):
    from openpyxl import Workbook
    from insight_agent.core import LIMITS
    from insight_agent.files import parse_xlsx
    import re
    wb = Workbook(write_only=True)
    ws = wb.create_sheet('rows')
    for row in [['id'], ['a'], ['b'], ['c']]:
        ws.append(row)
    raw = io.BytesIO(); wb.save(raw)
    edited = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw.getvalue())) as source, zipfile.ZipFile(edited, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == 'xl/worksheets/sheet1.xml':
                data = re.sub(rb'<dimension[^>]*/>', b'', data)
                if extent == 'underreported':
                    data = data.replace(b'<sheetPr>', b'<dimension ref="A1:A2"/><sheetPr>', 1)
            target.writestr(name, data)
    assert parse_xlsx(edited.getvalue())['rowCount'] == 3
    monkeypatch.setitem(LIMITS, 'sheet_rows', 2)
    expect('FILE_LIMIT', lambda: parse_xlsx(edited.getvalue()))


def test_xlsx_skips_report_title_rows_before_header():
    from openpyxl import Workbook
    from insight_agent.files import parse_xlsx

    wb = Workbook()
    ws = wb.active
    ws.append(['医院手术明细查询'])
    ws.append(['日期：2026-10-16~2026-10-22'])
    ws.append(['surgery_id', 'surgery_date', 'status'])
    ws.append(['S-1', '2026-10-16', '完成'])
    raw = io.BytesIO()
    wb.save(raw)

    parsed = parse_xlsx(raw.getvalue())
    sheet = parsed['sheets'][0]
    assert sheet['headerRow'] == 3
    assert sheet['headers'] == ['surgery_id', 'surgery_date', 'status']
    assert sheet['rows'][0]['values']['surgery_id'] == 'S-1'
    assert 'PREAMBLE_ROWS_SKIPPED' in sheet['warnings']


def test_session_capacity_accumulates_across_files(prepared, monkeypatch, tmp_path):
    from openpyxl import Workbook
    from insight_agent.core import LIMITS
    service, session, _, _ = prepared
    monkeypatch.setitem(LIMITS, 'session_rows', 5)
    wb = Workbook(); ws = wb.active
    ws.append(['id']); ws.append(['extra-1']); ws.append(['extra-2'])
    path = tmp_path / 'extra.xlsx'; wb.save(path)
    job_id = service.import_paths([str(path)], session['id'])['jobId']
    run_once(service)
    job = next(j for j in service.store.jobs() if j['id'] == job_id)
    assert job['result']['failures'][0]['code'] == 'FILE_LIMIT'
    assert service.store.get('session', session['id'])['attachmentIds'] == session['attachmentIds']


@pytest.mark.parametrize('protocol', ['chat', 'responses', 'anthropic'])
def test_final_model_request_and_no_secret_persistence(tmp_path, protocol):
    service = AppService(tmp_path / 'ws')
    vault = Vault()
    secret = 'synthetic-test-secret-never-real'
    p = save_profile(service.store, {'name':'test', 'provider':'test', 'model':'fixture', 'baseUrl':'https://example.test/v1', 'protocol':protocol}, secret, vault)
    requests = []
    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert secret not in request.content.decode()
        assert request.headers.get('x-api-key') == secret if protocol == 'anthropic' else request.headers.get('authorization') == 'Bearer '+secret
        if protocol == 'chat': value = {'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
        elif protocol == 'responses':
            assert body['store'] is False
            value = {'status':'completed','output':[{'content':[{'type':'output_text','text':'{"ok":true}'}]}]}
        else: value = {'stop_reason':'end_turn','content':[{'type':'text','text':'{"ok":true}'}]}
        return httpx.Response(200,json=value)
    result = Gateway(service.store,vault,httpx.MockTransport(reply)).test(p['id'])
    assert result['status'] == 'PASS' and len(requests) == 1
    assert all(secret.encode() not in f.read_bytes() for f in service.store.home.rglob('*') if f.is_file())
    assert 'credentialRef' not in dumps(service.bootstrap())


@pytest.mark.parametrize('status,code,count', [(401,'MODEL_AUTH_FAILED',1),(429,'MODEL_RATE_LIMIT',3),(302,'MODEL_PROTOCOL_ERROR',1)])
def test_model_failure_bounded(tmp_path, monkeypatch, status, code, count):
    service=AppService(tmp_path/'ws'); vault=Vault()
    p=save_profile(service.store,{'name':'t','provider':'t','model':'t','baseUrl':'https://example.test/v1','protocol':'chat'},'fake',vault)
    seen=[]
    def reply(req): seen.append(req); return httpx.Response(status,json={})
    monkeypatch.setattr('insight_agent.models.time.sleep',lambda _:None)
    expect(code,lambda:Gateway(service.store,vault,httpx.MockTransport(reply)).test(p['id']))
    assert len(seen)==count
    calls=service.store.list('model_call')
    assert len(calls)==count and all(c['status']=='FAILED' and c['errorCode']==code for c in calls)


def test_model_schema_budget_and_frozen_revision(tmp_path):
    service=AppService(tmp_path/'ws'); vault=Vault()
    p=save_profile(service.store,{'name':'t','provider':'t','model':'t','baseUrl':'https://example.test/v1','protocol':'chat'},'fake',vault)
    transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":false}'}}]}))
    gateway=Gateway(service.store,vault,transport)
    expect('MODEL_SCHEMA_INVALID',lambda:gateway.test(p['id']))
    service.save_settings({'modelTokens':1000})
    expect('BUDGET_EXCEEDED',lambda:gateway.call('connection',{'instruction':'Return JSON. '*1000},{'type':'object'},'large-input-budget'))
    gateway.frozen_profile=copy.deepcopy(service.store.get('model',p['id']))
    save_profile(service.store,{'id':p['id'],'name':'changed','provider':'t','model':'t','baseUrl':'https://example.test/v1','protocol':'chat'},credentials=vault)
    expect('MODEL_CONFIG_CHANGED',lambda:gateway.test(p['id']))


def test_unsaved_model_test_uses_current_profile_and_cleans_temporary_key(tmp_path):
    service = AppService(tmp_path / 'ws')
    vault = Vault()
    reference = 'credential_test_unsaved'
    secret = 'unsaved-secret-only'
    vault.set(reference, secret)
    profile = temporary_profile({'name': 'unsaved', 'provider': 'test', 'model': 'fixture', 'baseUrl': 'https://example.test/v1', 'protocol': 'chat'}, reference)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}))
    job_id = service.queue('model_test', 'settings', {'profile': profile, 'temporaryCredentialRef': reference})['jobId']
    assert secret not in dumps(service.store.jobs()[0]['payload'])
    assert run_once(service, Gateway(service.store, vault, transport))
    job = next(job for job in service.store.jobs() if job['id'] == job_id)
    assert job['status'] == 'SUCCEEDED' and job['result']['status'] == 'PASS'
    assert vault.get(reference) is None
    assert service.store.list('model') == []


def test_unsaved_model_test_cleans_key_after_failure_and_cancel(tmp_path):
    service = AppService(tmp_path / 'ws', credentials=Vault())
    reference = 'credential_test_failure'
    service.credentials.set(reference, 'unsaved-secret-only')
    profile = temporary_profile({'name': 'unsaved', 'provider': 'test', 'model': 'fixture', 'baseUrl': 'https://example.test/v1', 'protocol': 'chat'}, reference)
    job_id = service.queue('model_test', 'settings', {'profile': profile, 'temporaryCredentialRef': reference})['jobId']
    failing = Gateway(service.store, service.credentials, httpx.MockTransport(lambda _: httpx.Response(401, json={})))
    assert run_once(service, failing)
    failed = next(job for job in service.store.jobs() if job['id'] == job_id)
    assert failed['status'] == 'FAILED' and failed['error']['code'] == 'MODEL_AUTH_FAILED'
    assert service.credentials.get(reference) is None

    reference = 'credential_test_cancel'
    service.credentials.set(reference, 'unsaved-secret-only')
    profile = temporary_profile({'name': 'unsaved', 'provider': 'test', 'model': 'fixture', 'baseUrl': 'https://example.test/v1', 'protocol': 'chat'}, reference)
    job_id = service.queue('model_test', 'settings', {'profile': profile, 'temporaryCredentialRef': reference})['jobId']
    assert service.cancel(job_id)['status'] == 'CANCEL_REQUESTED'
    assert service.credentials.get(reference) is None


def test_outbound_closed_nested_and_exact_numbers():
    for payload in [{'question':'ok','metricCatalog':[{'id':'a','name':'n','rows':[]}]},{'question':'ok','path':'D:/private'}]:
        expect('OUTBOUND_POLICY',lambda:outbound('intent',payload))
    expect('OUTBOUND_POLICY',lambda:outbound('review',{'results':[{'metricId':'a','audit':{'patient':'sensitive'}}]}))
    value='123456789012345678'
    assert outbound('review',{'results':[{'metricId':'a','groups':[{'exactNumerator':value,'exactDenominator':'1','displayValue':value}]}]})['results'][0]['groups'][0]['exactNumerator']==value
    clean=outbound('intent',{'question':'姓名：合成测试 电话：13800138000 D:\\private\\file.xlsx','metricCatalog':[]})
    assert '13800138000' not in clean['question'] and '合成测试' not in clean['question'] and 'private' not in clean['question']


@pytest.mark.parametrize('cardinality,left,right,count', [('1:1',[{'id':'a','x':'1'}],[{'id':'a','y':'2'}],1),('1:n',[{'id':'a','x':'1'}],[{'id':'a','y':'2'},{'id':'a','y':'3'}],2),('n:1',[{'id':'a','x':'1'},{'id':'a','x':'2'}],[{'id':'a','y':'3'}],2)])
def test_join_cardinality_and_provenance(cardinality,left,right,count):
    for i,r in enumerate(left+right):r['_source']={'row':i+1}
    result,audit=join_rows(left,right,['id'],cardinality,{'id':'id','y':'y'})
    assert len(result)==count and audit['outputRows']==count
    assert all(r['_source']['left'] and r['_source']['right'] for r in result)


def test_join_rejects_ambiguity_and_reports_coverage():
    expect('DATA_CONFLICT',lambda:join_rows([{'id':'a'},{'id':'a'}],[{'id':'a'}],['id'],'1:1',{'id':'id'}))
    expect('DATA_INSUFFICIENT',lambda:join_rows([{'id':None}],[],['id'],'1:1',{}))
    expect('DATA_CONFLICT',lambda:join_rows([{'id':'a','x':'1'}],[{'id':'a','x':'2'}],['id'],'1:1',{'x':'x'}))
    expect('DATA_INSUFFICIENT',lambda:join_rows([{'id':'b'}],[{'id':'a'}],['id'],'1:1',{}))
    rows,audit=join_rows([{'id':'b'}],[{'id':'a'}],['id'],'1:1',{},False)
    assert rows==[] and audit['unmatchedLeftKeys']==audit['unmatchedRightKeys']==1


def test_xls_decimal_date_formula_and_hidden():
    import datetime
    import xlwt
    wb=xlwt.Workbook(); ws=wb.add_sheet('events'); hidden=wb.add_sheet('hidden'); hidden.visibility=1
    for i,h in enumerate(['amount','date','formula']):ws.write(0,i,h)
    ws.write(1,0,0.1); ws.write(1,1,datetime.datetime(2026,10,1),xlwt.easyxf(num_format_str='YYYY-MM-DD'))
    ws.write(1,2,xlwt.Formula('1+2')); hidden.write(0,0,'id');hidden.write(1,0,'synthetic')
    output=io.BytesIO(); wb.save(output)
    result=parse_xls(output.getvalue()); values=result['sheets'][0]['rows'][0]['values']
    assert values['amount']=='0.1' and values['date'].startswith('2026-10-01')
    assert values['formula']['invalid']=='FORMULA_CACHE_UNTRUSTED' and len(result['sheets'])==2


def docx_package(parts):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:
        for name,body in parts.items():z.writestr(name,'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+body+'</w:body></w:document>')
    return output.getvalue()


def test_docx_all_regions_and_final_view():
    data=docx_package({'word/document.xml':'<w:p><w:r><w:t>正文指标</w:t></w:r><w:txbxContent><w:p><w:r><w:t>文本框规则</w:t></w:r></w:p></w:txbxContent></w:p>','word/footnotes.xml':'<w:p><w:r><w:t>脚注条款</w:t></w:r></w:p>'})
    nodes=parse_docx(data)['nodes']
    assert [n['text'] for n in nodes]==['正文指标','文本框规则','脚注条款']
    assert all(sha(n['text'].encode())==n['quoteSha256'] for n in nodes)
    for body,code in [('<w:ins/>','DOCUMENT_FINAL_VIEW_UNRESOLVED'),('<w:pict/>','DOCUMENT_UNSUPPORTED_REGION')]:
        expect(code,lambda:parse_docx(docx_package({'word/document.xml':body})))


def test_amendment_requires_new_coverage_and_published_immutable(prepared):
    service,_,published,_=prepared
    draft=create_draft(service.store,published['attachmentId'],'revision',published['id'])
    draft=save_candidates(service.store,draft['id'],published['candidates'],published['coverage'],draft['revision'])
    draft=amend(service.store,draft['id'],'补充适用范围','审阅人','合成数据',[],draft['revision'])
    expect('DOCUMENT_COVERAGE_INCOMPLETE',lambda:publish(service.store,draft['id'],draft['revision'],'reviewer',True))
    expect('IMMUTABLE_VERSION',lambda:metadata(service.store,published['id'],published['revision'],'overwrite',''))
    service.default_definition(published['id'])
    assert service.create_session()['definitionId'] is None
    assert service.create_session(definition_id=published['id'])['definitionId']==published['id']


def test_backup_restore_migration_and_recursive_gc(prepared,tmp_path):
    service,run=complete(prepared)
    orphan=service.store.blob(b'unreferenced synthetic blob')
    preview=cleanup_preview(service.store,90)
    assert orphan in {b['sha256'] for b in preview['unreferencedBlobs']}
    verify_references(service.store.home)
    archive=tmp_path/'backup.7z'; password='synthetic-strong-password'
    backup(service.store,archive,password)
    expect('BACKUP_DECRYPT_FAILED',lambda:verify_restore(archive,'incorrect',tmp_path/'wrong'))
    assert not (tmp_path/'wrong').exists()
    verify_restore(archive,password,tmp_path/'restored')
    assert AppService(tmp_path/'restored').store.get('run',run['id'])['resultDigest']==run['resultDigest']
    migrate(service.store,tmp_path/'migrated')
    assert service.store.get('run',run['id'])['committed']
    # Checkpoint-only prepared reference must be validated even if no object holds it.
    from langgraph.checkpoint.sqlite import SqliteSaver
    with closing(sqlite3.connect(service.store.home/'checkpoints.sqlite3')) as conn:
        records=list(SqliteSaver(conn).list(None))
    prepared_hash=next(r.checkpoint['channel_values']['prepared_hash'] for r in records if 'prepared_hash' in r.checkpoint['channel_values'])
    children=json.loads(service.store.read_blob(prepared_hash))
    row_hash=next(i['rowsHash'] for i in children if i.get('rowsHash'))
    assert row_hash not in {b['sha256'] for b in preview['unreferencedBlobs']}
    (service.store.home/'blobs'/prepared_hash).unlink()
    expect('BACKUP_CORRUPT',lambda:verify_references(service.store.home))


@pytest.mark.parametrize('format',['docx','xlsx','csv','json','zip'])
def test_export_whitelist_exact_content_and_cancel(prepared,monkeypatch,format):
    service,run=complete(prepared)
    monkeypatch.setattr('insight_agent.reports.converter_path',lambda:None)
    artifact=export_run(service.store,run['id'],format)
    data=service.store.read_blob(artifact['sha256'])
    if format=='docx':
        doc=Document(io.BytesIO(data));assert doc.tables[0].cell(1,2).text=='2'
        assert artifact['layoutQA']=='LAYOUT_UNVERIFIED'
    elif format=='xlsx':
        wb=load_workbook(io.BytesIO(data));assert wb.active.cell(2,4).value=='2' and wb.active.cell(2,4).data_type=='s';wb.close()
    elif format=='json':
        value=json.loads(data);assert value['results'][0]['value']=='2'
        assert 'sources' not in dumps(value) and '_source' not in dumps(value) and 'attachmentId' not in dumps(value)
    elif format=='zip':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert set(z.namelist())=={'README.txt','summary.json','results.xlsx','contract-summary.json','processing-list.json','audit.json','manifest.json'}
            for member in json.loads(z.read('manifest.json'))['files']:
                assert sha(z.read(member['name']))==member['sha256']
            exported = ''.join(z.read(n).decode('utf-8') for n in z.namelist() if n.endswith('.json'))
            assert '_source' not in exported and 'attachmentId' not in exported and 'baseUrl' not in exported
    else:assert '2026-10-01' in data.decode('utf-8-sig')
    response=service.queue('export',run['sessionId'],{'runId':run['id'],'format':format})
    job=service.store.claim();service.cancel(response['jobId'])
    before=len(service.store.list('artifact'))
    expect('CANCELLED',lambda:export_run(service.store,run['id'],format,job=job))
    assert len(service.store.list('artifact'))==before


def test_all_data_exports_include_metric_definition_content(prepared, monkeypatch):
    service, session, definition, _ = prepared
    submit(service, session, {"scope": "ALL_DATA"})
    assert run_once(service)
    run = service.store.list("run")[0]
    monkeypatch.setattr("insight_agent.reports.converter_path", lambda: None)
    json_artifact = export_run(service.store, run["id"], "json")
    exported = json.loads(service.store.read_blob(json_artifact["sha256"]))
    assert exported["analysisScope"] == "ALL_DATA"
    assert exported["results"][0]["definitionOrder"] == 1
    assert exported["results"][0]["definition"]["name"] == definition["candidates"][0]["name"]
    docx_artifact = export_run(service.store, run["id"], "docx")
    document = Document(io.BytesIO(service.store.read_blob(docx_artifact["sha256"])))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "当前会话全部数据文件" in text
    assert "指标说明文件中的定义" in text


@pytest.mark.parametrize('phase',['PREPARING','CALCULATING','AUDITING','COMMITTING'])
def test_crash_recovery_and_fenced_worker(prepared,monkeypatch,phase):
    from insight_agent.agent import execute_run
    service,session,_,parameters=prepared
    submit(service,session,parameters)
    job=service.store.claim();run_id=service.process_turn(job['payload']['turnId'],job)
    original=service.store.event
    def crash(db,scope,type,data):
        if type=='run.phase' and data['phase']==phase:raise RuntimeError('synthetic process death')
        return original(db,scope,type,data)
    monkeypatch.setattr(service.store,'event',crash)
    with pytest.raises(RuntimeError):execute_run(service.store,run_id,job)
    assert not service.store.get('run',run_id).get('committed')
    monkeypatch.setattr(service.store,'event',original)
    service.store.recover()
    expect('LEASE_LOST',lambda:service.store.finish(job,{}))
    assert run_once(service)
    run=service.store.get('run',run_id)
    assert run['committed'] and run['results'][0]['groups'][0]['displayValue']=='2'
    assert len([m for m in service.store.list('message') if m.get('runId')==run_id])==1


def test_hard_timeout_fences_and_preserves_commit(prepared):
    service,session,_,parameters=prepared
    submit(service,session,parameters)
    job=service.store.claim();run_id=service.process_turn(job['payload']['turnId'],job)
    service.store.expire(job['id'],job['lease'])
    assert service.store.jobs()[0]['error']['code']=='RUN_TIMEOUT'
    assert not service.store.get('run',run_id)['committed']
    expect('LEASE_LOST',lambda:service.store.finish(job,{}))


def test_comparisons_exact_zero_missing_and_baseline(tmp_path):
    from insight_agent.comparisons import compare_runs,export_comparison
    service=AppService(tmp_path/'ws')
    runs=[]
    for index,number in enumerate(['0','1']):
        contract={'baselineHash':'baseline','definitionManifestHash':'definition','candidates':[{'status':'CLEAR','rule':{'timeWindow':{'startParameter':'start','endParameter':'end'}}}], 'parameters':{'start':f'2026-0{index+1}-01','end':f'2026-0{index+2}-01'}}
        result={'metricId':'rate','name':'rate','unit':'percent','groups':[{'group':{'department':'A'},'outcome':'VALUE','displayValue':number,'exactNumerator':number,'exactDenominator':'3'}]}
        if index==0:result['groups'].append({'group':{'department':'B'},'outcome':'VALUE','displayValue':'5','exactNumerator':'5','exactDenominator':'1'})
        with service.store.transaction() as db:runs.append(service.store.put('run',{'sessionId':'s','committed':True,'mode':'ANALYZE','review':{'status':'PASS'},'contract':contract,'resultDigest':digest(result),'results':[result]},db))
    comparison=compare_runs(service.store,'s',[r['id'] for r in runs],'period')
    a,b=comparison['series']
    assert a['difference']=={'numerator':'1','denominator':'3'} and a['differenceUnit']=='percentage_point'
    assert a['relativeChangePercent'] is None and a['rateReason']=='ZERO_BASE'
    assert b['points'][1]['exact'] is None and b['reasonCode']=='POINT_NOT_AVAILABLE'
    comparison['series'][0]['points'][0]['exact']['numerator']='123456789012345678'
    assert export_comparison(comparison)['series'][0]['points'][0]['exact']['numerator']=='123456789012345678'
    with service.store.transaction() as db:
        runs[1]['contract']['baselineHash']='different';service.store.put('run',runs[1],db)
    expect('COMPARISON_INCOMPARABLE',lambda:compare_runs(service.store,'s',[r['id'] for r in runs],'period'))
