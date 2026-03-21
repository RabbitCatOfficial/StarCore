from __future__ import annotations

from pathlib import Path
import json

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .audit import AuditContext, DEFAULT_AUDIT_REQUEST, is_supported_archive_path
from .config import (
    APP_ROOT,
    AppConfig,
    PROVIDER_PRESETS,
    TRANSPORT_LABELS,
    load_config,
    save_config,
)
from .reports import ReportRecord, ReportRepository
from .workers import AuditWorker, ConnectionTestWorker


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class AutoHeightTextBrowser(QTextBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(self._sync_height)

    def _sync_height(self, *_args) -> None:
        self.setFixedHeight(max(44, int(self.document().size().height() + 14)))

    def set_content(self, text: str, *, markdown: bool) -> None:
        if markdown:
            self.setMarkdown(text or " ")
        else:
            self.setPlainText(text or " ")
        self._sync_height()


class ChatMessageWidget(QWidget):
    ROLE_STYLE = {
        "assistant": {
            "title": "审计引擎",
            "bg": "#0d1c2f",
            "border": "#2a506f",
            "title_color": "#7fe2ff",
        },
        "user": {
            "title": "用户指令",
            "bg": "#113451",
            "border": "#3d82aa",
            "title_color": "#a8eaff",
        },
        "system": {
            "title": "系统状态",
            "bg": "#0f292b",
            "border": "#2f7376",
            "title_color": "#9ff4e5",
        },
    }

    def __init__(self, role: str, text: str = "", title: str | None = None) -> None:
        super().__init__()
        meta = self.ROLE_STYLE[role]
        self._buffer = text

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        bubble = QFrame()
        bubble.setMaximumWidth(980)
        bubble.setStyleSheet(
            f"QFrame {{ background: {meta['bg']}; border: 1px solid {meta['border']}; "
            "border-radius: 18px; }}"
            "QLabel { background: transparent; border: none; }"
        )
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(16, 14, 16, 14)
        bubble_layout.setSpacing(8)

        header = QLabel(title or meta["title"])
        header.setStyleSheet(
            f"color: {meta['title_color']}; font-family: 'Bahnschrift SemiBold'; "
            "font-size: 13px; font-weight: 700;"
        )
        self.body = AutoHeightTextBrowser()
        self.body.setStyleSheet("background: transparent; border: none; color: #edf8ff;")
        self.body.set_content(text, markdown=False)

        bubble_layout.addWidget(header)
        bubble_layout.addWidget(self.body)

        if role == "user":
            outer.addStretch(1)
            outer.addWidget(bubble, 0)
        elif role == "system":
            outer.addStretch(1)
            outer.addWidget(bubble, 0)
            outer.addStretch(1)
        else:
            outer.addWidget(bubble, 0)
            outer.addStretch(1)

    def append_text(self, chunk: str) -> None:
        self._buffer += chunk
        self.body.set_content(self._buffer, markdown=False)

    def finalize(self) -> None:
        self.body.set_content(self._buffer, markdown=True)

    def set_error(self, text: str) -> None:
        self._buffer = text
        self.body.set_content(text, markdown=False)


class ChatTimeline(QScrollArea):
    archive_dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ChatTimeline")
        self.setWidgetResizable(True)
        self.setAcceptDrops(True)
        self.setProperty("dragActive", False)

        container = QWidget()
        self.layout_root = QVBoxLayout(container)
        self.layout_root.setContentsMargins(20, 20, 20, 20)
        self.layout_root.setSpacing(14)

        self.empty_hint = QLabel(
            "将 `zip` 或 `tar.gz` 源码包拖到这里开始审计。\n\n"
            "下方输入框可补充审计范围、关注点或输出格式要求。"
        )
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setObjectName("MutedLabel")
        self.empty_hint.setStyleSheet(
            "border: 1px dashed #365874; border-radius: 18px; padding: 36px; "
            "font-size: 14px; line-height: 1.6;"
        )
        self.layout_root.addWidget(self.empty_hint)
        self.layout_root.addStretch(1)
        self.setWidget(container)

    def _update_empty_hint(self) -> None:
        self.empty_hint.setVisible(self.layout_root.count() <= 2)

    def add_message(self, role: str, text: str = "", title: str | None = None) -> ChatMessageWidget:
        widget = ChatMessageWidget(role, text=text, title=title)
        self.layout_root.insertWidget(self.layout_root.count() - 1, widget)
        self._update_empty_hint()
        QTimer.singleShot(0, self.scroll_to_bottom)
        return widget

    def clear_messages(self) -> None:
        while self.layout_root.count() > 1:
            item = self.layout_root.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._update_empty_hint()

    def scroll_to_bottom(self) -> None:
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        _repolish(self)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and is_supported_archive_path(urls[0].toLocalFile()):
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self._set_drag_active(False)
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.archive_dropped.emit(urls[0].toLocalFile())
            event.acceptProposedAction()
            return
        event.ignore()


class SystemPromptDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("系统提示词窗口")
        self.resize(900, 680)
        self.setMinimumSize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("系统提示词窗口")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "这里存放附加系统提示词。建议写清审计范围、输出格式和重点风险点，"
            "避免把运行日志或临时对话内容塞进来。"
        )
        subtitle.setObjectName("MutedLabel")
        subtitle.setWordWrap(True)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "示例：优先关注上传、鉴权绕过、命令执行；输出时按高危到低危排序，并给出可复现 PoC。"
        )
        self.editor.setMinimumHeight(420)

        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.stats_label = QLabel()
        self.stats_label.setObjectName("MutedLabel")
        footer.addWidget(self.stats_label)
        footer.addStretch(1)
        self.clear_button = QPushButton("清空")
        footer.addWidget(self.clear_button)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.button_box.button(QDialogButtonBox.Ok).setText("应用")
        self.button_box.button(QDialogButtonBox.Ok).setObjectName("accent")
        self.button_box.button(QDialogButtonBox.Cancel).setText("取消")
        footer.addWidget(self.button_box)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.editor, 1)
        layout.addLayout(footer)

        self.editor.textChanged.connect(self._update_stats)
        self.clear_button.clicked.connect(self.editor.clear)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self._update_stats()

    def _update_stats(self) -> None:
        text = self.editor.toPlainText().strip()
        lines = 0 if not text else text.count("\n") + 1
        self.stats_label.setText(f"当前已填写 {len(text)} 字符 / {lines} 行")

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)
        self._update_stats()

    def text(self) -> str:
        return self.editor.toPlainText().strip()


