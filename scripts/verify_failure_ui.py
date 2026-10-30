"""Exercise persisted extraction/turn failures through the real Qt WebChannel."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docx import Document
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from insight_agent.core import AppError
from insight_agent.desktop import InsightWindow
from insight_agent.service import AppService
from insight_agent.worker import run_once


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    checks = []
    legacy = '--legacy' in sys.argv
    prefix = 'legacy-' if legacy else ''
    output = ROOT / 'artifacts' / 'acceptance'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='DataMind-failure-UI-') as folder:
        service = AppService(Path(folder) / 'workspace')
        session = service.create_session('指标提取失败验收')
        document = Document()
        document.add_paragraph('统计口径：以手术事件计数。')
        document.add_paragraph('分母：全部手术台次。')
        path = Path(folder) / '指标说明.docx'
        document.save(path)
        service.import_paths([str(path)], session['id'])
        assert run_once(service)
        # Recreate a pre-catalog upload whose original extraction failed.
        automatic = service.store.claim()
        assert automatic['kind'] == 'metric_catalog'
        service.store.finish(automatic, error=AppError('CANCELLED', '历史任务夹具').as_dict())
        service.ensure_definition_extract(service.session_metrics(session['id'])['id'])
        assert service.store.jobs()[0]['kind'] == 'extract', service.store.jobs()

        class FailingGateway:
            def call(self, *args):
                raise AppError('OUTBOUND_POLICY', '模型请求包含未批准的嵌套字段 <b>需重试</b>')

        assert run_once(service, FailingGateway())
        session = service.store.get('session', session['id'])
        turn = service.submit_user_turn(session['id'], session['revision'], 'failure-ui', '', 'ANALYZE', {'scope': 'ALL_DATA'})
        assert run_once(service)
        assert service.store.get('turn', turn['turnId'])['state'] == 'REJECTED'
        if legacy:
            # Older uploads kept the file and failed extraction, but never
            # linked the draft to the conversation. Opening it must recover.
            with service.store.transaction() as db:
                definition = service.store.list('definition', db)[0]
                definition.pop('sessionId')
                service.store.put('definition', definition, db)
                session = service.store.get('session', session['id'], db)
                service.store.put('session', session | {'definitionId': None}, db)
        window = InsightWindow(service.store.home, start_worker=False)
        window.show()

        def js(expression):
            results = []
            window.page.runJavaScript('JSON.stringify((' + expression + '))', 0, results.append)
            deadline = time.monotonic() + 10
            while not results and time.monotonic() < deadline:
                QTest.qWait(30)
            assert results, expression
            return json.loads(results[0]) if results[0] else None

        def check(name, expression):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if js(expression):
                    checks.append(name)
                    return
                QTest.qWait(100)
            window.web.grab().save(str(output / (prefix + 'failure-ui-debug.png')))
            raise AssertionError(name + ' / ' + str(window.page.javascript_errors))

        try:
            check('analysis rejection shows persisted code and upstream cause', "document.querySelector('.conversation-unit-turn .job-failure')?.textContent.includes('DEFINITION_UNAVAILABLE') && document.querySelector('.conversation-unit-turn .job-failure')?.textContent.includes('未批准的嵌套字段')")
            check('extraction failure belongs to the upload unit', "document.querySelector('.conversation-unit-upload .job-failure')?.textContent.includes('OUTBOUND_POLICY')")
            check('failed extraction no longer claims to be running', "document.querySelector('.conversation-unit-upload')?.textContent.includes('自动提取未完成') && !document.querySelector('.conversation-unit-upload')?.textContent.includes('正在自动提取')")
            check('failure content is escaped', "!document.querySelector('.job-failure b') && document.querySelector('.job-failure')?.textContent.includes('<b>需重试</b>')")
            check('uploads and turns retain chronological order', "[...document.querySelectorAll('.conversation-unit')].every((item,index,all)=>!index||all[index-1].dataset.created<=item.dataset.created)")
            check('failure exposes retry and inspection controls', "Boolean(document.querySelector('.conversation-unit-upload [data-action=retryExtract]')) && Boolean(document.querySelector('.conversation-unit-upload [data-action=definition]'))")
            if legacy:
                recovered = service.store.get('session', session['id'])
                assert recovered['definitionId'] == definition['id']
                assert service.store.get('definition', definition['id'])['sessionId'] == session['id']
                checks.append('opening a legacy conversation restores its original document link')
            window.web.grab().save(str(output / (prefix + '05-failure-details.png')))
            js("document.querySelector('.conversation-unit-upload [data-action=definition]').click()")
            check('inspection opens actual linked definition', "document.querySelector('#overlay').classList.contains('show') && document.querySelector('#dialogBody').textContent.includes('手术事件')")
            js("document.querySelector('#closeModal').click()")
            js("document.querySelector('.conversation-unit-upload [data-action=retryExtract]').click()")
            check('retry queues catalog reading and replaces the historical extraction failure', "document.querySelector('.conversation-unit-upload')?.textContent.includes('等待自动提取') && !document.querySelector('.conversation-unit-upload .job-failure')")
            assert sum(job['kind'] == 'metric_catalog' and job['status'] == 'QUEUED' for job in service.store.jobs()) == 1
            assert not window.page.javascript_errors, window.page.javascript_errors
            checks.append('no JavaScript errors; retry is persisted once')
            (output / (prefix + 'failure-ui-verification.json')).write_text(json.dumps({'passed': checks, 'count': len(checks)}, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'PASS: {len(checks)} Qt failure recovery checks', flush=True)
        finally:
            # The retry is deliberately left queued for inspection, with no
            # worker. Cancel this fixture job before closing its test window.
            for job in service.store.jobs():
                if job['status'] == 'QUEUED':
                    service.cancel(job['id'])
            window.close()
            deadline = time.monotonic() + 15
            while (not window.ready_close or window.isVisible()) and time.monotonic() < deadline:
                QTest.qWait(100)
            assert window.ready_close and not window.isVisible(), 'Safe shutdown did not finish'
            app.processEvents()


if __name__ == '__main__':
    main()
