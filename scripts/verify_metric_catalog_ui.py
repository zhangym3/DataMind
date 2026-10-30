"""Verify the standalone metric catalogue through the real Qt WebChannel."""
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


class SyntheticCatalog:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def call(self, stage, payload, schema, budget_id):
        self.calls.append(payload)
        if stage != 'metric_catalog' or self.fail and len(self.calls) == 3:
            raise AppError('MODEL_UNAVAILABLE', '合成批次失败，可继续解析')
        if not payload['items']:
            return {'items': [{'name': f'<b>指标{i}</b>', 'category': '时间监测', 'locators': [n['locator']]}
                              for i, n in enumerate(payload['nodes'][1:], 1)],
                    'contextLocators': [payload['nodes'][0]['locator']], 'nonMetricLocators': []}
        return {'items': [{'id': i['id'], 'unit': '台', 'dimensions': ['科室'],
                          'timeWindow': '前一日', 'formula': '符合条件的手术事件数',
                          'conditions': ['入室至麻醉超过20分钟'], 'exclusions': ['移植手术'],
                          'unresolved': ['需确认日期归属'], 'locators': i['locators']}
                         for i in payload['items']]}


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    checks = []
    output = ROOT / 'artifacts' / 'acceptance'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='DataMind-catalog-UI-') as folder:
        service = AppService(Path(folder) / 'workspace')
        session = service.create_session('指标说明独立解析验收')
        document = Document()
        document.add_paragraph('统计口径：按手术事件、科室计数。')
        for index in range(1, 7):
            document.add_paragraph(f'指标{index}：入室至麻醉超过20分钟的台数；排除条件：移植手术。')
        path = Path(folder) / '指标说明.docx'
        document.save(path)
        service.import_paths([str(path)], session['id'])
        assert run_once(service)
        window = InsightWindow(service.store.home, start_worker=False)
        window.show()

        def js(expression):
            result = []
            window.page.runJavaScript('JSON.stringify((' + expression + '))', 0, result.append)
            deadline = time.monotonic() + 10
            while not result and time.monotonic() < deadline:
                QTest.qWait(30)
            assert result, expression
            return json.loads(result[0]) if result[0] else None

        def check(name, expression):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if js(expression):
                    checks.append(name)
                    return
                QTest.qWait(100)
            window.web.grab().save(str(output / 'metric-catalog-ui-debug.png'))
            raise AssertionError(name + ' / ' + str(window.page.javascript_errors))

        try:
            check('Word-only upload automatically queues parsing and provides its result entry',
                  "Boolean(document.querySelector('#metricParse')) && !document.querySelector('#metricParse').disabled && document.querySelector('#metricFileHint').textContent.includes('无需上传数据文件')")
            js("document.querySelector('#metricParse').click()")
            check('parse button queues the catalog and opens its own dialog',
                  "document.querySelector('#overlay').classList.contains('show') && document.querySelector('#dialogTitle').textContent==='指标说明解析' && document.querySelector('#dialogBody').textContent.includes('正在解析')")
            assert sum(j['kind'] == 'metric_catalog' for j in service.store.jobs()) == 1
            assert run_once(service, SyntheticCatalog(fail=True))
            check('partial progress appears live without reopening the dialog',
                  "document.querySelectorAll('.catalog-metric').length===6 && document.querySelectorAll('.catalog-metric dl').length===4 && document.querySelector('#dialogBody').textContent.includes('合成批次失败')")
            check('partial results preserve questions conditions and exclusions',
                  "document.querySelector('.catalog-metric').textContent.includes('需确认日期归属') && document.querySelector('.catalog-metric').textContent.includes('移植手术') && document.querySelector('.catalog-metric').textContent.includes('超过20分钟')")
            check('model text is escaped',
                  "!document.querySelector('.catalog-metric b') && document.querySelector('.catalog-metric').textContent.includes('<b>指标1</b>')")
            js("document.querySelector('.catalog-metric details').open=true")
            check('source evidence can be expanded',
                  "document.querySelector('.catalog-metric details[open] .source-node').textContent.includes('排除条件：移植手术')")
            window.web.grab().save(str(output / 'metric-catalog-partial.png'))
            js("document.querySelector('#dialogFooter [data-action=parseMetricSpec]').click()")
            check('continuation queues one new job and shows progress',
                  "document.querySelector('#dialogBody').textContent.includes('正在解析') && !document.querySelector('#dialogFooter [data-action=parseMetricSpec]')")
            resumed = SyntheticCatalog()
            assert run_once(service, resumed)
            assert len(resumed.calls) == 1 and len(resumed.calls[0]['items']) == 2
            checks.append('continuation requests only the two pending items')
            check('all completed details refresh live and button opens cached result',
                  "document.querySelectorAll('.catalog-metric dl').length===6 && document.querySelector('#metricParse').textContent==='查看解析结果' && document.querySelector('#dialogBody').textContent.includes('解析完成') && !document.querySelector('#dialogBody .job-failure')")
            js("document.querySelector('#closeModal').click()")
            js("document.querySelector('#metricParse').click()")
            check('cached catalog reopens with its retained source evidence',
                  "document.querySelector('#overlay').classList.contains('show') && document.querySelectorAll('.catalog-metric .source-node').length===6")
            assert not service.store.list('run')
            assert service.store.get('session', session['id'])['state'] == 'DRAFT'
            assert not window.page.javascript_errors, window.page.javascript_errors
            checks.append('no computation run, baseline lock or JavaScript error')
            window.web.grab().save(str(output / 'metric-catalog-ready.png'))
            (output / 'metric-catalog-ui-verification.json').write_text(
                json.dumps({'passed': checks, 'count': len(checks)}, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'PASS: {len(checks)} Qt metric catalog checks', flush=True)
        finally:
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
