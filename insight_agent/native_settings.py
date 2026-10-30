"""Native settings: API secrets never enter QWebChannel or HTML."""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QDoubleSpinBox, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget)

from .core import AppError, LIMITS
from .maintenance import backup, diagnostics, verify_restore, migrate, cleanup_preview, cleanup_blobs, verify_references
from .models import Credentials, Gateway, PRESETS, save_profile, temporary_profile
from .core import uid
from .service import DEFAULT_SETTINGS


class _InlineCredentials:
    """Expose a form-only secret to one synchronous test request."""

    def __init__(self, secret):
        self.secret = secret

    def get(self, _reference):
        return self.secret


class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.service = window, window.service
        self.profile_id = None
        self.setWindowTitle("设置 · Insight Agent")
        font = self.font()
        font.setPointSizeF(max(11.0, font.pointSizeF()))
        self.setFont(font)
        self.resize(980, 720)
        self.setMinimumSize(760, 560)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        def add_tab(page, title):
            # Keep the larger form text accessible on shorter screens.
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            tabs.addTab(scroll, title)

        model = QWidget()
        model_layout = QVBoxLayout(model)
        intro = QLabel("配置多套在线模型连接，按需选择默认模型。连接测试只发送固定 JSON 样例并立即返回结果，不进入后台处理。")
        intro.setWordWrap(True)
        model_layout.addWidget(intro)
        row = QHBoxLayout()
        model_layout.addLayout(row, 1)

        left = QVBoxLayout()
        left_title = QHBoxLayout()
        left_title.addWidget(QLabel("连接配置"))
        self.profile_count = QLabel()
        left_title.addWidget(self.profile_count)
        left_title.addStretch()
        left.addLayout(left_title)
        self.profiles = QListWidget()
        self.profiles.setMinimumWidth(260)
        self.profiles.setMaximumWidth(300)
        self.profiles.setWordWrap(True)
        self.profiles.currentRowChanged.connect(self.select)
        left.addWidget(self.profiles, 1)
        new_button = QPushButton("＋ 新建连接")
        new_button.clicked.connect(lambda: self.guard(self.new))
        left.addWidget(new_button)
        hint = QLabel("每套连接独立保存服务地址、模型和 Key。启用且设为默认后，新的分析才会使用它。")
        hint.setWordWrap(True)
        left.addWidget(hint)
        row.addLayout(left)

        panel = QVBoxLayout()
        row.addLayout(panel, 1)
        basic = QGroupBox("连接信息")
        form = QFormLayout(basic)
        self.name = QLineEdit()
        self.provider = QComboBox()
        self.provider.setEditable(True)
        self.provider.addItems(list(PRESETS))
        self.provider.currentTextChanged.connect(self.preset)
        self.protocol = QComboBox()
        self.protocol.addItems(["chat", "responses", "anthropic"])
        self.base = QLineEdit()
        self.model = QLineEdit()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("已保存 Key 可留空复用；填写新 Key 后可直接测试但不会自动保存配置")
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setSingleStep(0.1)
        self.temperature.setDecimals(2)
        self.temperature.setValue(0)
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 120)
        self.timeout.setSuffix(" 秒")
        self.enabled, self.default = QCheckBox("启用此连接"), QCheckBox("设为默认模型")
        self.enabled.setChecked(True)
        self.default.setChecked(True)
        self.tokens = QSpinBox()
        self.tokens.setRange(256, LIMITS["output_tokens"])
        self.tokens.setValue(4096)
        self.tokens.setToolTip("应用上限为 1,000,000 Token；实际请求还受模型接口上限和剩余任务预算约束。")
        for label, field in [("配置名称", self.name), ("服务商", self.provider), ("接口协议", self.protocol), ("Base URL", self.base), ("模型名称", self.model)]:
            form.addRow(label, field)
        panel.addWidget(basic)

        auth = QGroupBox("凭据与调用参数")
        auth_form = QFormLayout(auth)
        auth_form.addRow("API Key", self.key)
        auth_form.addRow("最大输出 Token", self.tokens)
        auth_form.addRow("温度", self.temperature)
        auth_form.addRow("请求超时", self.timeout)
        key_hint = QLabel("Key 只保存在 Windows 凭据管理器；测试不会清空输入框，也不会删除已保存 Key。")
        key_hint.setWordWrap(True)
        auth_form.addRow("", key_hint)
        panel.addWidget(auth)

        behavior = QGroupBox("使用策略")
        behavior_form = QFormLayout(behavior)
        behavior_form.addRow("", self.enabled)
        behavior_form.addRow("", self.default)
        panel.addWidget(behavior)

        self.status = QLabel()
        self.status.setWordWrap(True)
        panel.addWidget(self.status)
        actions = QHBoxLayout()
        save_button = QPushButton("保存连接")
        save_button.clicked.connect(lambda: self.guard(self.save))
        self.test_button = QPushButton("立即测试连接")
        self.test_button.clicked.connect(lambda: self.guard(self.test))
        delete_key_button = QPushButton("删除 Key")
        delete_key_button.clicked.connect(lambda: self.guard(self.delete_key))
        delete_button = QPushButton("删除连接")
        delete_button.clicked.connect(lambda: self.guard(self.delete))
        for button in (save_button, self.test_button, delete_key_button, delete_button):
            actions.addWidget(button)
        panel.addLayout(actions)
        panel.addStretch()
        add_tab(model, "模型服务")
        general = QWidget()
        form = QFormLayout(general)
        settings = self.service.settings()
        self.scale = QComboBox()
        self.scale.addItems(["100", "125", "150"])
        self.scale.setCurrentText(str(settings["fontScale"]))
        self.title = QLineEdit(settings["reportTitle"])
        self.online = QCheckBox("允许对话与分析调用已配置的在线模型")
        self.online.setChecked(settings["modelEnabled"])
        self.resume = QCheckBox("重启后自动恢复未完成处理（受原预算约束）")
        self.resume.setChecked(settings["autoResume"])
        self.template = QComboBox()
        self.template.addItem("内置分析报告 v1.0", "builtin/1.0")
        self.seconds, self.calls, self.budget, self.retention = QSpinBox(), QSpinBox(), QSpinBox(), QSpinBox()
        for widget, key, low, high in [(self.seconds,"runSeconds",60,900),(self.calls,"modelCalls",1,30),(self.budget,"modelTokens",1000,LIMITS["model_tokens"]),(self.retention,"retentionDays",1,3650)]:
            widget.setRange(low, high)
            widget.setValue(settings[key])
        for label, widget in [("界面缩放 %", self.scale),("报告标题",self.title),("",self.online),("运行预算（秒）",self.seconds),("模型调用次数",self.calls),("累计 Token 预算",self.budget),("保留期（天，清理需显式确认）",self.retention)]:
            form.addRow(label, widget)
        save = QPushButton("保存一般设置")
        save.clicked.connect(lambda: self.guard(self.save_general))
        form.addRow(save)
        form.addRow("恢复策略", self.resume)
        form.addRow("报告模板", self.template)
        reset = QPushButton("恢复默认设置")
        reset.clicked.connect(lambda: self.guard(self.reset_general))
        form.addRow(reset)
        add_tab(general, "分析与界面")
        maintenance = QWidget()
        box = QVBoxLayout(maintenance)
        info = QLabel("本地数据目录：\n" + str(self.service.store.home) + "\n\n备份包含业务原件及历史，采用加密 7z；不包含模型 Key。恢复到新空目录，不覆盖当前工作空间。")
        info.setWordWrap(True)
        info.setTextInteractionFlags(info.textInteractionFlags() | __import__('PySide6.QtCore', fromlist=['Qt']).Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(info)
        for label, callback in [("创建加密备份", self.backup), ("校验并恢复备份到新目录", self.restore), ("迁移数据目录（保留原目录供回退）", self.migrate), ("切换到已验证目录 / 回退（下次启动）", self.switch_workspace), ("预览并清理无引用文件", self.cleanup), ("导出诊断（版本、状态、错误码，无业务内容）", self.diagnostics), ("复制数据目录启动命令", lambda: __import__('PySide6.QtWidgets',fromlist=['QApplication']).QApplication.clipboard().setText('InsightAgent.exe --data-dir "' + str(self.service.store.home) + '"'))]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, fn=callback: self.guard(fn))
            box.addWidget(button)
        box.addWidget(QLabel("Insight Agent 1.0.0 · Python 3.11 · Windows 10/11 x64\n运算在本地执行；在线模型仅接收各阶段允许的脱敏信息。\n连接测试不代表业务口径验收。缺少转换器时 DOCX 仅可导出未核验草稿。"))
        box.addStretch()
        add_tab(maintenance, "存储与维护")
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self.refresh()
        self.new()

    def guard(self, fn):
        try:
            fn()
        except AppError as exc:
            QMessageBox.warning(self, exc.code, exc.message)
        except Exception as exc:
            QMessageBox.warning(self, "操作失败", "请检查路径、凭据服务或文件占用后重试。错误类型：" + type(exc).__name__)

    def refresh(self):
        self.items = self.service.store.list("model")
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for profile in self.items:
            state = "已测试" if (profile.get("check") or {}).get("status") == "PASS" else "未测试"
            badges = [state]
            if profile.get("default"):
                badges.append("默认")
            if not profile.get("enabled"):
                badges.append("已停用")
            item = QListWidgetItem(f"{profile['name']}\n{profile.get('provider', '')} · {profile.get('model', '')}\n{' · '.join(badges)}")
            item.setToolTip(profile.get("baseUrl", ""))
            self.profiles.addItem(item)
        self.profile_count.setText(f"{len(self.items)} 套")
        selected = next((index for index, profile in enumerate(self.items) if profile["id"] == self.profile_id), -1)
        self.profiles.setCurrentRow(selected)
        self.profiles.blockSignals(False)

    def preset(self, name):
        value = PRESETS.get(name)
        if not value:
            return
        self.base.setText(value["baseUrl"])
        self.protocol.setCurrentText(value["protocol"])

    def new(self):
        self.profile_id = None
        self.name.setText("新模型配置")
        self.model.clear()
        self.key.clear()
        self.preset(self.provider.currentText())
        self.temperature.setValue(0)
        self.timeout.setValue(120)
        self.enabled.setChecked(True)
        self.default.setChecked(not self.items)
        self.profiles.blockSignals(True)
        self.profiles.setCurrentRow(-1)
        self.profiles.blockSignals(False)
        self.status.setText("新连接尚未保存。填写完整后可直接测试，不会删除 API Key。")

    def select(self, index):
        if index < 0 or index >= len(self.items):
            return
        p = self.items[index]
        self.profile_id = p["id"]
        self.provider.setCurrentText(p["provider"])
        for widget, key in [(self.name,"name"),(self.base,"baseUrl"),(self.model,"model")]:
            widget.setText(p[key])
        self.protocol.setCurrentText(p["protocol"])
        self.enabled.setChecked(p["enabled"])
        self.default.setChecked(p["default"])
        self.tokens.setValue(p["maxOutputTokens"])
        self.temperature.setValue(float(p.get("temperature", "0")))
        self.timeout.setValue(p.get("timeoutSeconds", 120))
        self.key.clear()
        check = p.get("check") or {}
        checked_at = f"（{check.get('at')}）" if check.get("at") else ""
        self.status.setText(("Key 已保存" if p.get("hasKey") else "尚未配置 Key") + "；连接状态：" + str(check.get("status", "未测试")) + checked_at)

    def save(self):
        data = self.form_data()
        if self.profile_id:
            data["id"] = self.profile_id
        result = save_profile(self.service.store, data, self.key.text() or None)
        self.profile_id = result["id"]
        self.refresh()
        self.status.setText("连接配置已保存。API Key 保留在凭据管理器；可立即测试，历史运行不会切换配置。")

    def test(self):
        data = self.form_data()
        existing = self.service.store.get("model", self.profile_id) if self.profile_id else None
        same_target = existing and all(existing.get(key) == data.get(key) for key in ("baseUrl", "provider", "protocol"))
        entered_secret = self.key.text().strip()
        if not entered_secret and existing and same_target:
            vault = self.service.credentials or Credentials()
            if vault.get(existing["credentialRef"]):
                self.status.setText("正在测试已保存连接，请稍候……")
                try:
                    result = Gateway(self.service.store, vault).test(profile_id=existing["id"])
                except AppError as exc:
                    self.status.setText(f"连接测试失败：{exc.message}")
                    raise
                self.refresh()
                self.status.setText("连接测试通过。配置和 API Key 均已保留。")
                return result
            raise AppError("MODEL_KEY_MISSING", "当前配置没有可用 Key，请填写 API Key 后测试")
        if not entered_secret:
            raise AppError("MODEL_KEY_MISSING", "当前配置没有可用 Key，请填写 API Key 后测试")
        self.status.setText("正在测试当前表单，请稍候……")
        profile = temporary_profile(data, uid("inline_test_"))
        try:
            result = Gateway(self.service.store, _InlineCredentials(entered_secret)).test(profile=profile)
        except AppError as exc:
            self.status.setText(f"连接测试失败：{exc.message}")
            raise
        self.status.setText("连接测试通过。当前表单可以继续修改或保存，API Key 未被清空或删除。")
        return result

    def form_data(self):
        return {"name":self.name.text().strip(),"provider":self.provider.currentText().strip(),"protocol":self.protocol.currentText(),"baseUrl":self.base.text().strip(),"model":self.model.text().strip(),"enabled":self.enabled.isChecked(),"default":self.default.isChecked(),"temperature":str(self.temperature.value()),"maxOutputTokens":self.tokens.value(),"timeoutSeconds":self.timeout.value()}

    def delete_key(self):
        if not self.profile_id:
            return
        p = self.service.store.get("model", self.profile_id)
        (self.service.credentials or Credentials()).delete(p["credentialRef"])
        p.update(hasKey=False, check=None)
        with self.service.store.transaction() as db:
            self.service.store.put("model",p,db)
        self.key.clear()
        self.refresh()
        self.status.setText("Key 已从凭据管理器删除。")

    def delete(self):
        if self.profile_id and QMessageBox.question(self, "删除配置", "确认删除配置及 Key？历史结果保留。") == QMessageBox.StandardButton.Yes:
            self.delete_key()
            with self.service.store.transaction() as db:
                db.execute("DELETE FROM objects WHERE kind='model' AND id=?", (self.profile_id,))
            self.refresh()
            self.new()

    def save_general(self):
        self.service.save_settings({"fontScale":int(self.scale.currentText()),"reportTitle":self.title.text(),"modelEnabled":self.online.isChecked(),"autoResume":self.resume.isChecked(),"template":self.template.currentData(),"runSeconds":self.seconds.value(),"modelCalls":self.calls.value(),"modelTokens":self.budget.value(),"retentionDays":self.retention.value()})
        self.window.web.setZoomFactor(int(self.scale.currentText()) / 100)
        QMessageBox.information(self, "已保存", "一般设置已保存。分析与预算设置用于后续处理。")

    def reset_general(self):
        self.service.save_settings(DEFAULT_SETTINGS)
        self.scale.setCurrentText(str(DEFAULT_SETTINGS["fontScale"]))
        self.title.setText(DEFAULT_SETTINGS["reportTitle"])
        self.online.setChecked(DEFAULT_SETTINGS["modelEnabled"])
        self.resume.setChecked(DEFAULT_SETTINGS["autoResume"])
        for widget, key in ((self.seconds,"runSeconds"),(self.calls,"modelCalls"),(self.budget,"modelTokens"),(self.retention,"retentionDays")):
            widget.setValue(DEFAULT_SETTINGS[key])
        self.window.web.setZoomFactor(1)
        self.window.page.runJavaScript("document.getElementById('app').classList.remove('context-collapsed')")

    def backup(self):
        if any(j["status"] in ("RUNNING", "QUEUED") for j in self.service.store.jobs()):
            raise AppError("MAINTENANCE_BUSY", "请等待当前处理完成后再备份")
        path, _ = QFileDialog.getSaveFileName(self, "保存加密备份", "DataMind-backup.7z", "7z (*.7z)")
        if not path:
            return
        password, ok = QInputDialog.getText(self, "备份密码", "至少 12 个字符；遗失后无法恢复", QLineEdit.EchoMode.Password)
        if ok:
            self.window.stop_worker()
            try:
                backup(self.service.store, path, password)
                QMessageBox.information(self, "备份完成", "加密备份已保存，不包含 Key。")
            finally:
                if self.window.enable_worker:
                    self.window.start_worker()

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择备份", "", "7z (*.7z)")
        if not path:
            return
        password, ok = QInputDialog.getText(self, "恢复密码", "备份密码", QLineEdit.EchoMode.Password)
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择新的空目录")
        if folder:
            result = verify_restore(path, password, folder)
            QMessageBox.information(self, "恢复完成", result["message"] + "\n目录：" + folder)

    def migrate(self):
        if any(j["status"] in ("RUNNING", "QUEUED") for j in self.service.store.jobs()):
            raise AppError("MAINTENANCE_BUSY", "请先完成当前处理")
        folder = QFileDialog.getExistingDirectory(self, "选择新的空数据目录")
        if not folder:
            return
        self.window.stop_worker()
        migrated = False
        try:
            result = migrate(self.service.store, folder)
            from PySide6.QtCore import QSettings
            QSettings().setValue("dataDirectory", folder)
            migrated = True
            QMessageBox.information(self, "迁移已验证", "下次启动使用：" + folder + "\n原目录保留，可在切换目录中回退：" + result["rollbackPath"] + "\n应用即将安全退出，请重新启动。")
            self.accept()
            self.window.close()
        finally:
            if not migrated and self.window.enable_worker:
                self.window.start_worker()

    def switch_workspace(self):
        if any(j["status"] in ("RUNNING", "QUEUED") for j in self.service.store.jobs()):
            raise AppError("MAINTENANCE_BUSY", "请先完成当前处理")
        folder = QFileDialog.getExistingDirectory(self, "选择已存在的数据目录（下次启动生效）")
        if not folder:
            return
        if not (Path(folder) / "datamind.sqlite3").is_file():
            raise AppError("WORKSPACE_INVALID", "所选目录不是已有工作空间")
        verify_references(Path(folder))
        from PySide6.QtCore import QSettings
        QSettings().setValue("dataDirectory", folder)
        QMessageBox.information(self, "目录已选择", "应用即将安全退出，下次启动使用所选目录。")
        self.accept()
        self.window.close()

    def cleanup(self):
        days = self.service.settings()["retentionDays"]
        preview = cleanup_preview(self.service.store, days)
        message = f"可清理无引用文件 {len(preview['unreferencedBlobs'])} 个，共 {preview['bytes']} 字节。\n超过保留期的归档会话 {len(preview['sessions'])} 个；会话需在归档列表中单独永久删除。\n本操作仅删除预览中的无引用文件，是否继续？"
        if QMessageBox.question(self, "清理预览", message) == QMessageBox.StandardButton.Yes:
            self.window.stop_worker()
            try:
                result = cleanup_blobs(self.service.store, days, preview["previewHash"])
                QMessageBox.information(self, "清理完成", f"已清理 {result['deletedBlobs']} 个无引用文件。")
            finally:
                if self.window.enable_worker:
                    self.window.start_worker()

    def diagnostics(self):
        result = diagnostics(self.service.store)
        path, _ = QFileDialog.getSaveFileName(self, "保存诊断记录", result["fileName"], "JSON (*.json)")
        if path:
            shutil.copyfile(self.service.store.home / "exports" / result["fileName"], path)
