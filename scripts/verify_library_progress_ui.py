"""Verify live library parsing progress through the real Qt WebChannel."""
# ruff: noqa: E402 -- Qt environment and project path must precede imports.
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docx import Document
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from insight_agent.core import AppError
from insight_agent.desktop import InsightWindow
from insight_agent.worker import run_once


def verify(folder, output):
    window = InsightWindow(folder / "home", start_worker=False)
    window.resize(1440, 1000)
    window.show()
    service = window.service
    checks = []
    release = threading.Event()
    entered = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

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
        raise AssertionError(name + " / " + str(window.page.javascript_errors) + " / " + str(js("document.querySelector('#toast')?.textContent")))

    def click(selector):
        check("button exists: " + selector, f"!!document.querySelector({json.dumps(selector)})")
        js(f"document.querySelector({json.dumps(selector)}).click()")

    def screenshot(name):
        window.resize(window.width() + 1, window.height())
        QTest.qWait(700)
        window.resize(window.width() - 1, window.height())
        QTest.qWait(400)
        assert window.web.grab().save(str(output / name))

    class Gateway:
        def __init__(self, fail=False):
            self.calls, self.fail = 0, fail

        def call(self, stage, payload, schema, budget_id):
            self.calls += 1
            if self.calls == (2 if self.fail else 1):
                entered.set()
                if not release.wait(45):
                    raise RuntimeError("Synthetic UI checkpoint timed out")
                if self.fail:
                    raise AppError("MODEL_UNAVAILABLE", "合成服务不可用，请检查模型设置后重试")
            return {"items": [{"slot": s["id"], "name": "总手术量", "method": "SURGERY_COUNT", "room": "", "start": "", "end": "", "minutes": 0, "reason": ""} for s in payload["slots"]]}

    try:
        check("desktop bootstrap loads", "!!document.querySelector('#readiness') && document.querySelector('#readiness').textContent!=='正在连接本机服务'")
        click("#navLibrary")
        check("library view opens", "!document.querySelector('#libraryView').hidden")
        document = Document()
        document.add_paragraph("统计口径：手术事件总手术量，单位台。")
        document.add_paragraph("分母：本次全部手术事件。")
        for _ in range(6):
            document.add_paragraph("总手术量X台。")
        path = folder / "合成进度指标说明.docx"
        document.save(path)
        window.bridge.import_paths([str(path)])
        check("queued upload shows file name before worker starts", "document.querySelector('#libraryStatus').textContent.includes('合成进度指标说明.docx') && document.querySelector('#libraryStatus').textContent.includes('0/1 个文件已结束')")
        assert run_once(service)
        window.bridge.changed.emit()
        definition = service.store.list("definition")[0]
        job_id = next(j["id"] for j in service.store.jobs() if j["kind"] == "extract")
        selector = f'[data-library-job="{job_id}"]'
        card = f"document.querySelector({json.dumps(selector)})?"
        check("read and routing events are available on page", "document.querySelector('#libraryStatus').textContent.includes('文件处理完成') && !!document.querySelector('#libraryStatus .activity-detail')")
        check("queued parsing exposes document node count", card + ".textContent.includes('原文 8 个节点') && " + card + ".textContent.includes('已排队，等待开始')")
        future = executor.submit(run_once, service, Gateway(fail=True))
        deadline = time.monotonic() + 15
        while not entered.is_set() and time.monotonic() < deadline:
            QTest.qWait(50)
        assert entered.is_set()
        check("model wait shows actual completed batch count", card + ".textContent.includes('已完成 4/6 个数字待填位置') && " + card + ".textContent.includes('正在等待模型返回本批结果')")
        check("completed metric names and current source positions are visible", card + ".textContent.includes('总手术量') && " + card + ".textContent.includes('原文位置 word/document.xml')")
        check("numeric progress reflects saved 4 of 6", card + ".querySelector('[role=progressbar]').getAttribute('aria-valuenow')==='66'")
        clock = js(card + ".querySelector('[data-library-started]').textContent")
        QTest.qWait(1500)
        check("elapsed clock advances while model waits", card + f".querySelector('[data-library-started]').textContent!=={json.dumps(clock)}")
        click(f'#versions [data-action="libShow"][data-id="{definition["id"]}"]')
        check("maintenance modal also shows live batch progress", "document.querySelector('[data-library-definition-progress]').textContent.includes('4/6')")
        js("document.querySelector('#libNotes').value='未保存的版本说明'")
        window.bridge.changed.emit()
        QTest.qWait(1400)
        check("progress refresh preserves editable form values", "document.querySelector('#libNotes').value==='未保存的版本说明'")
        click("#closeModal")
        screenshot("library-progress-running.png")
        release.set()
        assert future.result(timeout=10)
        window.bridge.changed.emit()
        check("failed attempt retains last completed count and error", card + ".textContent.includes('MODEL_UNAVAILABLE') && " + card + ".textContent.includes('已完成 4/6') && !!" + card + ".querySelector('[data-action=libRetry]')")
        screenshot("library-progress-failed.png")
        click(selector + ' [data-action="libRetry"]')
        release.clear()
        entered.clear()
        retry_id = next(j["id"] for j in service.store.jobs() if j["kind"] == "extract" and j["status"] == "QUEUED")
        retry_card = f"document.querySelector('[data-library-job=\"{retry_id}\"]')?"
        future = executor.submit(run_once, service, Gateway())
        deadline = time.monotonic() + 15
        while not entered.is_set() and time.monotonic() < deadline:
            QTest.qWait(50)
        assert entered.is_set()
        check("retry reuses saved batches and replaces old failure", retry_card + ".textContent.includes('4/6') && " + retry_card + ".textContent.includes('已恢复上次成功批次') && !document.querySelector('#libraryStatus').textContent.includes('MODEL_UNAVAILABLE')")
        release.set()
        assert future.result(timeout=10)
        window.bridge.changed.emit()
        check("completed parsing shows all slots and review action", retry_card + ".textContent.includes('已保存 6 项') && " + retry_card + ".textContent.includes('解析完成，待复核发布') && " + retry_card + ".querySelector('[role=progressbar]').getAttribute('aria-valuenow')==='100'")
        check("completed stages are all marked done", retry_card + ".querySelectorAll('.library-progress-stages .done').length===5")
        click(f'[data-library-job="{retry_id}"] [data-action="libShow"]')
        check("completed parsed candidates can be reviewed", "document.querySelector('#dialogBody').textContent.includes('指标（6 项）') && !document.querySelector('[data-action=libPublish]').disabled")
        click("#closeModal")
        check("full progress history is expandable", retry_card + ".querySelector('details[data-library-log]')!==null")
        js(f"document.querySelector('[data-library-job=\"{retry_id}\"] details[data-library-log]').open=true")
        window.bridge.changed.emit()
        QTest.qWait(1400)
        check("polling preserves expanded history", retry_card + ".querySelector('details[data-library-log]').open")
        screenshot("library-progress-complete.png")
        loaded = []
        window.web.loadFinished.connect(loaded.append)
        window.web.reload()
        deadline = time.monotonic() + 15
        while not loaded and time.monotonic() < deadline:
            QTest.qWait(50)
        assert loaded and loaded[-1]
        check("page reload completes", "!!document.querySelector('#readiness') && document.querySelector('#readiness').textContent!=='正在连接本机服务' && !!document.querySelector('#navLibrary')")
        click("#navLibrary")
        check("reopening page restores durable progress", retry_card + ".textContent.includes('解析完成，待复核发布')")
        bad = folder / "损坏的指标说明.docx"
        bad.write_bytes(b"invalid office file")
        window.bridge.import_paths([str(bad)])
        assert run_once(service)
        window.bridge.changed.emit()
        check("import errors show file-specific details", "document.querySelector('#libraryStatus').textContent.includes('损坏的指标说明.docx') && document.querySelector('#libraryStatus').textContent.includes('1 个文件失败') && document.querySelector('#libraryStatus .job-failure')!==null")
        assert not service.store.list("session") and not service.store.list("run")
        assert not window.page.javascript_errors, window.page.javascript_errors
        checks.append("no conversation, computation, online model call or JavaScript error")
        return {"count": len(checks), "passed": checks}
    finally:
        release.set()
        executor.shutdown(wait=True)
        for job in service.store.jobs():
            if job["status"] == "QUEUED":
                service.cancel(job["id"])
        window.close()
        deadline = time.monotonic() + 15
        while (not window.ready_close or window.isVisible()) and time.monotonic() < deadline:
            QTest.qWait(100)
        QApplication.instance().processEvents()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    output = ROOT / "artifacts/acceptance"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="DataMind-library-progress-UI-") as folder:
        result = verify(Path(folder), output)
    (output / "library-progress-ui-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    app.processEvents()


if __name__ == "__main__":
    main()
