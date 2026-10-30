"""Windows desktop host; secrets and filesystem paths stay on the native side."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QLockFile, QObject, QSettings, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox

from .core import AppError, dumps, require, uid
from .models import cleanup_temporary_credentials
from .service import AppService
from .worker import worker_main

WEB_DIR = Path(__file__).resolve().parent / "web"


class LocalRequests(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        allowed = url.scheme() in ("qrc", "data", "about")
        if url.isLocalFile():
            allowed = Path(url.toLocalFile()).resolve().is_relative_to(WEB_DIR)
        info.block(not allowed)


class LocalPage(QWebEnginePage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.javascript_errors = []

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.scheme() == "about" or (url.isLocalFile() and Path(url.toLocalFile()).resolve() == WEB_DIR / "index.html")

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.javascript_errors.append(message)


class DesktopBridge(QObject):
    changed = Signal()
    notice = Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.service = window.service
        self.session_id = None
        self.library_context = False
        self.allow_paste = False

    def inform(self, message, code="INFO"):
        self.notice.emit(dumps({"code": code, "message": message}))
        self.changed.emit()

    @Slot(str, str, result=str)
    def request(self, method, encoded):
        try:
            require(len(encoded) <= 3 * 1024**2, "REQUEST_LIMIT", "请求过大")
            args = json.loads(encoded)
            require(isinstance(args, dict), "INVALID_REQUEST", "请求格式无效")
            result = self.service.dispatch(method, args)
            return dumps({"ok": True, "data": result})
        except AppError as exc:
            return dumps({"ok": False, "error": exc.as_dict()})
        except (ValueError, TypeError, KeyError):
            return dumps({"ok": False, "error": {"code": "INVALID_REQUEST", "message": "请求字段无效，请检查输入后重试"}})
        except Exception:
            return dumps({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "本机服务异常，请导出诊断记录"}})

    @Slot(str)
    def setContext(self, session_id):
        self.library_context = session_id == "library"
        self.session_id = None if self.library_context else session_id or None

    @Slot(bool)
    def allowFilePaste(self, enabled):
        self.allow_paste = enabled

    @Slot()
    def pickFiles(self):
        formats = "*.xlsx *.xls *.docx *.doc"
        is_v2 = bool(self.session_id and self.service.store.get("session", self.session_id).get("schemaVersion") == 2)
        paths, _ = QFileDialog.getOpenFileNames(self.window, "导入手术数据" if is_v2 else "导入数据或指标说明", "", "Excel 工作簿 (*.xlsx)" if is_v2 else "Office 文件 (" + formats + ")")
        if paths:
            self.import_paths(paths)

    def import_paths(self, paths):
        try:
            target = self.session_id
            if not target and not self.library_context:
                target = self.service.create_session_v2()["id"]
                self.setContext(target)
                self.notice.emit(dumps({"code": "NEW_SESSION", "message": "已创建数据对话", "sessionId": target}))
            if target and self.service.store.get("session", target)["state"] == "LOCKED" and self.service.store.get("session", target).get("schemaVersion") != 2:
                QMessageBox.information(self.window, "无法导入文件", "当前会话已锁定，请先点击“新建数据对话”后再导入文件。")
                return
            self.service.import_paths(paths, target)
            self.inform("文件已开始处理，识别结果会显示在指标管理" if self.library_context else "文件已开始处理，识别结果会显示在当前对话")
        except AppError as exc:
            self.inform(exc.message, exc.code)

    @Slot(result=bool)
    def pasteFiles(self):
        mime = QApplication.clipboard().mimeData()
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()] if mime and mime.hasUrls() else []
        if not paths:
            return False
        self.import_paths(paths)
        return True

    @Slot(str)
    def copyText(self, text):
        QApplication.clipboard().setText(text)

    @Slot()
    def settings(self):
        from .native_settings import SettingsDialog
        SettingsDialog(self.window).exec()
        self.changed.emit()

    def artifact_file(self, artifact_id, rendered=False):
        artifact = self.service.store.get("artifact", artifact_id)
        hash = artifact.get("renderHash") if rendered else artifact["sha256"]
        require(hash, "NOT_FOUND", "该产物没有渲染预览")
        extension = "pdf" if rendered else artifact["format"]
        path = self.service.store.home / "open" / (artifact_id + "." + extension)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(self.service.store.read_blob(hash))
        return path

    @Slot(str, bool)
    def openArtifact(self, artifact_id, rendered=False):
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.artifact_file(artifact_id, rendered))))
        except AppError as exc:
            self.inform(exc.message, exc.code)

    @Slot(str)
    def saveArtifact(self, artifact_id):
        try:
            source = self.artifact_file(artifact_id)
            path, _ = QFileDialog.getSaveFileName(self.window, "另存为", source.name, "导出文件 (*." + source.suffix[1:] + ")")
            if path:
                shutil.copyfile(source, path)
                self.inform("已保存导出文件")
        except (AppError, OSError) as exc:
            self.inform(getattr(exc, "message", "文件无法保存，请选择其他位置"), getattr(exc, "code", "SAVE_FAILED"))

    @Slot(str)
    def saveComparison(self, comparison_id):
        try:
            value = self.service.store.get("comparison", comparison_id)
            require(value["sessionId"] == self.session_id, "SCOPE_FORBIDDEN", "比较不属于当前会话")
            path, _ = QFileDialog.getSaveFileName(self.window, "导出比较汇总", comparison_id + ".json", "JSON (*.json)")
            if path:
                from .comparisons import export_comparison
                Path(path).write_text(dumps(export_comparison(value)), encoding="utf-8")
                self.inform("比较汇总已保存")
        except (AppError, OSError) as exc:
            self.inform(getattr(exc, "message", "无法保存比较汇总"), getattr(exc, "code", "SAVE_FAILED"))

    @Slot(str)
    def openOriginal(self, attachment_id):
        try:
            attachment = self.service.store.get("attachment", attachment_id)
            require(attachment["scope"] == (self.session_id or "library"), "SCOPE_FORBIDDEN", "文件不属于当前范围")
            if QMessageBox.question(self.window, "打开托管原件", "将使用系统关联应用打开本地副本，请勿启用宏或外部链接。是否继续？") == QMessageBox.StandardButton.Yes:
                extension = Path(attachment["name"]).suffix
                path = self.service.store.home / "open" / (uid() + extension)
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(self.service.store.read_blob(attachment["sha256"]))
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except AppError as exc:
            self.inform(exc.message, exc.code)


class NativeView(QWebEngineView):
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.window().bridge.import_paths(paths)
            event.acceptProposedAction()


class InsightWindow(QMainWindow):
    def __init__(self, home=None, start_worker=True):
        super().__init__()
        self.service = AppService(home)
        self.lock = QLockFile(str(self.service.store.home / "instance.lock"))
        require(self.lock.tryLock(0), "APP_ALREADY_RUNNING", "当前数据目录已在另一个窗口中打开")
        self.service.store.recover(self.service.settings()["autoResume"])
        cleanup_temporary_credentials(self.service.store)
        self.worker = None
        self.closing = False
        self.ready_close = False
        self.worker_failures = 0
        self.observed_jobs = {}
        self.enable_worker = start_worker
        self.stop_event = multiprocessing.Event()
        self.setWindowTitle("Insight Agent · 数据指标智能分析")
        self.setWindowIcon(QIcon(str(WEB_DIR / "icon.svg")))
        self.resize(1480, 940)
        self.setMinimumSize(1100, 700)
        self.web = NativeView(self)
        self.web.setZoomFactor(self.service.settings()["fontScale"] / 100)
        self.page = LocalPage(self.web)
        self.web.setPage(self.page)
        self.web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        settings = self.web.settings()
        for flag in (QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, QWebEngineSettings.WebAttribute.LocalStorageEnabled, QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows):
            settings.setAttribute(flag, False)
        self.interceptor = LocalRequests(self)
        self.page.profile().setUrlRequestInterceptor(self.interceptor)
        self.setCentralWidget(self.web)
        self.channel = QWebChannel(self.page)
        self.bridge = DesktopBridge(self)
        self.channel.registerObject("desktop", self.bridge)
        self.page.setWebChannel(self.channel)
        self.web.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))
        self.shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        self.shortcut.activated.connect(lambda: self.page.runJavaScript("window.appActions && window.appActions.newSession()"))
        QApplication.instance().installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1200)
        if self.enable_worker:
            self.start_worker()

    def start_worker(self):
        self.stop_event.clear()
        self.worker = multiprocessing.Process(target=worker_main, args=(str(self.service.store.home), self.stop_event), daemon=True)
        self.worker.start()

    def stop_worker(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(3)
            if self.worker.is_alive():
                self.worker.terminate()
                self.worker.join(2)
            self.worker = None

    def tick(self):
        if self.closing:
            if self.worker and self.worker.is_alive():
                committing = any(r.get("phase") == "COMMITTING" and not r.get("committed") for r in self.service.store.list("run"))
                if time.monotonic() - self.close_started > 10 and not committing:
                    self.worker.terminate()
                    self.worker.join(2)
                else:
                    return
            if self.worker:
                self.worker.join()
                self.worker = None
            self.service.store.recover(False)
            cleanup_temporary_credentials(self.service.store)
            self.ready_close = True
            QTimer.singleShot(0, self.close)
            return
        if self.enable_worker and self.worker and not self.worker.is_alive():
            self.worker.join()
            self.worker_failures += 1
            self.service.store.recover(self.service.settings()["autoResume"])
            cleanup_temporary_credentials(self.service.store)
            if self.worker_failures <= 3:
                self.start_worker()
                self.bridge.inform("后台处理已恢复，已提交结果保持完整", "WORKER_RECOVERED")
            else:
                self.worker = None
                self.bridge.inform("后台进程连续退出，已暂停自动重启。请导出诊断后重新启动应用。", "WORKER_UNAVAILABLE")
        if self.enable_worker and self.worker and self.worker.is_alive():
            active = [j for j in self.service.store.jobs() if j["status"] == "RUNNING"]
            live = {(j["id"], j["lease"]) for j in active}
            self.observed_jobs = {k: v for k, v in self.observed_jobs.items() if k in live}
            for job in active:
                key = (job["id"], job["lease"])
                started = self.observed_jobs.setdefault(key, time.monotonic())
                budget = self.service.settings()["runSeconds"]
                runs = [r for r in self.service.store.list("run") if r.get("jobId") == job["id"]]
                if runs:
                    budget = runs[0]["contract"]["limits"]["run_seconds"]
                if time.monotonic() - started > budget + 5:
                    self.stop_worker()
                    self.service.store.expire(job["id"], job["lease"])
                    cleanup_temporary_credentials(self.service.store)
                    self.start_worker()
                    self.bridge.inform("处理超时，后台已停止并恢复处理。", "RUN_TIMEOUT")
                    break
        self.bridge.changed.emit()

    def eventFilter(self, obj, event):
        # A file clipboard paste is consumed on key press. Suppress the
        # matching release so Chromium cannot replay it after the clipboard
        # owner changes to plain text.
        if event.type() == QEvent.Type.KeyRelease and event.matches(QKeySequence.StandardKey.Paste) and self.bridge.allow_paste and self.isActiveWindow():
            return True
        if event.type() == QEvent.Type.KeyPress and event.matches(QKeySequence.StandardKey.Paste) and self.bridge.allow_paste and self.isActiveWindow():
            self.page.runJavaScript("window.appActions && window.appActions.noteFilePaste()")
            pasted_files = self.bridge.pasteFiles()
            if pasted_files:
                return True
            self.page.runJavaScript("window.appActions && window.appActions.noteTextPasteShortcut()")
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        if self.ready_close:
            self.timer.stop()
            with self.service.store.transaction() as db:
                self.service.store.put("ui", {"id": "window", "width": self.width(), "height": self.height()}, db)
            QApplication.instance().removeEventFilter(self)
            self.lock.unlock()
            event.accept()
            return
        if self.closing:
            event.ignore()
            return
        active = [j for j in self.service.store.jobs() if j["status"] in ("QUEUED", "RUNNING")]
        if active and QMessageBox.question(self, "停止处理并退出", "存在未完成的处理。停止后退出？已提交结果和草稿保留。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        for job in active:
            self.service.cancel(job["id"])
        self.closing = True
        self.close_started = time.monotonic()
        self.stop_event.set()
        self.setWindowTitle("Insight Agent · 正在安全停止处理…")
        event.ignore()
        self.tick()


def run(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--smoke-output", help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args(argv[1:])
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("Insight Agent")
    app.setOrganizationName("DataMind")
    app.setStyle("Fusion")
    try:
        window = InsightWindow(args.data_dir or QSettings().value("dataDirectory") or None)
    except AppError as exc:
        QMessageBox.critical(None, exc.code, exc.message)
        return 1
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        prefs = window.service.store.list("ui")
        prefs = prefs[0] if prefs else {"width": 1480, "height": 940}
        window.resize(min(prefs["width"], available.width()), min(prefs["height"], available.height() - 40))
        window.move(available.center() - window.rect().center())
    window.show()
    if args.smoke_output:
        require(args.data_dir, "INVALID_REQUEST", "打包验收必须指定隔离数据目录")
        from .smoke import attach
        window.smoke_timer = attach(app, window, args.smoke_output)
    return app.exec()
