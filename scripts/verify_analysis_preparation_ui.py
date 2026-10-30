"""Verify one-click analysis preparation through the real Qt WebChannel."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from insight_agent.desktop import InsightWindow
from insight_agent.worker import run_once
from test_analysis_preparation import TimeoutGateway
from test_metric_catalog import CatalogGateway
from test_report_plan import FillGateway, uploaded_fill


def verify_scenario(folder, output, old_timeout):
    service, session_id = uploaded_fill.__wrapped__(folder)
    if old_timeout:
        assert run_once(service, TimeoutGateway())
        service.parse_session_metrics(session_id)
        assert run_once(service, CatalogGateway())
        assert service.session_metric_catalog(session_id)["status"] == "READY"
    window = InsightWindow(service.store.home, start_worker=False)
    window.show()
    checks = []

    def js(expression):
        values = []
        window.page.runJavaScript("JSON.stringify((" + expression + "))", 0, values.append)
        deadline = time.monotonic() + 10
        while not values and time.monotonic() < deadline:
            QTest.qWait(30)
        assert values, expression
        return json.loads(values[0]) if values[0] else None

    def check(name, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                checks.append(name)
                return
            QTest.qWait(100)
        raise AssertionError(name + " / " + str(window.page.javascript_errors)
                             + " / " + str(js("document.querySelector('#toast')?.textContent")))

    def turn_job():
        return next((j for j in service.store.jobs() if j["kind"] == "turn"), None)

    try:
        check("analysis and plan buttons are available before submitting",
              lambda: js("document.querySelectorAll('.file-row').length===2 && !document.querySelector('#analyzeBtn').disabled && !document.querySelector('#planBtn').disabled"))
        if not old_timeout:
            check("guidance allows analysis while the explanation is parsing",
                  lambda: js("document.querySelector('#conversationGuidance').textContent.includes('开始分析时会等待规则就绪后自动继续')"))
        js("document.querySelector('#analyzeBtn').click()")
        check("one button click submits one analysis request", lambda: turn_job() is not None)
        job_id = turn_job()["id"]
        turn_id = turn_job()["payload"]["turnId"]
        check("pending analysis disables duplicate submission",
              lambda: js("document.querySelector('#analyzeBtn').disabled && document.querySelector('#planBtn').disabled"))
        if old_timeout:
            assert run_once(service)
            assert turn_job()["status"] == "QUEUED"
        pending = turn_job()
        dependency_id = pending["payload"]["waitForJobId"]
        assert pending["status"] == "QUEUED"
        assert service.store.get("turn", turn_id)["state"] == "PENDING"
        checks.append("the same request persists its preparation dependency")
        check("preparation progress is visible in the conversation",
              lambda: js("document.querySelector('#chatFeed').textContent.includes('正在准备本次说明的指标规则，完成后自动继续处理请求')"))
        assert run_once(service, FillGateway())
        assert service.session_metrics(session_id)["status"] == "PUBLISHED"
        check("successful preparation is shown before resuming analysis",
              lambda: js("document.querySelector('#chatFeed').textContent.includes('指标规则已就绪，正在继续本次分析')"))
        assert run_once(service)
        completed = turn_job()
        assert completed["id"] == job_id and completed["status"] == "SUCCEEDED", completed
        runs = service.store.list("run")
        assert len(runs) == 1 and runs[0]["committed"]
        assert runs[0]["id"] == completed["result"]["runId"]
        assert len([j for j in service.store.jobs() if j["kind"] == "turn"]) == 1
        checks.append("one click resumes the same request and commits exactly one run")
        assert [r["groups"][0]["displayValue"] for r in runs[0]["results"] if r["outcome"] == "VALUE"] == ["2", "1", "1"]
        check("computed results appear without a second click",
              lambda: js("document.querySelector('.filled-report')?.textContent.includes('2026-10-01：2台')"))
        check("unavailable source data remains an explicit report gap",
              lambda: js("document.querySelector('.filled-report')?.textContent.includes('缺少16:00排程历史快照')"))
        assert not window.page.javascript_errors, window.page.javascript_errors
        checks.append("no JavaScript runtime errors")
        scenario = "old-timeout" if old_timeout else "active-extraction"
        window.web.grab().save(str(output / f"analysis-preparation-{scenario}.png"))
        return {"scenario": scenario, "count": len(checks), "passed": checks,
                "turnId": turn_id, "jobId": job_id, "preparationJobId": dependency_id,
                "runCount": len(runs), "values": ["2", "1", "1"]}
    finally:
        for job in service.store.jobs():
            if job["status"] == "QUEUED":
                service.cancel(job["id"])
        window.close()
        deadline = time.monotonic() + 15
        while (not window.ready_close or window.isVisible()) and time.monotonic() < deadline:
            QTest.qWait(100)
        assert window.ready_close and not window.isVisible(), "Safe shutdown did not finish"
        QApplication.instance().processEvents()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    output = ROOT / "artifacts/acceptance"
    output.mkdir(parents=True, exist_ok=True)
    scenarios = []
    for old_timeout in (True, False):
        with tempfile.TemporaryDirectory(prefix="DataMind-analysis-preparation-UI-") as folder:
            scenarios.append(verify_scenario(Path(folder), output, old_timeout))
    result = {"count": sum(s["count"] for s in scenarios), "scenarios": scenarios,
              "isolatedWorkspace": True, "syntheticData": True, "realModelRequests": 0}
    (output / "analysis-preparation-ui-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: {result['count']} one-click analysis UI checks", flush=True)
    app.processEvents()


if __name__ == "__main__":
    main()
