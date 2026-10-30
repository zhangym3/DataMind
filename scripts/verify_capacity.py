"""One synthetic capacity run through the actual bounded worker; no online calls."""
import json
import multiprocessing
import platform
import runpy
import sys
import tempfile
import time
from pathlib import Path

import psutil
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from insight_agent.worker import worker_main
    result = {'status': 'RUNNING', 'rows': 300000, 'sheets': 3, 'columns': 4,
              'trials': 1, 'latencyPercentiles': None, 'syntheticOnly': True,
              'python': platform.python_version(), 'platform': platform.platform(),
              'cpu': platform.processor(), 'logicalCpus': psutil.cpu_count(),
              'hostMemoryBytes': psutil.virtual_memory().total, 'samples': []}
    output = ROOT / 'artifacts/acceptance/capacity.json'
    peak = 0
    with tempfile.TemporaryDirectory(prefix='DataMind-capacity-') as folder:
        folder = Path(folder)
        fixture = runpy.run_path(str(ROOT / 'tests/test_integration.py'))
        service, session, definition, parameters = fixture['prepared'].__wrapped__(folder)
        session = service.update_session(session['id'], session['revision'], {'attachmentIds': [], 'mappings': {}})
        rule = definition['candidates'][0]['rule']
        headers = [field['id'] for field in rule['requiredFields']]
        wb = Workbook(write_only=True)
        for index in range(3):
            sheet = wb.create_sheet(f'Synthetic-{index+1}')
            sheet.append(headers)
            for row in range(100000):
                # Independent expectation: half are 21 minutes (included),
                # half exactly 20 (excluded by the strictly-greater rule).
                sheet.append([f'S{index}-{row}', '2026-10-15', '2026-10-15T08:00:00',
                              '2026-10-15T08:21:00' if row % 2 else '2026-10-15T08:20:00'])
        source = folder / 'synthetic-capacity.xlsx'
        wb.save(source)
        result['inputBytes'] = source.stat().st_size
        stop = multiprocessing.Event()
        process = multiprocessing.Process(target=worker_main, args=(service.store.home, stop))
        process.start()
        monitor = psutil.Process(process.pid)

        def wait_job(id, phase):
            nonlocal peak
            started = time.monotonic()
            while time.monotonic() - started < 900:
                assert process.is_alive(), f'Worker exited {process.exitcode}'
                memory = monitor.memory_info()
                peak = max(peak, memory.rss)
                jobs = {j['id']: j for j in service.store.jobs()}
                job = jobs[id]
                if job['status'] not in ('QUEUED', 'RUNNING'):
                    assert job['status'] == 'SUCCEEDED', job
                    result['samples'].append({'phase': phase, 'seconds': round(time.monotonic()-started, 3)})
                    print(f'{phase} passed in {result["samples"][-1]["seconds"]}s', flush=True)
                    return job
                time.sleep(0.1)
            raise AssertionError(f'{phase} exceeded 900 seconds')

        try:
            job = wait_job(service.import_paths([str(source)], session['id'])['jobId'], 'parse_import')
            assert not job['result']['failures'], job
            session = service.store.get('session', session['id'])
            assert len(session['attachmentIds']) == 1
            id = session['attachmentIds'][0]
            attachment = service.store.get('attachment', id)
            assert [s['rowCount'] for s in attachment['profile']['sheets']] == [100000]*3
            mapping = {rule['metricId']: {'mode': 'union', 'sources': [
                {'attachmentId': id, 'sheet': f'Synthetic-{i+1}', 'fields': {h: h for h in headers}}
                for i in range(3)], 'coverage': {'start': '2026-10-01', 'end': '2026-11-01',
                                                'complete': True, 'evidence': 'Synthetic full population'}}}
            session = service.update_session(session['id'], session['revision'], {'mappings': mapping})
            wait_job(service.submit_user_turn(session['id'], session['revision'], 'capacity', '', 'ANALYZE', parameters)['jobId'], 'analyze_commit')
            run = service.store.list('run')[0]
            assert run['committed'] and run['results'][0]['groups'][0]['displayValue'] == '150000', run['results']
            assert peak < 2 * 1024**3
            result.update(status='PASS', expected='150000', actual=run['results'][0]['groups'][0]['displayValue'],
                          peakWorkerRssBytes=peak, memoryLimitBytes=2*1024**3,
                          contractHash=run['contractHash'], resultDigest=run['resultDigest'])
        except Exception as error:
            result.update(status='FAILED', error=str(error), peakWorkerRssBytes=peak)
            raise
        finally:
            stop.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
