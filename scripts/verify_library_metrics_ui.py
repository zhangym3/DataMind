"""Verify indicator-first browsing, version filters and live refresh in Qt."""
# ruff: noqa: E402 -- Set the Qt environment and project import path first.
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
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from insight_agent.desktop import InsightWindow
from insight_agent.worker import run_once


def verify(folder, output):
    window = InsightWindow(folder / "home", start_worker=False)
    window.resize(1440, 1000)
    window.show()
    service, checks = window.service, []

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
        window.web.grab().save(str(output / "library-metrics-failure.png"))
        raise AssertionError(name + " / " + str(window.page.javascript_errors) + " / " + str(js("({toast:document.querySelector('#toast')?.textContent,count:document.querySelector('#libraryMetricCount')?.textContent,rows:document.querySelectorAll('#libraryMetrics tbody tr').length,body:document.querySelector('#libraryMetrics')?.textContent.slice(0,700)})")))

    def click(selector):
        check("button exists: " + selector, f"!!document.querySelector({json.dumps(selector)})")
        js(f"document.querySelector({json.dumps(selector)}).click()")

    def set_input(key, value, event="input"):
        js(f"(()=>{{const el=document.getElementById({json.dumps(key)});el.value={json.dumps(value)};el.dispatchEvent(new Event({json.dumps(event)}));return true;}})()")

    def choose(version):
        set_input("libraryVersionFilter", version, "change")

    def shot(name):
        js("document.querySelector('#libraryView').scrollTop=0")
        window.resize(window.width() + 1, window.height())
        QTest.qWait(700)
        window.resize(window.width() - 1, window.height())
        QTest.qWait(400)
        assert window.web.grab().save(str(output / name))

    def save(name, draft=None, metric_id=None, description=None):
        spec = {"name": name, "description": description or f"合成统计口径：{name}，按唯一手术事件编号计数。", "actor": "合成维护人",
                "domain": "surgery", "entity": "surgery_event", "method": "count_distinct",
                "dateField": "operation_date", "keyField": "surgery_event_id", "unit": "case", "scale": 0}
        return service.dispatch("definition.manual.save", {"spec": spec, **(
            {"definition_id": draft["id"], "revision": draft["revision"], "metric_id": metric_id} if draft else {"name": name + "指标集"})})

    def publish(draft):
        return service.dispatch("definition.publish", {"definition_id": draft["id"], "revision": draft["revision"], "actor": "合成审核人", "reviewed": True})

    table = "document.querySelector('#libraryMetrics')"
    rows = "document.querySelectorAll('#libraryMetrics tbody tr')"
    names = "[...document.querySelectorAll('#libraryMetrics .metric-name-cell>strong')].map(el=>el.textContent)"
    try:
        check("desktop bootstrap is ready", "document.querySelector('#readiness')?.textContent!=='正在连接本机服务' && !!document.querySelector('#navLibrary')")
        click("#navLibrary")
        check("empty library offers imports and manual indicators", table + ".textContent.includes('暂无指标') && document.querySelector('#libraryVersionFilter').value==='latest'")
        v1 = publish(save("旧版专有名称"))
        v2 = service.dispatch("definition.clone", {"definition_id": v1["id"], "revision": v1["revision"]})
        metric_id = v2["candidates"][0]["id"]
        v2 = save("手术事件总量", v2, metric_id, "以唯一手术事件为统计对象，重复编号仅纳入一次；覆盖全部材料。")
        for i in range(1, 27):
            v2 = save(f"合成统计项目 {i:02d}", v2)
        other = save("另一个指标集的项目")
        with service.store.transaction() as db:
            hidden = other | {"id": "private-ui", "sessionId": "private-session", "name": "私有说明不公开"}
            service.store.put("definition", hidden, db)
            service.store.put("definition", other | {"id": "deleted-ui", "name": "已删除说明不公开", "deletedAt": "2026-10-30T00:00:00Z"}, db)
        window.bridge.changed.emit()
        check("latest combines only newest versions of each shared family", "document.querySelector('#libraryMetricCount').textContent==='共 28 项' && " + rows + ".length===25 && !" + names + ".includes('旧版专有名称')")
        check("version choices exclude private and deleted snapshots", "document.querySelector('#libraryVersionFilter').options.length===4 && !document.querySelector('#libraryView').textContent.includes('不公开')")
        check("indicator table precedes secondary collapsed version controls", "document.querySelector('#libraryMetrics').getBoundingClientRect().top<document.querySelector('#libraryVersionsPanel').getBoundingClientRect().top && !document.querySelector('#libraryVersionsPanel').open")
        check("latest summary does not duplicate a version list", "document.querySelector('#librarySelection').textContent.includes('2 个指标集') && document.querySelectorAll('#librarySelection button').length===0")
        shot("library-metrics-latest.png")
        click('[data-action="libMetricNext"]')
        check("pagination exposes all remaining indicators", rows + ".length===3 && document.querySelector('#libraryMetricPagination').textContent.includes('第 26–28 项')")
        set_input("librarySearch", "重复编号")
        check("search covers full descriptions and resets pagination", rows + ".length===1 && " + table + ".textContent.includes('手术事件总量') && document.querySelector('#libraryMetricPagination').textContent.includes('第 1–1 项')")
        click(f'#libraryMetrics [data-action="libMetricShow"][data-id="{metric_id}"]')
        check("indicator details show correct version and source evidence", "document.querySelector('#dialogTitle').textContent==='手术事件总量' && document.querySelector('#dialogBody').textContent.includes('v2') && document.querySelector('#dialogBody').textContent.includes('重复编号') && document.querySelector('#dialogBody .source-node')!==null")
        click('#dialogFooter [data-action="libEditMetric"]')
        check("draft indicator can be edited directly from its details", "document.querySelector('#manual_name')?.value==='手术事件总量'")
        set_input("manual_description", "已修订统计口径：按唯一手术事件计数，排除重复编号。")
        click('[data-action="libSaveManual"]')
        check("saved edits refresh main list without page reload", table + ".textContent.includes('已修订统计口径') && !!document.querySelector('#libVersion')")
        click("#closeModal")
        set_input("librarySearch", "不存在的项目")
        check("empty search result is explicit", table + ".textContent.includes('没有匹配的指标') && document.querySelector('#libraryMetricCount').textContent.includes('共 0 项')")
        set_input("librarySearch", "")
        check("clearing search restores all selected metrics", "document.querySelector('#libraryMetricCount').textContent==='共 28 项'")
        choose(v1["id"])
        check("historical filter reads only the chosen snapshot", rows + ".length===1 && " + table + ".textContent.includes('旧版专有名称') && !" + table + ".textContent.includes('手术事件总量') && !" + table + ".textContent.includes('另一个指标集')")
        shot("library-metrics-historical.png")
        choose("latest")
        check("switching back restores latest", "document.querySelector('#libraryMetricCount').textContent==='共 28 项'")
        # Both changes happen before either WebChannel reply can arrive.
        js(f"(()=>{{const el=document.querySelector('#libraryVersionFilter');el.value={json.dumps(v1['id'])};el.dispatchEvent(new Event('change'));el.value='latest';el.dispatchEvent(new Event('change'));return true;}})()")
        check("fast history-to-latest switch never shows stale response", "document.querySelector('#libraryMetrics').getAttribute('aria-busy')==='false' && document.querySelector('#libraryVersionFilter').value==='latest' && document.querySelector('#libraryMetricCount').textContent==='共 28 项' && !" + names + ".includes('旧版专有名称')")
        choose(v2["id"])
        check("specific new version excludes other metric families", "document.querySelector('#libraryMetricCount').textContent==='共 27 项' && !" + table + ".textContent.includes('另一个指标集')")
        click('#librarySelection [data-action="libShow"]')
        click('[data-action="libPublish"]')
        js("document.querySelector('#libReviewed').checked=true")
        click('[data-action="libPublishConfirm"]')
        check("publishing refreshes version and metric status", "document.querySelector('#dialogBody').textContent.includes('已发布') && !document.querySelector('#librarySelection').textContent.includes('草稿')")
        click('[data-action="libClone"]')
        check("creating a new version refreshes filter choices", "document.querySelector('#libVersion')?.value==='v3' && document.querySelector('#libraryVersionFilter').options.length===5")
        v3 = next(d for d in service.store.list("definition") if d.get("versionLabel") == "v3")
        click("#closeModal")
        choose(v3["id"])
        check("newly cloned draft is independently selectable", "document.querySelector('#librarySelection').textContent.includes('v3') && document.querySelector('#libraryMetricCount').textContent==='共 27 项'")
        click('#librarySelection [data-action="libShow"]')
        click('[data-action="libDelete"]')
        set_input("libDeleteText", "确认删除")
        click('[data-action="libDeleteConfirm"]')
        check("deleting selected version returns safely to latest", "document.querySelector('#libraryVersionFilter').value==='latest' && document.querySelector('#libraryMetricCount').textContent==='共 28 项' && !document.querySelector('#overlay').classList.contains('show')")
        document = Document()
        document.add_paragraph("统计口径：手术事件总手术量，单位台。")
        document.add_paragraph("分母：本次全部手术事件。")
        document.add_paragraph("总手术量X台。")
        document.add_paragraph("总手术量X台。")
        path = folder / "合成新导入指标说明.docx"
        document.save(path)
        window.bridge.import_paths([str(path)])
        check("new import expands progress without hiding metric list", "document.querySelector('#libraryProgressPanel').open && document.querySelector('#libraryMetricCount').textContent==='共 28 项'")
        assert run_once(service)
        imported_job = next(j for j in service.store.jobs() if j["kind"] == "import")
        assert imported_job["status"] == "SUCCEEDED" and not imported_job["result"].get("failures"), imported_job
        window.bridge.changed.emit()
        imported = next(d for d in service.store.list("definition") if d["name"] == path.name)
        check("imported version is offered before parsing finishes", f"!!document.querySelector('#libraryVersionFilter option[value=\"{imported['id']}\"]')")
        choose(imported["id"])
        check("unparsed version does not substitute historical indicators", rows + ".length===0 && " + table + ".textContent.includes('指标正在解析')")

        class Gateway:
            def call(self, stage, payload, schema, budget_id):
                return {"items": [{"slot": s["id"], "name": "总手术量", "method": "SURGERY_COUNT", "room": "", "start": "", "end": "", "minutes": 0, "reason": ""} for s in payload["slots"]]}

        assert run_once(service, Gateway())
        window.bridge.changed.emit()
        check("finished parsing automatically fills metrics of selected version", rows + ".length===2 && document.querySelector('#libraryMetricCount').textContent==='共 2 项' && " + table + ".textContent.includes('分析时核验')")
        check("same-name placeholders remain distinct rows", "new Set([..." + rows + "].map(el=>el.dataset.metricId)).size===2")
        choose("latest")
        check("latest now includes newly parsed indicators", "document.querySelector('#libraryMetricCount').textContent==='共 30 项'")
        window.resize(960, 820)
        QTest.qWait(500)
        check("narrow layout contains the table in horizontal scroll", "document.querySelector('.library-metric-table').scrollWidth>document.querySelector('.library-metric-table').clientWidth && document.querySelector('#libraryView').scrollWidth<=document.querySelector('#libraryView').clientWidth+1")
        shot("library-metrics-narrow.png")
        window.resize(1440, 1000)
        loaded = []
        window.web.loadFinished.connect(loaded.append)
        window.web.reload()
        deadline = time.monotonic() + 15
        while not loaded and time.monotonic() < deadline:
            QTest.qWait(50)
        assert loaded and loaded[-1]
        check("reloaded desktop reconnects", "!!document.querySelector('#readiness') && document.querySelector('#readiness').textContent!=='正在连接本机服务'")
        click("#navLibrary")
        check("reopened library defaults to all latest metrics", "document.querySelector('#libraryVersionFilter').value==='latest' && document.querySelector('#libraryMetricCount').textContent==='共 30 项'")

        # Published editing creates a draft only when Save succeeds, and the
        # explicitly selected version follows that draft.
        choose(v2["id"])
        check("published version is ready for maintenance", "document.querySelector('#libraryMetricCount').textContent==='共 27 项'")
        old = service.managed_definition(v2["id"])
        click(f'#libraryMetrics [data-action="libMetricShow"][data-id="{metric_id}"]')
        click('#dialogFooter [data-action="libEditMetric"]')
        check("published manual metric opens editable form", "document.querySelector('#manual_name')?.value==='手术事件总量'")
        set_input("manual_name", "手术事件总量修订")
        click('[data-action="libSaveManual"]')
        check("published edit follows its new draft in version filter", f"document.querySelector('#libraryVersionFilter').value!=={json.dumps(v2['id'])} && document.querySelector('#libraryVersionFilter').value!=='latest' && {table}.textContent.includes('手术事件总量修订') && !!document.querySelector('#libVersion')")
        edited_id = js("document.querySelector('#libraryVersionFilter').value")
        assert service.managed_definition(v2["id"]) == old
        assert service.managed_definition(edited_id)["status"] == "DRAFT"
        click("#closeModal")
        choose("latest")
        check("edited draft preserves overall count", "document.querySelector('#libraryMetricCount').textContent==='共 30 项'")

        # Selections span pages and searches, while version changes reset them.
        box = '#libraryMetrics tbody [data-action="libSelectMetric"]'
        click(box)
        check("partial page selection marks header indeterminate", "document.querySelector('[data-action=libSelectPage]').indeterminate && document.querySelector('#libraryBulkActions').textContent.includes('1 项')")
        click('[data-action="libMetricNext"]')
        click(box)
        check("selection spans pages", "document.querySelector('#libraryBulkActions').textContent.includes('2 项')")
        click('[data-action="libSelectPage"]')
        check("select all affects only current page", "document.querySelector('[data-action=libSelectPage]').checked && document.querySelector('#libraryBulkActions').textContent.includes('6 项')")
        click(box)
        check("unchecking one row updates bulk count and indeterminate", "document.querySelector('[data-action=libSelectPage]').indeterminate && document.querySelector('#libraryBulkActions').textContent.includes('5 项')")
        set_input("librarySearch", "没有匹配的临时搜索")
        check("search preserves selections outside its results", f"{rows}.length===0 && document.querySelector('#libraryBulkActions').textContent.includes('5 项')")
        choose(v2["id"])
        check("changing version clears prior selections", "document.querySelector('#libraryBulkActions').hidden || !document.querySelector('#libraryBulkActions').textContent.includes('5 项')")
        set_input("librarySearch", "")
        choose("latest")
        check("latest is ready for cross-family deletion", "document.querySelector('#libraryMetricCount').textContent==='共 30 项'")
        set_input("librarySearch", "手术事件总量修订")
        click(box)
        set_input("librarySearch", "另一个指标集的项目")
        click(box)
        check("two different metric families can be selected", "document.querySelector('#libraryBulkActions').textContent.includes('2 项')")
        before = service.store.list("definition")
        click('[data-action="libBulkDelete"]')
        check("bulk confirmation names all selected metrics", "document.querySelector('#dialogBody').textContent.includes('手术事件总量修订') && document.querySelector('#dialogBody').textContent.includes('另一个指标集的项目') && !document.querySelector('#dialogBody').textContent.includes('删除指标说明 / 版本')")
        click('[data-action="libCancelMetricDelete"]')
        assert service.store.list("definition") == before
        check("cancel leaves the selection available", "!document.querySelector('#overlay').classList.contains('show') && document.querySelector('#libraryBulkActions').textContent.includes('2 项')")
        click('[data-action="libBulkDelete"]')
        set_input("libDeleteMetricsText", "错误文字")
        click('[data-action="libDeleteMetricsConfirm"]')
        check("wrong confirmation is rejected without closing dialog", "document.querySelector('#toast').textContent.includes('请输入') && !!document.querySelector('#libDeleteMetricsText')")
        assert service.store.list("definition") == before
        set_input("libDeleteMetricsText", "确认删除")
        click('[data-action="libDeleteMetricsConfirm"]')
        check("successful bulk deletion closes confirmation", "!document.querySelector('#overlay').classList.contains('show') && document.querySelector('#toast').textContent.includes('已删除 2 项指标')")
        set_input("librarySearch", "")
        check("bulk deletion removes both metrics from latest", "document.querySelector('#libraryMetricCount').textContent==='共 28 项' && !document.querySelector('#libraryBulkActions').textContent.includes('2 项')")
        assert service.managed_definition(v2["id"]) == old
        assert len(service.managed_definition(edited_id)["metricExclusions"]) == 1
        choose(v2["id"])
        check("historical published indicators survive deletion from new draft", "document.querySelector('#libraryMetricCount').textContent==='共 27 项' && " + table + ".textContent.includes('手术事件总量')")

        # Original Word positions use the specialized plan editor.
        imported = service.managed_definition(imported["id"])
        imported = publish(imported)
        window.bridge.changed.emit()
        choose(imported["id"])
        check("published Word metrics are selectable", "document.querySelector('#libraryMetricCount').textContent==='共 2 项'")
        report_metric = imported["candidates"][0]["id"]
        click(f'#libraryMetrics [data-action="libMetricShow"][data-id="{report_metric}"]')
        click('#dialogFooter [data-action="libEditMetric"]')
        check("Word editor exposes existing plan", "document.querySelector('#metricEditMethod')?.value==='SURGERY_COUNT' && document.querySelector('#metricEditName')?.value==='总手术量'")
        set_input("metricEditName", "已维护总手术量")
        set_input("metricEditDescription", "合成维护说明：按原文总手术量取数。")
        click('[data-action="libSaveMetric"]')
        check("Word plan edits create a draft and refresh indicator list", f"document.querySelector('#libraryVersionFilter').value!=={json.dumps(imported['id'])} && {table}.textContent.includes('已维护总手术量') && !!document.querySelector('#libVersion')")
        report_draft_id = js("document.querySelector('#libraryVersionFilter').value")
        report_draft = service.managed_definition(report_draft_id)
        assert report_draft["candidates"][0]["plan"] == report_draft["reportPlan"]["slots"][0]["plan"]
        click("#closeModal")

        # Deleting one item from a published version follows the new draft;
        # the published version and its immutable manifest stay unchanged.
        choose(imported["id"])
        check("original published Word version remains unchanged", table + ".textContent.includes('总手术量') && !" + table + ".textContent.includes('已维护总手术量')")
        click(f'#libraryMetrics [data-action="libMetricShow"][data-id="{report_metric}"]')
        click('[data-action="libMetricDelete"]')
        set_input("libDeleteMetricsText", "确认删除")
        click('[data-action="libDeleteMetricsConfirm"]')
        check("published deletion follows a new draft with remaining metrics", f"document.querySelector('#libraryVersionFilter').value!=={json.dumps(imported['id'])} && document.querySelector('#libraryVersionFilter').value!=='latest' && document.querySelector('#libraryMetricCount').textContent==='共 1 项'")
        assert service.managed_definition(imported["id"]) == imported

        # A description is still removed individually, including published ones.
        choose(imported["id"])
        check("published description can be selected for removal", "document.querySelector('#libraryMetricCount').textContent==='共 2 项'")
        click('#librarySelection [data-action="libShow"]')
        click('[data-action="libDelete"]')
        set_input("libDeleteText", "确认删除")
        click('[data-action="libDeleteConfirm"]')
        check("published description is removed from version choices", f"!document.querySelector('#libraryVersionFilter option[value=\"{imported['id']}\"]') && !document.querySelector('#overlay').classList.contains('show')")
        assert service.store.get("definition", imported["id"])["deletedAt"]
        check("latest contains remaining indicators after all maintenance", "document.querySelector('#libraryVersionFilter').value==='latest' && document.querySelector('#libraryMetricCount').textContent==='共 27 项'")
        shot("library-metrics-maintained.png")
        assert not service.store.list("model_call") and not service.store.list("session")
        assert not window.page.javascript_errors, window.page.javascript_errors
        checks.append("zero online calls, no data conversation changes and no JavaScript errors")
        return {"count": len(checks), "passed": checks, "modelCalls": 0}
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
    with tempfile.TemporaryDirectory(prefix="DataMind-library-metrics-UI-") as folder:
        result = verify(Path(folder), output)
    (output / "library-metrics-ui-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    app.processEvents()


if __name__ == "__main__":
    main()