class SettingsPage(QWidget):
    save_requested = Signal()
    test_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self.system_prompt_value = ""
        self.system_prompt_dialog = SystemPromptDialog(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("PanelCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        title = QLabel("模型与审计参数")
        title.setObjectName("PageTitle")
        subtitle = QLabel("支持主流 OpenAI 兼容模型、Ollama 和自定义接口，所有请求默认使用流式输出。")
        subtitle.setObjectName("MutedLabel")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        self.provider_combo = QComboBox()
        for preset in PROVIDER_PRESETS.values():
            self.provider_combo.addItem(preset.label, preset.key)
        self.transport_combo = QComboBox()
        for key, label in TRANSPORT_LABELS.items():
            self.transport_combo.addItem(label, key)
        self.base_url_input = QLineEdit()
        self.chat_path_input = QLineEdit()
        self.model_input = QLineEdit()
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.temperature_input = QDoubleSpinBox()
        self.temperature_input.setRange(0.0, 2.0)
        self.temperature_input.setSingleStep(0.1)
        self.top_p_input = QDoubleSpinBox()
        self.top_p_input.setRange(0.0, 1.0)
        self.top_p_input.setSingleStep(0.05)
        self.max_tokens_input = QSpinBox()
        self.max_tokens_input.setRange(128, 32768)
        self.timeout_input = QSpinBox()
        self.timeout_input.setRange(10, 3600)
        self.audit_files_input = QSpinBox()
        self.audit_files_input.setRange(5, 200)
        self.audit_chars_input = QSpinBox()
        self.audit_chars_input.setRange(5000, 200000)
        self.per_file_chars_input = QSpinBox()
        self.per_file_chars_input.setRange(500, 20000)
        self.verify_ssl_checkbox = QCheckBox("验证 SSL 证书")
        self.codex_mode_checkbox = QCheckBox("Codex local command mode")
        self.extra_headers_input = QPlainTextEdit()
        self.extra_headers_input.setFixedHeight(80)
        self.system_prompt_summary = QLabel()
        self.system_prompt_summary.setWordWrap(True)
        self.system_prompt_summary.setObjectName("MutedLabel")
        self.edit_system_prompt_button = QPushButton("打开系统提示词窗口")
        self.endpoint_preview = QLabel()
        self.endpoint_preview.setObjectName("MutedLabel")
        self.test_result_label = QLabel("未测试连接")
        self.test_result_label.setObjectName("MutedLabel")

        prompt_card = QFrame()
        prompt_card.setObjectName("InsetCard")
        prompt_card_layout = QVBoxLayout(prompt_card)
        prompt_card_layout.setContentsMargins(14, 12, 14, 12)
        prompt_card_layout.setSpacing(10)
        prompt_hint = QLabel("附加系统提示词会拼接到基础审计提示词后，适合存放长期有效的审计要求。")
        prompt_hint.setObjectName("MutedLabel")
        prompt_hint.setWordWrap(True)
        prompt_card_layout.addWidget(self.system_prompt_summary)
        prompt_card_layout.addWidget(prompt_hint)
        prompt_card_layout.addWidget(self.edit_system_prompt_button, 0, Qt.AlignRight)

        form.addRow("服务商", self.provider_combo)
        form.addRow("接口协议", self.transport_combo)
        form.addRow("Base URL", self.base_url_input)
        form.addRow("接口路径", self.chat_path_input)
        form.addRow("模型名", self.model_input)
        form.addRow("API Key", self.api_key_input)
        form.addRow("Temperature", self.temperature_input)
        form.addRow("Top P", self.top_p_input)
        form.addRow("输出 Token 上限", self.max_tokens_input)
        form.addRow("请求超时（秒）", self.timeout_input)
        form.addRow("送审文件上限", self.audit_files_input)
        form.addRow("快照总字符上限", self.audit_chars_input)
        form.addRow("单文件字符上限", self.per_file_chars_input)
        form.addRow("HTTPS", self.verify_ssl_checkbox)
        form.addRow("Agent", self.codex_mode_checkbox)
        form.addRow("额外请求头 JSON", self.extra_headers_input)
        form.addRow("附加系统提示词", prompt_card)

        layout.addLayout(form)
        layout.addWidget(self.endpoint_preview)

        actions = QHBoxLayout()
        actions.addWidget(self.test_result_label, 1)
        self.test_button = QPushButton("测试连接")
        actions.addStretch(1)
        actions.addWidget(self.test_button)
        self.save_button = QPushButton("保存配置")
        self.save_button.setObjectName("accent")
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        outer.addWidget(card)

        self.provider_combo.currentIndexChanged.connect(self._apply_selected_preset)
        self.save_button.clicked.connect(self.save_requested.emit)
        self.test_button.clicked.connect(self.test_requested.emit)
        self.transport_combo.currentIndexChanged.connect(self._update_endpoint_preview)
        self.base_url_input.textChanged.connect(self._update_endpoint_preview)
        self.chat_path_input.textChanged.connect(self._update_endpoint_preview)
        self.model_input.textChanged.connect(self._update_endpoint_preview)
        self.codex_mode_checkbox.stateChanged.connect(self._update_endpoint_preview)
        self.edit_system_prompt_button.clicked.connect(self._open_system_prompt_dialog)
        self._set_system_prompt_text("")

    def _set_system_prompt_text(self, text: str) -> None:
        self.system_prompt_value = text.strip()
        if not self.system_prompt_value:
            self.system_prompt_summary.setText(
                "未配置附加系统提示词。点击右侧按钮后，可在独立窗口里维护长期有效的审计要求。"
            )
            return

        preview = self.system_prompt_value
        if len(preview) > 180:
            preview = f"{preview[:180].rstrip()}..."
        self.system_prompt_summary.setText(
            f"已配置 {len(self.system_prompt_value)} 字符。\n{preview}"
        )

    def _open_system_prompt_dialog(self) -> None:
        self.system_prompt_dialog.set_text(self.system_prompt_value)
        if self.system_prompt_dialog.exec() == QDialog.Accepted:
            self._set_system_prompt_text(self.system_prompt_dialog.text())

    def _apply_selected_preset(self, *_args) -> None:
        if self._loading:
            return
        preset = PROVIDER_PRESETS[self.provider_combo.currentData()]
        self.transport_combo.setCurrentIndex(max(self.transport_combo.findData(preset.transport), 0))
        self.base_url_input.setText(preset.base_url)
        self.chat_path_input.setText(preset.chat_path)
        self.model_input.setText(preset.default_model)
        self._update_endpoint_preview()

    def _update_endpoint_preview(self) -> None:
        temp_config = AppConfig(
            provider=str(self.provider_combo.currentData()),
            transport=str(self.transport_combo.currentData()),
            base_url=self.base_url_input.text().strip(),
            chat_path=self.chat_path_input.text().strip(),
            model=self.model_input.text().strip(),
            enable_codex_mode=self.codex_mode_checkbox.isChecked(),
        )
        agent_mode = (
            "Codex local shell"
            if temp_config.enable_codex_mode and temp_config.transport == "responses"
            else "Standard chat"
        )
        self.endpoint_preview.setText(
            f"当前请求地址：{temp_config.resolve_chat_url()} | 传输协议：{temp_config.transport}"
        )
        self.endpoint_preview.setText(f"{self.endpoint_preview.text()} | Agent: {agent_mode}")

    def apply_config(self, config: AppConfig) -> None:
        self._loading = True
        self.provider_combo.setCurrentIndex(max(self.provider_combo.findData(config.provider), 0))
        self.transport_combo.setCurrentIndex(max(self.transport_combo.findData(config.transport), 0))
        self.base_url_input.setText(config.base_url)
        self.chat_path_input.setText(config.chat_path)
        self.model_input.setText(config.model)
        self.api_key_input.setText(config.api_key)
        self.temperature_input.setValue(config.temperature)
        self.top_p_input.setValue(config.top_p)
        self.max_tokens_input.setValue(config.max_output_tokens)
        self.timeout_input.setValue(config.timeout_seconds)
        self.audit_files_input.setValue(config.audit_max_files)
        self.audit_chars_input.setValue(config.audit_max_chars)
        self.per_file_chars_input.setValue(config.per_file_char_limit)
        self.extra_headers_input.setPlainText(config.extra_headers_json)
        self._set_system_prompt_text(config.system_prompt_suffix)
        self.verify_ssl_checkbox.setChecked(config.verify_ssl)
        self.codex_mode_checkbox.setChecked(config.enable_codex_mode)
        self._loading = False
        self.set_test_result("未测试连接", state="neutral")
        self._update_endpoint_preview()

    def collect_config(self) -> AppConfig:
        headers_text = self.extra_headers_input.toPlainText().strip()
        if headers_text:
            parsed = json.loads(headers_text)
            if not isinstance(parsed, dict):
                raise ValueError("额外请求头必须是 JSON 对象。")

        provider_key = str(self.provider_combo.currentData())
        return AppConfig(
            provider=provider_key,
            transport=str(self.transport_combo.currentData()),
            base_url=self.base_url_input.text().strip(),
            chat_path=self.chat_path_input.text().strip(),
            model=self.model_input.text().strip(),
            api_key=self.api_key_input.text(),
            temperature=float(self.temperature_input.value()),
            top_p=float(self.top_p_input.value()),
            max_output_tokens=int(self.max_tokens_input.value()),
            timeout_seconds=int(self.timeout_input.value()),
            audit_max_files=int(self.audit_files_input.value()),
            audit_max_chars=int(self.audit_chars_input.value()),
            per_file_char_limit=int(self.per_file_chars_input.value()),
            extra_headers_json=headers_text,
            system_prompt_suffix=self.system_prompt_value,
            verify_ssl=self.verify_ssl_checkbox.isChecked(),
            enable_codex_mode=self.codex_mode_checkbox.isChecked(),
        )

    def set_test_result(self, text: str, *, state: str = "neutral") -> None:
        color_map = {
            "neutral": "#95a9bf",
            "running": "#8de9ff",
            "success": "#9ff4e5",
            "error": "#ffb1b1",
        }
        self.test_result_label.setText(text)
        self.test_result_label.setStyleSheet(f"color: {color_map.get(state, '#95a9bf')};")

    def set_action_buttons_enabled(self, enabled: bool) -> None:
        self.save_button.setEnabled(enabled)
        self.test_button.setEnabled(enabled)
        self.edit_system_prompt_button.setEnabled(enabled)


class ReportsPage(QWidget):
    def __init__(self, repository: ReportRepository) -> None:
        super().__init__()
        self.repository = repository
        self.records: list[ReportRecord] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        title = QLabel("本地审计报告")
        title.setObjectName("PageTitle")
        subtitle = QLabel("所有报告存储在项目下的 runtime_data/reports 目录。")
        subtitle.setObjectName("MutedLabel")
        title_block = QVBoxLayout()
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.open_folder_button = QPushButton("打开报告目录")
        self.open_file_button = QPushButton("打开选中报告")
        header.addWidget(self.refresh_button)
        header.addWidget(self.open_folder_button)
        header.addWidget(self.open_file_button)
        outer.addLayout(header)

        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)

        list_card = QFrame()
        list_card.setObjectName("PanelCard")
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(18, 18, 18, 18)
        list_layout.setSpacing(12)
        self.report_list = QListWidget()
        self.report_list.setSelectionMode(QAbstractItemView.SingleSelection)
        list_layout.addWidget(self.report_list)

        preview_card = QFrame()
        preview_card.setObjectName("PanelCard")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(18, 18, 18, 18)
        preview_layout.setSpacing(12)
        self.report_meta = QLabel("未选择报告")
        self.report_meta.setObjectName("SectionTitle")
        self.report_preview = QTextBrowser()
        self.report_preview.setOpenExternalLinks(True)
        preview_layout.addWidget(self.report_meta)
        preview_layout.addWidget(self.report_preview, 1)

        splitter.addWidget(list_card)
        splitter.addWidget(preview_card)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([360, 980])
        outer.addWidget(splitter, 1)

        self.report_list.currentItemChanged.connect(self._render_selected_report)
        self.refresh_button.clicked.connect(self.refresh_reports)
        self.open_folder_button.clicked.connect(self.open_reports_folder)
        self.open_file_button.clicked.connect(self.open_selected_report_file)

    def refresh_reports(self) -> None:
        self.records = self.repository.list_reports()
        self.report_list.clear()
        for record in self.records:
            item = QListWidgetItem(f"{record.project_name}\n{record.created_at} · {record.model}")
            item.setToolTip(record.summary)
            item.setData(Qt.UserRole, record)
            self.report_list.addItem(item)

        if self.report_list.count() > 0:
            self.report_list.setCurrentRow(0)
        else:
            self.report_meta.setText("暂无审计报告")
            self.report_preview.setMarkdown(
                "### 暂无报告\n\n拖入源码包完成首次审计后，报告会自动保存在本地。"
            )

    def _current_record(self) -> ReportRecord | None:
        item = self.report_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _render_selected_report(self, *_args) -> None:
        record = self._current_record()
        if record is None:
            return
        self.report_meta.setText(
            f"{record.project_name} | {record.created_at} | {record.provider}/{record.model}"
        )
        self.report_preview.setMarkdown(self.repository.read_report(record))

    def open_reports_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.repository.reports_dir)))

    def open_selected_report_file(self) -> None:
        record = self._current_record()
        if record is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(record.file_path))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("星核代码审计台")
        self.resize(1720, 1040)
        self.setMinimumSize(1380, 900)

        self.config = load_config()
        self.report_repository = ReportRepository()
        self.worker: AuditWorker | None = None
        self.connection_worker: ConnectionTestWorker | None = None
        self.current_context: AuditContext | None = None
        self.conversation_history: list[dict] = []
        self.active_message_widget: ChatMessageWidget | None = None
        self.pending_user_prompt = ""

        self._build_ui()
        self.settings_page.apply_config(self.config)
        self.reports_page.refresh_reports()
        self._update_runtime_labels()
        self._reset_runtime_metrics()
        self._sync_ui_busy_state()
        self.status_chip.setText("空闲")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.stack = QStackedWidget()
        sidebar = self._build_sidebar()
        root.addWidget(sidebar)
        self.chat_page = self._build_chat_page()
        self.reports_page = ReportsPage(self.report_repository)
        self.settings_page = SettingsPage()
        self.settings_page.save_requested.connect(self._save_settings)
        self.settings_page.test_requested.connect(self._test_connection)

        self.stack.addWidget(self.chat_page)
        self.stack.addWidget(self.reports_page)
        self.stack.addWidget(self.settings_page)
        root.addWidget(self.stack, 1)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(268)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 18)
        layout.setSpacing(12)

        title = QLabel("星核代码审计台")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Nebula Audit\nQt · Streamed LLM · Local Reports")
        subtitle.setObjectName("SubTitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(18)

        self.button_group = QButtonGroup(self)
        self.chat_button = QPushButton("审计聊天")
        self.reports_button = QPushButton("报告管理")
        self.settings_button = QPushButton("API 配置")
        for index, button in enumerate((self.chat_button, self.reports_button, self.settings_button)):
            button.setCheckable(True)
            button.setProperty("nav", True)
            self.button_group.addButton(button, index)
            layout.addWidget(button)
        self.chat_button.setChecked(True)
        self.button_group.idClicked.connect(self.stack.setCurrentIndex)

        layout.addSpacing(18)
        runtime_card = QFrame()
        runtime_card.setObjectName("PanelCard")
        runtime_layout = QVBoxLayout(runtime_card)
        runtime_layout.setContentsMargins(14, 14, 14, 14)
        runtime_layout.setSpacing(8)
        runtime_title = QLabel("运行摘要")
        runtime_title.setObjectName("SectionTitle")
        self.sidebar_model_label = QLabel()
        self.sidebar_model_label.setObjectName("MutedLabel")
        self.sidebar_storage_label = QLabel()
        self.sidebar_storage_label.setObjectName("MutedLabel")
        self.sidebar_progress_label = QLabel("预计进度：0%")
        self.sidebar_progress_label.setObjectName("MutedLabel")
        self.sidebar_command_label = QLabel("本地命令：0 次")
        self.sidebar_command_label.setObjectName("MutedLabel")
        runtime_layout.addWidget(runtime_title)
        runtime_layout.addWidget(self.sidebar_model_label)
        runtime_layout.addWidget(self.sidebar_storage_label)
        runtime_layout.addWidget(self.sidebar_progress_label)
        runtime_layout.addWidget(self.sidebar_command_label)
        layout.addWidget(runtime_card)

        layout.addStretch(1)
        footer = QLabel(f"项目目录\n{APP_ROOT}")
        footer.setObjectName("MutedLabel")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return sidebar

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(18)

        hero = QFrame()
        hero.setObjectName("HeroCard")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        hero_layout.setSpacing(18)

        title_block = QVBoxLayout()
        title = QLabel("拖拽源码包开始流式审计")
        title.setObjectName("PageTitle")
        subtitle = QLabel("把压缩包拖进聊天区，系统会自动解压、抽取重点源码快照并发送给模型。")
        subtitle.setObjectName("MutedLabel")
        self.project_label = QLabel("当前项目：未加载")
        self.project_label.setObjectName("SectionTitle")
        self.model_label = QLabel()
        self.model_label.setObjectName("MutedLabel")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_block.addSpacing(6)
        title_block.addWidget(self.project_label)
        title_block.addWidget(self.model_label)

        hero_layout.addLayout(title_block, 1)
        runtime_panel = QFrame()
        runtime_panel.setObjectName("InsetCard")
        runtime_panel.setMinimumWidth(360)
        runtime_panel.setMaximumWidth(420)
        runtime_panel_layout = QVBoxLayout(runtime_panel)
        runtime_panel_layout.setContentsMargins(16, 16, 16, 16)
        runtime_panel_layout.setSpacing(10)

        runtime_header = QHBoxLayout()
        runtime_header.setContentsMargins(0, 0, 0, 0)
        runtime_title = QLabel("运行状态")
        runtime_title.setObjectName("SectionTitle")
        self.status_chip = QLabel("空闲")
        self.status_chip.setObjectName("StatusChip")
        runtime_header.addWidget(runtime_title)
        runtime_header.addStretch(1)
        runtime_header.addWidget(self.status_chip, 0, Qt.AlignTop)

        self.progress_title_label = QLabel("预计进度 0%")
        self.progress_title_label.setObjectName("SectionTitle")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_detail_label = QLabel("等待开始")
        self.progress_detail_label.setObjectName("MutedLabel")
        self.command_count_label = QLabel("本地命令 0 次")
        self.command_count_label.setObjectName("MutedLabel")

        runtime_panel_layout.addLayout(runtime_header)
        runtime_panel_layout.addWidget(self.progress_title_label)
        runtime_panel_layout.addWidget(self.progress_bar)
        runtime_panel_layout.addWidget(self.progress_detail_label)
        runtime_panel_layout.addWidget(self.command_count_label)
        hero_layout.addWidget(runtime_panel, 0)
        outer.addWidget(hero)

        self.chat_timeline = ChatTimeline()
        self.chat_timeline.archive_dropped.connect(self._start_audit_from_path)
        outer.addWidget(self.chat_timeline, 1)

        composer = QFrame()
        composer.setObjectName("ComposerCard")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(20, 18, 20, 18)
        composer_layout.setSpacing(12)
        composer_title = QLabel("任务输入")
        composer_title.setObjectName("SectionTitle")
        composer_hint = QLabel(
            "首次审计时，这里作为审计附加要求；项目加载后，这里可继续追问，例如“展开讲第一个高危点”。"
        )
        composer_hint.setObjectName("MutedLabel")
        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText("可选：填写关注模块、输出格式、PoC 方式或后续追问内容。")
        self.prompt_input.setMinimumHeight(138)
        self.prompt_input.setMaximumHeight(220)

        button_row = QHBoxLayout()
        self.choose_archive_button = QPushButton("选择源码包")
        self.send_button = QPushButton("发送追问")
        self.send_button.setObjectName("accent")
        self.clear_session_button = QPushButton("清空会话")
        button_row.addWidget(self.choose_archive_button)
        button_row.addStretch(1)
        button_row.addWidget(self.clear_session_button)
        button_row.addWidget(self.send_button)

        composer_layout.addWidget(composer_title)
        composer_layout.addWidget(composer_hint)
        composer_layout.addWidget(self.prompt_input)
        composer_layout.addLayout(button_row)
        outer.addWidget(composer)

        self.choose_archive_button.clicked.connect(self._choose_archive)
        self.send_button.clicked.connect(self._handle_send_clicked)
        self.clear_session_button.clicked.connect(self._clear_session)
        return page

    def _reset_runtime_metrics(self, detail: str = "等待开始") -> None:
        self._update_progress_widgets(0, detail)
        self._update_command_counter(0)

    def _update_progress_widgets(self, progress: int, detail: str) -> None:
        clamped = max(0, min(int(progress), 100))
        self.progress_bar.setValue(clamped)
        self.progress_title_label.setText(f"预计进度 {clamped}%")
        self.progress_detail_label.setText(detail)
        self.sidebar_progress_label.setText(f"预计进度：{clamped}%")

    def _update_command_counter(self, count: int) -> None:
        text = f"本地命令 {count} 次"
        self.command_count_label.setText(text)
        self.sidebar_command_label.setText(f"本地命令：{count} 次")

    def _collect_settings(self, *, show_success: bool) -> AppConfig | None:
        try:
            config = self.settings_page.collect_config()
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, "配置错误", f"额外请求头不是有效 JSON：{exc}")
            return None
        except ValueError as exc:
            QMessageBox.warning(self, "配置错误", str(exc))
            return None

        save_config(config)
        self.config = config
        self._update_runtime_labels()
        if show_success:
            self.status_chip.setText("配置已保存")
        return config

    def _save_settings(self) -> None:
        self._collect_settings(show_success=True)

    def _update_runtime_labels(self) -> None:
        agent_mode = (
            "Codex local shell"
            if self.config.enable_codex_mode and self.config.transport == "responses"
            else "Standard chat"
        )
        self.model_label.setText(
            f"当前模型：{self.config.provider} / {self.config.model} | 流式接口：{self.config.resolve_chat_url()}"
        )
        self.sidebar_model_label.setText(f"模型：{self.config.provider} / {self.config.model}")
        self.sidebar_storage_label.setText(f"报告目录：{self.report_repository.reports_dir}")

        self.model_label.setText(f"{self.model_label.text()} | Agent: {agent_mode}")
        self.sidebar_model_label.setText(f"{self.sidebar_model_label.text()} | Agent: {agent_mode}")

    def _sync_ui_busy_state(self) -> None:
        busy = self.worker is not None or self.connection_worker is not None
        self.choose_archive_button.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.clear_session_button.setEnabled(not busy)
        self.chat_timeline.setAcceptDrops(not busy)
        self.settings_page.set_action_buttons_enabled(not busy)

    def _choose_archive(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择源码压缩包",
            str(Path.home()),
            "Archives (*.zip *.tar *.tar.gz *.tgz *.tar.bz2 *.tar.xz);;All Files (*.*)",
        )
        if path:
            self._start_audit_from_path(path)

    def _append_system_message(self, text: str) -> None:
        self.chat_timeline.add_message("system", text)

    def _start_audit_from_path(self, archive_path: str) -> None:
        if self.worker is not None or self.connection_worker is not None:
            self._append_system_message("当前已有任务运行中，请等待本次审计完成。")
            return
        if not is_supported_archive_path(archive_path):
            self._append_system_message("不支持该压缩包格式，当前仅支持 zip 与 tar 系列。")
            return

        config = self._collect_settings(show_success=False)
        if config is None:
            return

        prompt = self.prompt_input.toPlainText().strip()
        self.pending_user_prompt = prompt or DEFAULT_AUDIT_REQUEST
        self.current_context = None
        self.conversation_history = []
        self.project_label.setText(f"当前项目：{Path(archive_path).name}")

        self.chat_timeline.add_message("system", f"已接收源码包：{archive_path}")
        self.chat_timeline.add_message("user", self.pending_user_prompt)
        self.active_message_widget = self.chat_timeline.add_message("assistant", "正在准备审计上下文...")
        self.prompt_input.clear()
        self._reset_runtime_metrics("准备审计上下文")

        self.worker = AuditWorker(
            mode="audit",
            config=config,
            report_repository=self.report_repository,
            archive_path=archive_path,
            user_text=prompt,
        )
        self._wire_worker()
        self.status_chip.setText("审计中")
        self._sync_ui_busy_state()
        self.worker.start()

    def _handle_send_clicked(self) -> None:
        if self.worker is not None or self.connection_worker is not None:
            self._append_system_message("当前已有任务运行中，请等待其完成。")
            return

        prompt = self.prompt_input.toPlainText().strip()
        if not prompt:
            self._append_system_message("请输入追问内容，或先拖入源码包开始首次审计。")
            return

        if self.current_context is None:
            self._append_system_message("当前还没有项目上下文，请先拖入源码压缩包。")
            return

        config = self._collect_settings(show_success=False)
        if config is None:
            return

        self.pending_user_prompt = prompt
        self.chat_timeline.add_message("user", prompt)
        self.active_message_widget = self.chat_timeline.add_message("assistant", "正在继续分析...")
        self.prompt_input.clear()
        self._reset_runtime_metrics("准备追问分析")

        self.worker = AuditWorker(
            mode="chat",
            config=config,
            report_repository=self.report_repository,
            context=self.current_context,
            user_text=prompt,
            conversation_history=self.conversation_history,
        )
        self._wire_worker()
        self.status_chip.setText("追问中")
        self._sync_ui_busy_state()
        self.worker.start()

    def _test_connection(self) -> None:
        if self.worker is not None or self.connection_worker is not None:
            self._append_system_message("当前已有任务运行中，请等待其完成。")
            return

        config = self._collect_settings(show_success=False)
        if config is None:
            return

        self.settings_page.set_test_result("正在测试连接...", state="running")
        self.connection_worker = ConnectionTestWorker(config=config)
        self.connection_worker.status_changed.connect(self.status_chip.setText)
        self.connection_worker.completed.connect(self._on_test_connection_completed)
        self.connection_worker.failed.connect(self._on_test_connection_failed)
        self.connection_worker.finished.connect(self._on_test_connection_finished)
        self.status_chip.setText("连接测试中")
        self._sync_ui_busy_state()
        self.connection_worker.start()

    def _wire_worker(self) -> None:
        if self.worker is None:
            return
        self.worker.status_changed.connect(self.status_chip.setText)
        self.worker.progress_changed.connect(self._on_worker_progress_changed)
        self.worker.command_count_changed.connect(self._on_worker_command_count_changed)
        self.worker.chunk_received.connect(self._on_worker_chunk)
        self.worker.context_ready.connect(self._on_context_ready)
        self.worker.tool_event.connect(self._append_system_message)
        self.worker.completed.connect(self._on_worker_completed)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.finished.connect(self._on_worker_finished)

    def _on_context_ready(self, context: AuditContext) -> None:
        self.current_context = context
        self.project_label.setText(
            f"当前项目：{context.project_name} | 文件 {context.stats['total_files']} | 快照 {context.stats['selected_files']}"
        )

        if self.config.enable_codex_mode and context.workspace_dir:
            self._append_system_message(f"[Codex] 工作目录: {context.workspace_dir}")

    def _on_worker_progress_changed(self, progress: int, detail: str) -> None:
        self._update_progress_widgets(progress, detail)

    def _on_worker_command_count_changed(self, count: int) -> None:
        self._update_command_counter(count)

    def _on_worker_chunk(self, chunk: str) -> None:
        if self.active_message_widget is not None:
            self.active_message_widget.append_text(chunk)

    def _on_worker_completed(self, result: dict) -> None:
        content = result.get("content", "").strip() or "模型未返回可显示内容。"
        if self.active_message_widget is not None:
            if not result.get("content", "").strip():
                self.active_message_widget.set_error(content)
            else:
                self.active_message_widget.finalize()
        self.active_message_widget = None

        mode = result.get("mode")
        context = result.get("context")
        if isinstance(context, AuditContext):
            self.current_context = context

        self.conversation_history.append({"role": "user", "content": self.pending_user_prompt})
        self.conversation_history.append({"role": "assistant", "content": content})

        if mode == "audit":
            report = result.get("report")
            if report is not None:
                self.reports_page.refresh_reports()
                self._append_system_message(f"审计完成，报告已保存：{report.file_path}")
                self._update_progress_widgets(100, "审计完成，报告已保存")
        else:
            self._update_progress_widgets(100, "追问分析完成")
        self.pending_user_prompt = ""

    def _on_worker_failed(self, error_text: str) -> None:
        if self.active_message_widget is not None:
            self.active_message_widget.set_error(f"任务失败：{error_text}")
            self.active_message_widget = None
        self.status_chip.setText("任务失败")
        self._update_progress_widgets(self.progress_bar.value(), "任务失败")
        self._append_system_message(error_text)
        QMessageBox.warning(self, "任务失败", error_text)

    def _on_worker_finished(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None
        self._sync_ui_busy_state()

    def _on_test_connection_completed(self, result: dict) -> None:
        content = result.get("content", "").strip()
        preview = content[:160] + ("..." if len(content) > 160 else "")
        self.settings_page.set_test_result(f"连接成功：{preview}", state="success")
        self.status_chip.setText("连接测试成功")
        QMessageBox.information(self, "连接测试成功", f"接口可用。\n\n返回预览：{preview}")

    def _on_test_connection_failed(self, error_text: str) -> None:
        self.settings_page.set_test_result(f"连接失败：{error_text}", state="error")
        self.status_chip.setText("连接测试失败")
        QMessageBox.warning(self, "连接测试失败", error_text)

    def _on_test_connection_finished(self) -> None:
        if self.connection_worker is not None:
            self.connection_worker.deleteLater()
            self.connection_worker = None
        self._sync_ui_busy_state()

    def _clear_session(self) -> None:
        if self.worker is not None:
            self._append_system_message("请等待当前任务完成后再清空会话。")
            return
        self.current_context = None
        self.conversation_history = []
        self.pending_user_prompt = ""
        self.active_message_widget = None
        self.project_label.setText("当前项目：未加载")
        self.status_chip.setText("空闲")
        self.prompt_input.clear()
        self.chat_timeline.clear_messages()
        self._reset_runtime_metrics()
