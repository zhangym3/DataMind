"""Exercise the document-fill result, progress and clear controls in an isolated Qt UI."""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from insight_agent.desktop import InsightWindow
from insight_agent.worker import run_once
from test_report_plan import FillGateway, uploaded_fill


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    output = ROOT / "artifacts/acceptance"
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix="DataMind-fill-UI-") as folder:
        service, session_id = uploaded_fill.__wrapped__(Path(folder))
        assert run_once(service, FillGateway())
        session = service.store.get("session", session_id)
        service.submit_user_turn(session_id, session["revision"], "fill-ui", "", "ANALYZE", {"scope": "ALL_DATA"})
        assert run_once(service)
        window = InsightWindow(service.store.home, start_worker=False)
        window.show()

        def js(expression):
            values = []
            window.page.runJavaScript("JSON.stringify((" + expression + "))", 0, values.append)
            deadline = time.monotonic() + 10
            while not values and time.monotonic() < deadline:
                QTest.qWait(30)
            assert values, expression
            return json.loads(values[0]) if values[0] else None

        def check(name, expression):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if js(expression):
                    checks.append(name)
                    return
                QTest.qWait(100)
            raise AssertionError(name + str(window.page.javascript_errors))

        try:
            check("report contains the original content with computed values", "document.querySelector('.filled-report')?.textContent.includes('2026-10-01：2台')")
            check("gap stays in the original planned slot", "document.querySelector('.filled-report')?.textContent.includes('待补充：缺少16:00排程历史快照')")
            check("formula and strict threshold are preserved", "document.querySelector('.filled-report')?.textContent.includes('严格大于20分钟，比例公式x100%')")
            check("definition shows positions without claiming that all are computable", "document.querySelector('#sideVersionDesc')?.textContent.includes('4 个待填位置') && !document.querySelector('#sideVersionDesc')?.textContent.includes('0 项可计算')")
            check("slot progress belongs to the original upload", "document.querySelector('.conversation-unit-upload')?.textContent.includes('待填清单已就绪')")
            check("exports and one gap are visible", "document.querySelectorAll('.run-card [data-action=export]').length===6 && document.querySelector('.filled-report summary')?.textContent.includes('1 处')")
            check("conversation units remain chronological", "[...document.querySelectorAll('.conversation-unit')].every((v,i,a)=>!i||a[i-1].dataset.created<=v.dataset.created)")
            js("document.querySelector('.filled-report').scrollIntoView({block:'center'})")
            QTest.qWait(200)
            window.web.grab().save(str(output / "document-fill-ui.png"))
            js("document.querySelector('#moreBtn').click()")
            js("document.querySelector('[data-action=clearConversation]').click()")
            js("(document.querySelector('#clearConversationAcknowledged').checked=true,document.querySelector('[data-action=clearConversationConfirm]').click())")
            check("clear hides messages and results from the current conversation view", "[...document.querySelectorAll('#chatFeed .conversation-unit,#chatFeed .filled-report')].every(v=>v.closest('.retained-history') && !v.closest('.retained-history').open) && Boolean(document.querySelector('.conversation-cleared-note'))")
            check("clear retains uploaded sources", "document.querySelectorAll('.file-row').length===2")
            assert service.store.list("run")[0]["filledReport"]
            checks.append("clear preserves the stored report for audit")
            assert not window.page.javascript_errors, window.page.javascript_errors
            checks.append("no JavaScript runtime errors")
            (output / "document-fill-ui-verification.json").write_text(json.dumps({"count": len(checks), "passed": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"PASS: {len(checks)} document fill UI checks", flush=True)
        finally:
            window.close()
            deadline = time.monotonic() + 15
            while (not window.ready_close or window.isVisible()) and time.monotonic() < deadline:
                QTest.qWait(100)
            assert window.ready_close and not window.isVisible()


if __name__ == "__main__":
    main()
