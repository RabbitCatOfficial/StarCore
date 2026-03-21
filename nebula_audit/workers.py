from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QThread, Signal

from .audit import (
    AuditContext,
    build_audit_messages,
    build_followup_messages,
    prepare_audit_context,
)
from .config import AppConfig
from .llm import create_streaming_client
from .reports import ReportRepository


class ConnectionTestWorker(QThread):
    status_changed = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, *, config: AppConfig) -> None:
        super().__init__()
        self.config = config

    def run(self) -> None:
        try:
            client = create_streaming_client(self.config)
            self.status_changed.emit("正在测试模型连接")
            messages = [
                {
                    "role": "system",
                    "content": "You are a connection test assistant. Reply with CONNECTION_OK only.",
                },
                {"role": "user", "content": "请只返回 CONNECTION_OK。"},
            ]

            stream = client.stream_chat(messages)
            buffer = []
            try:
                for chunk in stream:
                    buffer.append(chunk)
                    if len("".join(buffer)) >= 160:
                        break
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()

            content = "".join(buffer).strip()
            if not content:
                raise RuntimeError("连接成功但模型未返回可见内容。")

            self.status_changed.emit("连接测试成功")
            self.completed.emit({"content": content})
        except Exception as exc:
            self.failed.emit(str(exc))


class AuditWorker(QThread):
    status_changed = Signal(str)
    progress_changed = Signal(int, str)
    command_count_changed = Signal(int)
    chunk_received = Signal(str)
    context_ready = Signal(object)
    tool_event = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        mode: str,
        config: AppConfig,
        report_repository: ReportRepository,
        archive_path: str | None = None,
        user_text: str = "",
        context: AuditContext | None = None,
        conversation_history: Sequence[dict] | None = None,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.config = config
        self.report_repository = report_repository
        self.archive_path = archive_path
        self.user_text = user_text
        self.context = context
        self.conversation_history = list(conversation_history or [])
        self.command_count = 0
        self.progress_value = 0
        self.progress_detail = "等待开始"
        self.generated_chars = 0

    def _emit_progress(self, value: int, detail: str | None = None) -> None:
        next_value = max(self.progress_value, max(0, min(int(value), 100)))
        next_detail = self.progress_detail if detail is None else detail
        if next_value != self.progress_value or next_detail != self.progress_detail:
            self.progress_value = next_value
            self.progress_detail = next_detail
            self.progress_changed.emit(self.progress_value, self.progress_detail)

    def _set_command_count(self, count: int) -> None:
        next_count = max(0, int(count))
        if next_count != self.command_count:
            self.command_count = next_count
            self.command_count_changed.emit(self.command_count)

    def _reset_runtime_counters(self) -> None:
        self.command_count = 0
        self.progress_value = 0
        self.progress_detail = "等待开始"
        self.generated_chars = 0
        self.command_count_changed.emit(0)
        self.progress_changed.emit(0, self.progress_detail)

    def _handle_client_event(self, event: dict) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "tool_call_started":
            command = str(event.get("command", "")).strip()
            cwd = str(event.get("cwd", "")).strip()
            self._set_command_count(self.command_count + 1)
            self._emit_progress(
                min(78, 44 + self.command_count * 6),
                f"正在执行第 {self.command_count} 条本地命令",
            )
            self.status_changed.emit("AI 正在调用本地命令")
            details = [f"[Codex] 本地命令 #{self.command_count}: {command}"]
            if cwd:
                details.append(f"[Codex] 目录: {cwd}")
            reason = str(event.get("reason", "")).strip()
            if reason:
                details.append(f"[Codex] 原因: {reason}")
            self.tool_event.emit("\n".join(details))
            return

        if event_type == "tool_call_completed":
            command = str(event.get("command", "")).strip()
            exit_code = event.get("exit_code")
            timed_out = bool(event.get("timed_out"))
            suffix = "timeout" if timed_out else f"exit={exit_code}"
            detail = "命令执行超时，AI 正在调整取证方式" if timed_out else "命令执行完成，AI 正在整理证据"
            self._emit_progress(min(84, 48 + self.command_count * 6), detail)
            self.tool_event.emit(f"[Codex] 命令完成: {command} ({suffix})")
            return

        if event_type == "tool_call_rejected":
            message = str(event.get("message", "")).strip() or "本地命令被拒绝"
            self._emit_progress(max(self.progress_value, 42), "命令被策略拦截，AI 正在切换方案")
            self.tool_event.emit(f"[Codex] 命令被拒绝: {message}")
            return

        if event_type == "tool_round_limit_reached":
            message = str(event.get("message", "")).strip() or "已达到本地命令轮次上限"
            self.status_changed.emit("AI 正在整理最终结论")
            self._emit_progress(88, "已达到命令轮次上限，正在汇总结论")
            self.tool_event.emit(f"[Codex] {message}")

    def run(self) -> None:
        try:
            client = create_streaming_client(self.config)
            active_context = self.context
            self._reset_runtime_counters()

            if self.mode == "audit":
                self.status_changed.emit("正在解压源码压缩包")
                self._emit_progress(10, "正在解压源码包")
                active_context = prepare_audit_context(self.archive_path or "", self.config)
                self.context_ready.emit(active_context)
                self._emit_progress(28, "源码快照已生成")
                self.status_changed.emit("正在向模型发送代码快照")
                messages = build_audit_messages(active_context, self.user_text)
                self._emit_progress(42, "正在向模型发送代码快照")
            elif self.mode == "chat":
                if active_context is None:
                    raise RuntimeError("当前没有可追问的项目上下文。")
                self.status_changed.emit("正在继续分析当前项目")
                self._emit_progress(18, "正在加载当前项目上下文")
                messages = build_followup_messages(
                    active_context,
                    self.user_text,
                    self.conversation_history,
                )
                self._emit_progress(36, "正在向模型发送追问")
            else:
                raise RuntimeError(f"未知任务模式: {self.mode}")

            buffer = []
            workspace_dir = active_context.workspace_dir if active_context is not None else None
            for chunk in client.stream_chat(
                messages,
                workspace_dir=workspace_dir,
                event_handler=self._handle_client_event,
            ):
                buffer.append(chunk)
                self.generated_chars += len(chunk)
                if self.mode == "audit":
                    detail = "模型正在输出审计报告"
                    estimated = 54 + min(40, self.generated_chars // 120)
                else:
                    detail = "模型正在输出追问结果"
                    estimated = 50 + min(40, self.generated_chars // 120)
                self._emit_progress(estimated, detail)
                self.chunk_received.emit(chunk)

            content = "".join(buffer).strip()
            if not content:
                raise RuntimeError("模型返回为空，未生成可保存的内容。")
            saved_report = None
            if self.mode == "audit":
                self._emit_progress(96, "正在保存审计报告")
                saved_report = self.report_repository.save_report(
                    project_name=active_context.project_name,
                    archive_name=active_context.archive_name,
                    model=self.config.model,
                    provider=self.config.provider,
                    body_markdown=content,
                )
                self.status_changed.emit("审计完成，报告已保存")
                self._emit_progress(100, "审计完成")
            else:
                self.status_changed.emit("追问分析完成")
                self._emit_progress(100, "追问分析完成")

            self.completed.emit(
                {
                    "mode": self.mode,
                    "content": content,
                    "context": active_context,
                    "report": saved_report,
                }
            )
        except Exception as exc:
            self._emit_progress(self.progress_value, "任务失败")
            self.failed.emit(str(exc))
