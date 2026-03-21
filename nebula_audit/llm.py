from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List

import requests

from .config import AppConfig


class LLMClientError(RuntimeError):
    pass


StreamEventHandler = Callable[[dict], None]


def _emit_event(handler: StreamEventHandler | None, payload: dict) -> None:
    if handler is not None:
        handler(payload)


def _load_extra_headers(raw_text: str) -> Dict[str, str]:
    if not raw_text.strip():
        return {}
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"额外请求头 JSON 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise LLMClientError("额外请求头必须是 JSON 对象。")
    return {str(key): str(value) for key, value in payload.items()}


def _extract_openai_delta(choice: dict) -> str:
    delta = choice.get("delta", {})
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _decode_stream_line(raw_line) -> str:
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return str(raw_line)


def _iter_sse_payloads(response) -> Iterable[dict]:
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = _decode_stream_line(raw_line).strip()
        if line.startswith("event:"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        yield payload


def _messages_to_responses_input(messages: List[dict]) -> List[dict]:
    items = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        items.append(
            {
                "role": role,
                "content": [
                    {
                        "type": "input_text",
                        "text": content,
                    }
                ],
            }
        )
    return items


def _extract_responses_text_from_output(output: list[dict] | None) -> str:
    parts: list[str] = []
    for item in output or []:
        item_type = str(item.get("type", ""))
        if item_type == "message":
            for content in item.get("content") or []:
                content_type = str(content.get("type", ""))
                if content_type in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        elif item_type in {"output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _extract_responses_output_items(payload: dict) -> list[dict]:
    output = payload.get("output")
    if not isinstance(output, list):
        nested_response = payload.get("response")
        if isinstance(nested_response, dict):
            output = nested_response.get("output")
    if not isinstance(output, list):
        return []
    return [item for item in output if isinstance(item, dict)]


def _extract_responses_function_calls(payload: dict) -> list[dict]:
    output = _extract_responses_output_items(payload)
    return [item for item in output if item.get("type") == "function_call"]


def _extract_responses_text(payload: dict) -> str:
    direct_output = payload.get("output")
    if isinstance(direct_output, list):
        text = _extract_responses_text_from_output(direct_output)
        if text:
            return text

    nested_response = payload.get("response")
    if isinstance(nested_response, dict):
        nested_output = nested_response.get("output")
        if isinstance(nested_output, list):
            text = _extract_responses_text_from_output(nested_output)
            if text:
                return text

    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text

    return ""


def _extract_responses_text_from_completed(payload: dict) -> str:
    response = payload.get("response") or {}
    output = response.get("output") or []
    return _extract_responses_text_from_output(output)


def _ordered_output_items(items_by_index: dict[int, dict]) -> list[dict]:
    return [items_by_index[index] for index in sorted(items_by_index)]


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]", True


class OpenAICompatibleClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def stream_chat(
        self,
        messages: List[dict],
        *,
        workspace_dir: str | None = None,
        event_handler: StreamEventHandler | None = None,
    ) -> Iterable[str]:
        del workspace_dir, event_handler

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        headers.update(_load_extra_headers(self.config.extra_headers_json))

        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_output_tokens,
        }

        with requests.post(
            self.config.resolve_chat_url(),
            headers=headers,
            json=payload,
            stream=True,
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_ssl,
        ) as response:
            if not response.ok:
                raise LLMClientError(
                    f"模型请求失败（HTTP {response.status_code}）：{response.text[:1200]}"
                )

            for payload in _iter_sse_payloads(response):
                choices = payload.get("choices") or []
                if not choices:
                    continue
                chunk = _extract_openai_delta(choices[0])
                if chunk:
                    yield chunk


class ResponsesClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _build_headers(self, *, stream: bool) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        headers.update(_load_extra_headers(self.config.extra_headers_json))
        return headers

    def _base_payload(self, *, input_items: List[dict], stream: bool) -> dict:
        return {
            "model": self.config.model,
            "input": input_items,
            "stream": stream,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_output_tokens": self.config.max_output_tokens,
        }

    def _post_json(self, payload: dict) -> dict:
        with requests.post(
            self.config.resolve_chat_url(),
            headers=self._build_headers(stream=False),
            json=payload,
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_ssl,
        ) as response:
            if not response.ok:
                raise LLMClientError(
                    f"模型请求失败（HTTP {response.status_code}）：{response.text[:1200]}"
                )
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise LLMClientError(f"Responses API 返回了无法解析的 JSON：{exc}") from exc

    def stream_chat(
        self,
        messages: List[dict],
        *,
        workspace_dir: str | None = None,
        event_handler: StreamEventHandler | None = None,
    ) -> Iterable[str]:
        del workspace_dir, event_handler

        payload = self._base_payload(
            input_items=_messages_to_responses_input(messages),
            stream=True,
        )

        yielded_any = False
        completed_text = ""
        with requests.post(
            self.config.resolve_chat_url(),
            headers=self._build_headers(stream=True),
            json=payload,
            stream=True,
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_ssl,
        ) as response:
            if not response.ok:
                raise LLMClientError(
                    f"模型请求失败（HTTP {response.status_code}）：{response.text[:1200]}"
                )

            for payload in _iter_sse_payloads(response):
                event_type = payload.get("type", "")
                if event_type == "response.output_text.delta":
                    chunk = str(payload.get("delta", ""))
                    if chunk:
                        yielded_any = True
                        yield chunk
                    continue

                if event_type == "response.output_text.done":
                    if not completed_text:
                        completed_text = str(payload.get("text", ""))
                    continue

                if event_type == "response.completed":
                    completed_text = _extract_responses_text_from_completed(payload)
                    continue

                if event_type in {"response.failed", "error"}:
                    error = payload.get("error") or {}
                    message = error.get("message") or payload.get("message") or json.dumps(
                        payload, ensure_ascii=False
                    )
                    raise LLMClientError(str(message))

        if completed_text and not yielded_any:
            yield completed_text


