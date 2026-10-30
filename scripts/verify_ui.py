"""Real Qt/WebChannel acceptance journey using synthetic Office documents only."""
from __future__ import annotations

import json
import multiprocessing
import runpy
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from insight_agent.desktop import InsightWindow


def main():
    artifacts = ROOT / "artifacts" / "acceptance"
    artifacts.mkdir(parents=True, exist_ok=True)
    checks = []
    app = QApplication.instance() or QApplication(sys.argv)
    with tempfile.TemporaryDirectory(prefix="DataMind-UI-中文-") as folder:
        fixture = runpy.run_path(str(ROOT / "tests" / "test_integration.py"))
        service, session, _, _ = fixture["prepared"].__wrapped__(Path(folder))
        for mapping in session['mappings'].values():
            mapping['coverage']['start'] = '2026-09-01'
        session = service.update_session(session['id'], session['revision'], {'mappings': session['mappings']})
        window = InsightWindow(service.store.home)
        window.show()
        QTest.qWait(1500)

        def js(expression):
            result = []
            window.page.runJavaScript("JSON.stringify((" + expression + "))", 0, result.append)
            deadline = time.monotonic() + 10
            while not result and time.monotonic() < deadline:
                QTest.qWait(20)
            assert result, "JavaScript callback timed out"
            return json.loads(result[0]) if result[0] else None

        def until(expression, timeout=20):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if js(expression):
                    return
                QTest.qWait(100)
            window.web.grab().save(str(artifacts / 'failure.png'))
            raise AssertionError(expression + " / debug: " + str(js("JSON.stringify({prompt:document.querySelector('#prompt')?.value,title:document.querySelector('#chatName')?.outerHTML,toast:document.querySelector('#toast')?.innerText})")) + " / console: " + str(window.page.javascript_errors))

        def check(name, expression):
            until(expression)
            checks.append(name)

        def click(selector):
            assert js(f"Boolean(document.querySelector({json.dumps(selector)}))"), selector
            js(f"document.querySelector({json.dumps(selector)}).click()")
            QTest.qWait(200)

        try:
            check("real WebChannel bootstrap and persistent session", "document.querySelector('#chatName').textContent==='合成手术数据'")
            check("no demonstration authority", "document.querySelectorAll('.file-row').length===1 && ![...document.scripts].some(s=>s.src.includes('demo'))")
            check("data and metric context panels are separated", "document.querySelector('#dataFiles') && document.querySelector('#metricFile') && document.querySelector('#pendingFiles') && document.querySelector('#metricFileCounter').textContent==='0 / 1'")
            check("existing workbook is shown in data files panel", "document.querySelector('#dataFiles .file-row') && document.querySelector('#dataFiles .file-row .file-icon').textContent==='X' && !document.querySelector('#metricFile .file-row')")
            check("upload batch is rendered as one chronological conversation unit", "document.querySelectorAll('.conversation-unit-upload').length===1 && document.querySelector('.conversation-unit-upload')?.textContent.includes('上传文件')")
            click('#analyzeBtn')
            check("real persistent worker and LangGraph publish exact result", "document.querySelector('.run-card')?.textContent.includes('已完成') && document.querySelector('.result-metric')?.textContent.includes('2')")
            check("all-data scope and definition-backed output are visible", "Boolean(document.querySelector('.run-scope')?.textContent) && Boolean(document.querySelector('.analysis-flow')) && Boolean(document.querySelector('.metric-definition'))")
            check("analysis turn is rendered after upload unit", "document.querySelectorAll('.conversation-unit-turn').length>=1 && [...document.querySelectorAll('.conversation-unit')].every((item,index,all)=>index===0||all[index-1].dataset.created<=item.dataset.created)")
            check("baseline locked after analysis", "document.querySelector('#mappingBtn').disabled")
            click('[data-action="evidence"]')
            check("row evidence available", "document.querySelector('#dialogBody').textContent.includes('手术事件')")
            click('#closeModal')
            click('[data-action="copy"]')
            assert '分析已完成' in app.clipboard().text()
            checks.append("native clipboard copies committed answer")
            click('[data-action="feedback"][data-value="true"]')
            check("feedback persists", "document.querySelector('[data-action=feedback][data-value=true]').textContent==='取消赞'")
            click('[data-action="export"][data-format="docx"]')
            check("Word export honestly marked layout unverified", "document.querySelector('.artifact')?.textContent.includes('版面未核验')")
            window.web.grab().save(str(artifacts / '01-analysis.png'))
            click('[data-action="query"]')
            click('[data-action="querySubmit"]')
            check('local result filter references existing result without recomputation', "document.querySelector('#chatFeed').textContent.includes('匹配 1 组') && document.querySelectorAll('.run-card').length===1")
            click('[data-action="formula"]')
            check('local formula inspection works without online model', "document.querySelector('#chatFeed').textContent.includes('以下为已冻结口径')")
            check('all-data run hides date comparison controls', "document.querySelectorAll('[data-action=compare]').length===0 && !document.querySelector('#dateStart') && !document.querySelector('#dateEnd')")
            check('indicator management is available while locked analysis stays in chat', "!!document.querySelector('#navLibrary') && document.querySelector('#libraryView').hidden && document.querySelector('#selectMetricVersion').disabled")
            click('#viewVersion')
            check("published library metrics expose original evidence and version maintenance", "document.querySelector('#dialogBody').textContent.includes('严格超过20分钟') && !!document.querySelector('[data-action=libClone]') && !document.querySelector('[data-action=libPublish]') && document.querySelector('#metricParse').disabled")
            window.web.grab().save(str(artifacts / '02-metrics.png'))
            click('#closeModal')
            click('#openNew')
            check("new data session does not inherit files", "document.querySelector('#chatName').textContent==='新数据对话' && document.querySelectorAll('.file-row').length===0")
            js("document.querySelector('#prompt').focus()")
            QTest.qWait(100)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(Path(folder) / '测试数据.xlsx'))])
            app.clipboard().setMimeData(mime)
            # A physical Qt key event exercises the native clipboard event filter.
            window.activateWindow()
            QTest.qWait(100)
            QTest.keyClick(window.web.focusProxy(), Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            check("Ctrl+V file URL imports real local workbook", "document.querySelectorAll('.file-row').length===1")
            app.clipboard().setText('纯文本粘贴')
            js("(document.querySelector('#prompt').value='',true)")
            QTest.keyClick(window.web.focusProxy(), Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            QTest.qWait(300)
            check("Ctrl+V text inserts exactly once", "document.querySelector('#prompt').value==='纯文本粘贴'")
            QTest.qWait(1000)
            check("composer draft persisted", "true")
            assert '纯文本粘贴' in service.store.list('session')[0]['draft']
            click('#moreBtn')
            click('[data-action="archive"]')
            check('archive disables new analysis', "document.querySelector('#chatTags').textContent.includes('已归档') && document.querySelector('#analyzeBtn').disabled")
            click('#moreBtn')
            click('[data-action="archive"]')
            check('restore archive preserves attachments', "!document.querySelector('#analyzeBtn').disabled && document.querySelectorAll('.file-row').length===1")
            click('#moreBtn')
            check('current data conversation exposes permanent delete', "document.querySelector('[data-action=deleteSession]')?.textContent==='永久删除数据对话'")
            click('#closeModal')
            for number in range(35):
                service.create_session(f'分页验收 {number:02}')
            window.bridge.changed.emit()
            check('real history first page capped at thirty', "document.querySelectorAll('.history-item').length===30")
            click('[data-action="pageNext"]')
            check('second history page available', "document.querySelectorAll('.history-item').length===7")
            js("(document.querySelector('#search').value='分页验收 00',document.querySelector('#search').dispatchEvent(new Event('input')),true)")
            check('history search filters database', "document.querySelectorAll('.history-item').length===1 && document.querySelector('.history-item').textContent.includes('分页验收 00')")
            window.resize(1100, 700)
            QTest.qWait(200)
            check("minimum window has no horizontal document overflow", "document.documentElement.scrollWidth===innerWidth")
            window.web.grab().save(str(artifacts / '03-compact.png'))
            window.web.setZoomFactor(1.5)
            if not js("document.querySelector('#app').classList.contains('context-collapsed')"):
                click('#toggleContext')
            QTest.qWait(250)
            check('150 percent zoom keeps composer and controls within window', "document.querySelector('#sendBtn').getBoundingClientRect().bottom<=innerHeight && document.querySelector('#analyzeBtn').getBoundingClientRect().right<=innerWidth && document.documentElement.scrollWidth===innerWidth")
            js("document.querySelector('#workspaceInfo').focus()")
            check('settings reachable by keyboard at 150 percent zoom', "document.activeElement.id==='workspaceInfo' && document.querySelector('#workspaceInfo').getBoundingClientRect().bottom<=innerHeight")
            window.web.grab().save(str(artifacts / '04-zoom-150.png'))
            assert not window.page.javascript_errors, window.page.javascript_errors
            checks.append('no JavaScript runtime errors')
            (artifacts / 'ui-verification.json').write_text(json.dumps({'passed':checks,'count':len(checks)},ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'PASS: {len(checks)} Qt acceptance checks; {artifacts}',flush=True)
        finally:
            window.stop_worker()
            window.close()
            deadline = time.monotonic() + 15
            while (not window.ready_close or window.isVisible()) and time.monotonic() < deadline:
                QTest.qWait(100)
            assert window.ready_close and not window.isVisible(), "Safe shutdown did not release workspace"
            assert not (service.store.home / 'instance.lock').exists(), "Workspace lock leaked"


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
