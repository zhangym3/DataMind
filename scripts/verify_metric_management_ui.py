"""Exercise restored management and offline metric forms through Qt WebChannel."""
# ruff: noqa: E402 -- Qt environment and project path must be set before imports.
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docx import Document
from openpyxl import Workbook
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from insight_agent.desktop import InsightWindow
from insight_agent import manual_metrics
from insight_agent.worker import run_once


def verify(folder, output):
    window = InsightWindow(folder / "home", start_worker=False)
    window.resize(1440, 1000)
    window.show()
    service = window.service
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
        raise AssertionError(name + " / " + str(window.page.javascript_errors) + " / " + str(js("document.querySelector('#toast')?.textContent")))

    def click(selector):
        check("button exists: " + selector, lambda: js(f"!!document.querySelector({json.dumps(selector)})"))
        js(f"document.querySelector({json.dumps(selector)}).click()")

    def fill(values):
        js("(()=>{" + "".join(f"document.getElementById({json.dumps(key)}).value={json.dumps(value)};" for key, value in values.items()) + "return true;})()")

    def screenshot(name):
        # WebEngine can report DOM changes before it paints its next frame.
        window.resize(window.width() + 1, window.height())
        QTest.qWait(1200)
        window.resize(window.width() - 1, window.height())
        QTest.qWait(500)
        assert window.web.grab().save(str(output / name))

    try:
        check("bootstrap and restored navigation load", lambda: js("!!document.querySelector('#readiness') && document.querySelector('#readiness').textContent!=='正在连接本机服务'"))
        click("#navLibrary")
        check("management view sets native library import context", lambda: window.bridge.library_context and js("!document.querySelector('#libraryView').hidden"))
        click("#manualMetric")
        check("manual form is available without Word or model", lambda: js("!!document.getElementById('manual_name')"))
        fill({"manual_collection": "合成手术指标", "manual_name": "手术台数", "manual_description": "按手术事件唯一编号计算本次全部手术台数", "manual_actor": ""})
        click('[data-action="libSaveManual"]')
        check("incomplete form stays open and does not create records", lambda: not service.store.list("definition") and js("document.querySelector('#toast').textContent.includes('维护人')"))
        fill({"manual_actor": "合成维护人"})
        screenshot("metric-management-manual-form.png")
        click('[data-action="libSaveManual"]')
        check("manual indicator is persisted and shown", lambda: len(service.store.list("definition")) == 1 and js("!!document.querySelector('[data-action=libAddMetric]')"))
        draft = service.store.list("definition")[0]
        metric_id = draft["candidates"][0]["id"]
        click('[data-action="libAddMetric"]')
        check("existing draft supports another manual indicator", lambda: js("!!document.querySelector('#manual_method')"))
        fill({"manual_name": "平均时长", "manual_description": "对本次全部手术的 duration 字段计算平均分钟数", "manual_method": "mean", "manual_valueField": "duration", "manual_unit": "minute", "manual_scale": 1})
        js("document.querySelector('#manual_method').dispatchEvent(new Event('change'))")
        click('[data-action="libSaveManual"]')
        check("two manually compiled indicators are shown", lambda: len(service.store.get("definition", draft["id"])["candidates"]) == 2 and js("document.querySelector('#dialogBody').textContent.includes('平均时长') && !!document.querySelector('[data-action=libPublish]')"))
        click(f'[data-action="libEditMetric"][data-id="{metric_id}"]')
        check("manual editing preloads the existing metric", lambda: js("document.querySelector('#manual_name')?.value==='手术台数'"))
        fill({"manual_name": "手术事件台数"})
        click('[data-action="libSaveManual"]')
        check("editing retains metric identity", lambda: service.store.get("definition", draft["id"])["candidates"][0]["name"] == "手术事件台数" and js("!!document.querySelector('[data-action=libPublish]')"))
        click('[data-action="libPublish"]')
        check("publication asks for the existing review contract", lambda: js("!!document.querySelector('#libReviewed')"))
        js("document.querySelector('#libReviewed').checked=true")
        click('[data-action="libPublishConfirm"]')
        check("reviewed version publishes", lambda: service.store.get("definition", draft["id"])["status"] == "PUBLISHED" and js("!!document.querySelector('[data-action=libNewSession]')"))
        screenshot("metric-management-published.png")
        click('#dialogFooter [data-action="libNewSession"]')
        check("published metrics bind to a new data conversation", lambda: len(service.store.list("session")) == 1 and js("!document.querySelector('#chatView').hidden && document.querySelector('#sideVersion').textContent==='合成手术指标'"))
        session = service.store.list("session")[0]
        assert session["definitionId"] == draft["id"] and not session["attachmentIds"]
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "手术数据"
        sheet.append(["surgery_event_id", "operation_date", "duration"])
        for key, value in (("a", 10), ("b", 20), ("c", 30)):
            sheet.append([key, "2026-10-01", value])
        path = folder / "合成数据.xlsx"
        workbook.save(path)
        window.bridge.import_paths([str(path)])
        assert run_once(service)
        window.bridge.changed.emit()
        check("data import stays in the selected conversation", lambda: js("document.querySelectorAll('#dataFiles .file-row').length===1"))
        click("#mappingBtn")
        check("both manual rules have field mapping controls", lambda: js("document.querySelectorAll('.mapping-field').length===2"))
        fill({"mappingActor": "合成维护人", "mappingReason": "字段与合成数据一致"})
        js("(()=>{document.querySelectorAll('.coverage-evidence').forEach(x=>x.value='合成全量记录');document.querySelectorAll('.coverage-complete').forEach(x=>x.checked=true);return true;})()")
        click('[data-action="saveMapping"]')
        check("manual rules receive reviewed source mappings", lambda: len(service.store.get("session", session["id"])["mappings"]) == 2 and js("!document.querySelector('#overlay').classList.contains('show')"))
        click("#analyzeBtn")
        check("start analysis queues a real request", lambda: any(j["kind"] == "turn" for j in service.store.jobs()))
        assert run_once(service)
        window.bridge.changed.emit()
        check("offline computation commits both manual results", lambda: len(service.store.list("run")) == 1 and service.store.list("run")[0].get("committed") and js("document.querySelectorAll('.result-metric').length>=2"))
        results = service.store.list("run")[0]["results"]
        assert [r["groups"][0]["displayValue"] for r in results] == ["3", "20.0"]
        assert not service.store.list("model_call")
        checks.append("counts and mean equal independent expectations with zero model calls")
        screenshot("metric-management-calculated.png")
        click("#navLibrary")
        check("library context is restored after analysis", lambda: window.bridge.library_context)
        click(f'#versions [data-action="libShow"][data-id="{draft["id"]}"]')
        click('[data-action="libClone"]')
        check("published rules can be continued as v2", lambda: len(service.store.list("definition")) == 2 and js("document.querySelector('#libVersion')?.value==='v2'"))
        assert service.store.get("definition", draft["id"])["status"] == "PUBLISHED"
        parent = service.store.list("definition")[0]
        child = manual_metrics.clone(service.store, parent["id"], parent["revision"])
        child = service.publish_bind(child["id"], child["revision"], "合成审核人", True)
        old_run = service.store.list("run")[0]
        click('[data-action="libDelete"]')
        check("delete dialog explains retained history", lambda: js("document.querySelector('#dialogBody').textContent.includes('历史追溯')"))
        fill({"libDeleteText": "错误确认"})
        click('[data-action="libDeleteConfirm"]')
        check("incorrect confirmation keeps the draft", lambda: not service.store.get("definition", parent["id"]).get("deletedAt") and js("document.querySelector('#toast').textContent.includes('确认删除')"))
        fill({"libDeleteText": "确认删除"})
        click('[data-action="libDeleteConfirm"]')
        check("referenced draft disappears from management", lambda: service.store.get("definition", parent["id"]).get("deletedAt") and js(f"!document.querySelector('#versions [data-id=\"{parent['id']}\"]') && !document.querySelector('#overlay').classList.contains('show')"))
        check("deletion reports retained historical snapshot", lambda: js("document.querySelector('#toast').textContent.includes('快照保留')"))
        assert service.store.get("definition", child["id"]) == child
        assert service.store.list("run")[0] == old_run
        assert child["manifest"]["parentId"] == parent["id"]
        checks.append("published child and previous calculated results remain unchanged")
        screenshot("metric-management-deleted-draft.png")
        original_attachments = service.store.get("session", session["id"])["attachmentIds"]
        document = Document()
        document.add_paragraph("指标说明：按 operation_date 统计 surgery_event_id 唯一手术事件，缺失和非法值不计算，空集为0。")
        word = folder / "库指标说明.docx"
        document.save(word)
        window.bridge.import_paths([str(word)])
        assert run_once(service)
        imported = [a for a in service.store.list("attachment") if a["name"] == word.name]
        assert imported and imported[0]["scope"] == "library"
        assert service.store.get("session", session["id"])["attachmentIds"] == original_attachments
        assert len(service.store.list("session")) == 1
        checks.append("library Word import creates no conversation and changes no locked session")
        assert not window.page.javascript_errors, window.page.javascript_errors
        checks.append("no JavaScript runtime errors")
        return {"count": len(checks), "passed": checks, "values": ["3", "20.0"], "modelCalls": 0}
    finally:
        for job in service.store.jobs():
            if job["status"] == "QUEUED":
                service.cancel(job["id"])
        window.close()
        deadline = time.monotonic() + 15
        while (not window.ready_close or window.isVisible()) and time.monotonic() < deadline:
            QTest.qWait(100)
        assert window.ready_close and not window.isVisible()
        QApplication.instance().processEvents()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    output = ROOT / "artifacts/acceptance"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="DataMind-metric-management-UI-") as folder:
        result = verify(Path(folder), output)
    (output / "metric-management-ui-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    app.processEvents()


if __name__ == "__main__":
    main()