class CodexResponsesClient(ResponsesClient):
    COMMAND_TOOL_NAME = "run_local_command"
    MAX_TOOL_ROUNDS = 240
    COMMAND_TIMEOUT_SECONDS = 90
    STDOUT_LIMIT = 12000
    STDERR_LIMIT = 6000
    BLOCKED_SNIPPETS = (
        "\n",
        "\r",
        "remove-item",
        "del ",
        "erase ",
        "rmdir",
        "rd ",
        "rm ",
        "set-content",
        "add-content",
        "clear-content",
        "out-file",
        "new-item",
        "copy-item",
        "move-item",
        "rename-item",
        "set-itemproperty",
        "invoke-expression",
        "iex ",
        "start-process",
        "invoke-item",
        "stop-process",
        "shutdown",
        "restart-computer",
        "stop-computer",
        "format-",
        "taskkill",
        "git reset",
        "git checkout",
        "git clean",
    )
    SAFE_REDIRECTION_TARGETS = {"$null", "null", "nul", "&1", "&2"}
    REDIRECTION_PATTERN = re.compile(
        r"(?:(?<=^)|(?<=[\s)\]}]))(?P<operator>(?:\d+|\*)?>>|(?:\d+|\*)?>)\s*(?P<target>[^\s]+)",
        re.IGNORECASE,
    )

    def _command_tool_schema(self) -> dict:
        return {
            "type": "function",
            "name": self.COMMAND_TOOL_NAME,
            "description": (
                "Run a PowerShell command inside the current project workspace to inspect files, "
                "search code, or fetch web evidence. Network requests are allowed, but the command "
                "must not modify or delete local files, save downloaded content locally, or start "
                "other programs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The PowerShell command to run. Prefer rg, Get-ChildItem, "
                            "Get-Content and Select-String. Use curl, wget or "
                            "Invoke-WebRequest only when you do not write the response to disk."
                        ),
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Optional relative subdirectory inside the current project workspace.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "A short explanation of why this command is needed.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        }

    def _resolve_workdir(self, workspace_dir: str, requested_workdir: str) -> Path:
        base_dir = Path(workspace_dir).resolve()
        if not base_dir.exists() or not base_dir.is_dir():
            raise LLMClientError(f"本地命令工作目录不存在：{base_dir}")

        if not requested_workdir.strip():
            return base_dir

        requested_path = Path(requested_workdir.strip())
        resolved = (base_dir / requested_path).resolve()
        if not resolved.is_relative_to(base_dir):
            raise LLMClientError("本地命令只能在当前项目工作目录内执行。")
        if not resolved.exists() or not resolved.is_dir():
            raise LLMClientError(f"指定的工作目录不存在：{resolved}")
        return resolved

    def _validate_redirection_target(self, command: str) -> str | None:
        for match in self.REDIRECTION_PATTERN.finditer(command):
            target = match.group("target").strip().rstrip(";,)")
            normalized = target.strip("'\"").lower()
            if normalized in self.SAFE_REDIRECTION_TARGETS:
                continue
            return "允许使用重定向，但只能重定向到 `$null`、`NUL` 或 `&1`/`&2` 这类流目标，不能写入本地文件。"
        return None

    def _validate_download_output(self, command: str) -> str | None:
        if re.search(r"(?<![\w-])invoke-webrequest\b.*?\s-outfile(?:\s|:)", command, re.IGNORECASE):
            return "允许联网请求，但不能把下载结果通过 `-OutFile` 写入本地文件。"
        if re.search(r"(?<![\w-])(?:curl|wget)\b.*?(?:\s-O\b|\s--remote-name\b)", command, re.IGNORECASE):
            return "允许联网请求，但不能使用 `-O` 或 `--remote-name` 把结果保存到本地文件。"

        output_match = re.search(
            r"(?<![\w-])(?:curl|wget)\b.*?(?:\s-o\s*(?P<short_target>[^\s]+)|\s--output\s+(?P<long_target>[^\s]+))",
            command,
            re.IGNORECASE,
        )
        if output_match:
            target = output_match.group("short_target") or output_match.group("long_target") or ""
            if target.strip().strip("'\"") != "-":
                return "允许联网请求，但 `-o`/`--output` 只能输出到标准输出 `-`，不能写入本地文件。"
        return None

    def _validate_command(self, command: str) -> str | None:
        lowered = command.lower()
        for snippet in self.BLOCKED_SNIPPETS:
            if snippet in lowered:
                return (
                    "当前 Codex 模式允许多条只读/联网取证命令，但仍禁止删除、改写本地文件、"
                    "保存下载结果到磁盘或启动其他程序。"
                )
        redirection_error = self._validate_redirection_target(command)
        if redirection_error:
            return redirection_error
        download_error = self._validate_download_output(lowered)
        if download_error:
            return download_error
        return None

    def _parse_tool_arguments(self, raw_arguments) -> dict:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str):
            if not raw_arguments.strip():
                return {}
            try:
                payload = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMClientError(f"模型返回的工具参数不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise LLMClientError("模型返回的工具参数必须是 JSON 对象。")
            return payload
        raise LLMClientError("模型返回了无法识别的工具参数格式。")

    def _execute_local_command(
        self,
        *,
        command: str,
        workdir: Path,
        reason: str,
        event_handler: StreamEventHandler | None,
    ) -> dict:
        validation_error = self._validate_command(command)
        if validation_error:
            _emit_event(
                event_handler,
                {
                    "type": "tool_call_rejected",
                    "tool": self.COMMAND_TOOL_NAME,
                    "command": command,
                    "cwd": str(workdir),
                    "reason": reason,
                    "message": validation_error,
                },
            )
            return {
                "ok": False,
                "exit_code": None,
                "cwd": str(workdir),
                "command": command,
                "reason": reason,
                "stdout": "",
                "stderr": validation_error,
            }

        _emit_event(
            event_handler,
            {
                "type": "tool_call_started",
                "tool": self.COMMAND_TOOL_NAME,
                "command": command,
                "cwd": str(workdir),
                "reason": reason,
            },
        )

        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(self.COMMAND_TIMEOUT_SECONDS, self.config.timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            stdout, stdout_truncated = _truncate_text(str(stdout), self.STDOUT_LIMIT)
            stderr, stderr_truncated = _truncate_text(str(stderr), self.STDERR_LIMIT)
            result = {
                "ok": False,
                "exit_code": None,
                "timed_out": True,
                "cwd": str(workdir),
                "command": command,
                "reason": reason,
                "stdout": stdout,
                "stderr": stderr or f"命令执行超时（>{self.COMMAND_TIMEOUT_SECONDS}s）。",
                "truncated": stdout_truncated or stderr_truncated,
            }
            _emit_event(
                event_handler,
                {
                    "type": "tool_call_completed",
                    "tool": self.COMMAND_TOOL_NAME,
                    "command": command,
                    "cwd": str(workdir),
                    "exit_code": None,
                    "timed_out": True,
                },
            )
            return result

        stdout, stdout_truncated = _truncate_text(completed.stdout or "", self.STDOUT_LIMIT)
        stderr, stderr_truncated = _truncate_text(completed.stderr or "", self.STDERR_LIMIT)
        result = {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "cwd": str(workdir),
            "command": command,
            "reason": reason,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }
        _emit_event(
            event_handler,
            {
                "type": "tool_call_completed",
                "tool": self.COMMAND_TOOL_NAME,
                "command": command,
                "cwd": str(workdir),
                "exit_code": completed.returncode,
                "timed_out": False,
            },
        )
        return result

    def _build_tool_outputs(
        self,
        function_calls: list[dict],
        *,
        workspace_dir: str,
        event_handler: StreamEventHandler | None,
    ) -> list[dict]:
        tool_outputs: list[dict] = []
        for function_call in function_calls:
            call_id = str(function_call.get("call_id") or "").strip()
            if not call_id:
                raise LLMClientError("模型返回了缺少 call_id 的工具调用，无法继续。")

            tool_name = str(function_call.get("name") or "")
            if tool_name != self.COMMAND_TOOL_NAME:
                result = {
                    "ok": False,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": f"不支持的工具调用：{tool_name}",
                }
            else:
                arguments = self._parse_tool_arguments(function_call.get("arguments"))
                command = str(arguments.get("command", "")).strip()
                requested_workdir = str(arguments.get("workdir", "")).strip()
                reason = str(arguments.get("reason", "")).strip()
                if not command:
                    result = {
                        "ok": False,
                        "exit_code": None,
                        "stdout": "",
                        "stderr": "工具调用缺少 command 参数。",
                    }
                else:
                    workdir = self._resolve_workdir(workspace_dir, requested_workdir)
                    result = self._execute_local_command(
                        command=command,
                        workdir=workdir,
                        reason=reason,
                        event_handler=event_handler,
                    )

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )
        return tool_outputs

    def _stream_response_round(self, payload: dict) -> Iterable[str]:
        yielded_any = False
        completed_text = ""
        completed_output_items: list[dict] = []
        items_by_index: dict[int, dict] = {}

        with requests.post(
            self.config.resolve_chat_url(),
            headers=self._build_headers(stream=True),
            json=payload,
            stream=True,
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_ssl,
        ) as response:
            if not response.ok:
                raise LLMClientError(
                    f"模型请求失败（HTTP {response.status_code}）：{response.text[:1200]}"
                )

            for event in _iter_sse_payloads(response):
                event_type = str(event.get("type", ""))

                if event_type == "response.output_text.delta":
                    chunk = str(event.get("delta", ""))
                    if chunk:
                        yielded_any = True
                        yield chunk
                    continue

                if event_type == "response.output_text.done":
                    if not completed_text:
                        completed_text = str(event.get("text", ""))
                    continue

                if event_type == "response.output_item.added":
                    output_index = event.get("output_index")
                    item = event.get("item")
                    if isinstance(output_index, int) and isinstance(item, dict):
                        items_by_index[output_index] = dict(item)
                    continue

                if event_type == "response.output_item.done":
                    output_index = event.get("output_index")
                    item = event.get("item")
                    if isinstance(output_index, int) and isinstance(item, dict):
                        items_by_index[output_index] = dict(item)
                    continue

                if event_type == "response.function_call_arguments.delta":
                    output_index = event.get("output_index")
                    if not isinstance(output_index, int):
                        continue
                    item = items_by_index.setdefault(
                        output_index,
                        {
                            "type": "function_call",
                            "id": str(event.get("item_id", "")),
                            "call_id": str(event.get("call_id", "")),
                            "name": str(event.get("name", "")),
                            "arguments": "",
                        },
                    )
                    item["type"] = "function_call"
                    if event.get("item_id"):
                        item["id"] = str(event.get("item_id"))
                    if event.get("call_id"):
                        item["call_id"] = str(event.get("call_id"))
                    if event.get("name"):
                        item["name"] = str(event.get("name"))
                    item["arguments"] = f"{item.get('arguments', '')}{event.get('delta', '')}"
                    continue

                if event_type == "response.function_call_arguments.done":
                    output_index = event.get("output_index")
                    item = event.get("item")
                    if isinstance(output_index, int) and isinstance(item, dict):
                        items_by_index[output_index] = dict(item)
                        continue
                    if not isinstance(output_index, int):
                        continue
                    current = items_by_index.setdefault(
                        output_index,
                        {
                            "type": "function_call",
                            "id": str(event.get("item_id", "")),
                            "call_id": str(event.get("call_id", "")),
                            "name": str(event.get("name", "")),
                            "arguments": "",
                        },
                    )
                    current["type"] = "function_call"
                    if event.get("item_id"):
                        current["id"] = str(event.get("item_id"))
                    if event.get("call_id"):
                        current["call_id"] = str(event.get("call_id"))
                    if event.get("name"):
                        current["name"] = str(event.get("name"))
                    if event.get("arguments") is not None:
                        current["arguments"] = str(event.get("arguments"))
                    continue

                if event_type == "response.completed":
                    completed_text = _extract_responses_text_from_completed(event)
                    completed_output_items = _extract_responses_output_items(event)
                    continue

                if event_type in {"response.failed", "error"}:
                    error = event.get("error") or {}
                    message = error.get("message") or event.get("message") or json.dumps(
                        event, ensure_ascii=False
                    )
                    raise LLMClientError(str(message))

        output_items = completed_output_items or _ordered_output_items(items_by_index)
        if completed_text and not yielded_any:
            yield completed_text
        return {
            "output_items": output_items,
            "function_calls": [
                item for item in output_items if isinstance(item, dict) and item.get("type") == "function_call"
            ],
        }

    def stream_chat(
        self,
        messages: List[dict],
        *,
        workspace_dir: str | None = None,
        event_handler: StreamEventHandler | None = None,
    ) -> Iterable[str]:
        if not workspace_dir:
            yield from super().stream_chat(
                messages,
                workspace_dir=workspace_dir,
                event_handler=event_handler,
            )
            return

        conversation_items = _messages_to_responses_input(messages)

        for _round in range(self.MAX_TOOL_ROUNDS):
            payload = self._base_payload(input_items=conversation_items, stream=True)
            payload["tools"] = [self._command_tool_schema()]
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False

            round_result = yield from self._stream_response_round(payload)
            response_output_items = list(round_result.get("output_items") or [])
            function_calls = list(round_result.get("function_calls") or [])

            if not function_calls:
                return

            tool_outputs = self._build_tool_outputs(
                function_calls,
                workspace_dir=workspace_dir,
                event_handler=event_handler,
            )
            conversation_items = [*conversation_items, *response_output_items, *tool_outputs]

        _emit_event(
            event_handler,
            {
                "type": "tool_round_limit_reached",
                "tool": self.COMMAND_TOOL_NAME,
                "message": "已达到本地命令轮次上限，开始基于现有证据输出最终结论。",
            },
        )
        final_payload = self._base_payload(
            input_items=[
                *conversation_items,
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Tool use has reached the limit. Do not call any more tools. "
                                "Based only on the existing tool results and the provided source snapshot, "
                                "produce the best final answer now."
                            ),
                        }
                    ],
                },
            ],
            stream=True,
        )
        yield from self._stream_response_round(final_payload)


class OllamaClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def stream_chat(
        self,
        messages: List[dict],
        *,
        workspace_dir: str | None = None,
        event_handler: StreamEventHandler | None = None,
    ) -> Iterable[str]:
        del workspace_dir, event_handler

        headers = {"Content-Type": "application/json"}
        headers.update(_load_extra_headers(self.config.extra_headers_json))
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
                "num_predict": self.config.max_output_tokens,
            },
        }

        with requests.post(
            self.config.resolve_chat_url(),
            headers=headers,
            json=payload,
            stream=True,
            timeout=self.config.timeout_seconds,
            verify=self.config.verify_ssl,
        ) as response:
            if not response.ok:
                raise LLMClientError(
                    f"模型请求失败（HTTP {response.status_code}）：{response.text[:1200]}"
                )

            for raw_line in response.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                try:
                    payload = json.loads(_decode_stream_line(raw_line))
                except json.JSONDecodeError:
                    continue
                message = payload.get("message") or {}
                chunk = message.get("content", "")
                if chunk:
                    yield chunk


def create_streaming_client(
    config: AppConfig,
) -> OpenAICompatibleClient | ResponsesClient | CodexResponsesClient | OllamaClient:
    if config.transport == "responses":
        if config.enable_codex_mode:
            return CodexResponsesClient(config)
        return ResponsesClient(config)
    if config.transport == "ollama":
        return OllamaClient(config)
    return OpenAICompatibleClient(config)
