"""Dedicated coding-agent loop using the plugin model gateway."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from .agent_tools import get_tool_specs
from .permissions import command_requires_approval
from .protocol import BridgeError
from .prompts import SYSTEM_PROMPT, build_context_message
from .server import BridgeConnection
from .settings import BridgeSettings
from .tasks import TaskRecord


class AgentError(BridgeError):
    pass


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _tool_calls(message: Any) -> list[dict[str, str]]:
    raw_calls = _value(message, "tool_calls", None)
    if not raw_calls:
        return []
    calls: list[dict[str, str]] = []
    for raw in raw_calls:
        function = _value(raw, "function", None)
        calls.append({
            "id": str(_value(raw, "id", "") or f"call_{uuid.uuid4().hex[:8]}"),
            "name": str(_value(function, "name", "") or ""),
            "arguments": str(_value(function, "arguments", "{}") or "{}"),
        })
    return calls


def _assistant_message(content: Any, calls: list[dict[str, str]]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content if isinstance(content, str) and content else None,
    }
    if calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call["arguments"],
                },
            }
            for call in calls
        ]
    return message


class CodingAgent:
    def __init__(self, *, settings: BridgeSettings, logger: Any):
        self.settings = settings
        self.logger = logger

    async def run(
        self,
        *,
        connection: BridgeConnection,
        task: TaskRecord,
        model_client: Any,
        request: str,
        on_progress: Callable[[TaskRecord], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        await self._notify(task, on_progress, "preparing", "读取工作区与上下文")
        workspace_info = await self._try_request(connection, "workspace.info", {}, 20.0)
        active_editor = await self._try_request(connection, "workspace.activeEditor", {}, 20.0)
        diagnostics = await self._try_request(
            connection,
            "diagnostics.get",
            {"severity": "error", "maxResults": 20},
            20.0,
        )
        project_files = await self._load_project_files(connection)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": build_context_message(
                    request=request,
                    workspace_info=workspace_info if isinstance(workspace_info, dict) else None,
                    active_editor=active_editor if isinstance(active_editor, dict) else None,
                    diagnostics=diagnostics if isinstance(diagnostics, dict) else None,
                    project_files=project_files,
                ),
            },
            {"role": "user", "content": request},
        ]

        files_changed: list[str] = []
        last_diagnostics: dict[str, Any] = {}
        last_text = ""
        finish_result: dict[str, Any] | None = None
        repair_rounds = 0
        tools = get_tool_specs()
        step = 0
        stopped_by_limit = False

        while True:
            step += 1
            if task.cancel_event.is_set():
                raise asyncio.CancelledError
            if (
                self.settings.task_timeout_seconds > 0
                and time.time() - task.started_at > self.settings.task_timeout_seconds
            ):
                raise AgentError("MODEL_TIMEOUT", "Coding 任务超过时间预算")
            if task.max_steps > 0 and step > task.max_steps:
                task.set_phase("stopped", "达到配置的步数上限")
                if on_progress is not None:
                    try:
                        await on_progress(task)
                    except Exception:
                        pass
                stopped_by_limit = True
                break

            task.step = step
            task.status = "running"
            await self._notify(
                task,
                on_progress,
                "thinking",
                f"第 {step} 步：模型正在思考",
            )
            self._trim_messages(messages)

            try:
                request_kwargs: dict[str, Any] = {
                    "model": "coding",
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_completion_tokens": 8192,
                    "temperature": 0.2,
                }
                if self.settings.model_timeout_seconds > 0:
                    request_kwargs["timeout"] = self.settings.model_timeout_seconds
                response = await model_client.chat.completions.create(**request_kwargs)
            except asyncio.TimeoutError as exc:
                raise AgentError("MODEL_TIMEOUT", "Coding 模型请求超时") from exc
            except Exception as exc:
                raise self._map_model_error(exc) from exc

            choices = _value(response, "choices", None)
            if not choices:
                raise AgentError("MODEL_ERROR", "Coding 模型没有返回结果")
            message = _value(choices[0], "message", None)
            content = _value(message, "content", None)
            calls = _tool_calls(message)

            if not calls:
                last_text = content if isinstance(content, str) and content else "完成"
                await self._notify(task, on_progress, "finishing", "模型已完成，整理结果")
                break

            await self._notify(
                task,
                on_progress,
                "tool",
                f"第 {step} 步：准备执行 {len(calls)} 个工具",
            )
            messages.append(_assistant_message(content, calls))
            repair_diagnostics: dict[str, Any] | None = None

            for call in calls:
                name = call["name"]
                arguments: dict[str, Any] = {}
                result: dict[str, Any]
                try:
                    parsed = json.loads(call["arguments"])
                    if not isinstance(parsed, dict):
                        raise ValueError("tool arguments must be an object")
                    arguments = parsed
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    result = {
                        "is_error": True,
                        "error": "INVALID_TOOL_ARGUMENTS",
                        "message": str(exc),
                    }
                else:
                    await self._notify(
                        task,
                        on_progress,
                        "tool",
                        f"调用 {name}",
                        tool=name,
                        detail=self._tool_detail(name, arguments),
                    )
                    try:
                        result = await self._execute_tool(
                            connection=connection,
                            task=task,
                            name=name,
                            arguments=arguments,
                        )
                    except asyncio.CancelledError:
                        raise
                    except BridgeError as exc:
                        result = {
                            "is_error": True,
                            "error": exc.code,
                            "message": exc.message,
                        }
                    except Exception as exc:
                        result = {
                            "is_error": True,
                            "error": "INTERNAL_ERROR",
                            "message": str(exc),
                        }

                if name == "finish_task" and isinstance(result, dict):
                    finish_result = result
                if isinstance(result, dict):
                    for changed in result.get("files_changed") or []:
                        if isinstance(changed, str) and changed not in files_changed:
                            files_changed.append(changed)
                    if isinstance(result.get("diagnostics"), dict):
                        last_diagnostics = result["diagnostics"]

                ok = not (isinstance(result, dict) and result.get("is_error"))
                await self._notify(
                    task,
                    on_progress,
                    "tool_result",
                    f"{name} {'完成' if ok else '失败'}",
                    tool=name,
                    detail=self._result_detail(result),
                    ok=ok,
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

                if (
                    isinstance(result, dict)
                    and result.get("diagnostics")
                    and isinstance(result["diagnostics"], dict)
                    and result["diagnostics"].get("errors", 0) > 0
                    and repair_rounds < self.settings.max_repair_rounds
                ):
                    repair_diagnostics = result["diagnostics"]

            if repair_diagnostics is not None and repair_rounds < self.settings.max_repair_rounds:
                repair_rounds += 1
                await self._notify(task, on_progress, "repairing", "根据 diagnostics 修复问题")
                messages.append({
                    "role": "user",
                    "content": (
                        "Diagnostics reported errors after the edit. "
                        "Read the affected files and fix them before finishing:\n"
                        + json.dumps(repair_diagnostics, ensure_ascii=False)
                    ),
                })

            if finish_result is not None:
                break

        if finish_result is not None:
            summary = str(finish_result.get("summary") or last_text or "完成")
            for changed in finish_result.get("files_changed") or []:
                if isinstance(changed, str) and changed not in files_changed:
                    files_changed.append(changed)
            return {
                "summary": summary,
                "files_changed": files_changed,
                "diagnostics": last_diagnostics,
                "tests": str(finish_result.get("tests") or ""),
                "remaining_issues": str(finish_result.get("remaining_issues") or ""),
            }

        if stopped_by_limit:
            summary = last_text or "达到配置的步数上限，任务已停止"
        else:
            summary = last_text or "已完成"
        return {
            "summary": summary,
            "files_changed": files_changed,
            "diagnostics": last_diagnostics,
            "tests": "",
            "remaining_issues": "",
        }

    async def _notify(
        self,
        task: TaskRecord,
        on_progress: Callable[[TaskRecord], Awaitable[None]] | None,
        phase: str,
        message: str,
        *,
        tool: str = "",
        detail: str = "",
        ok: bool | None = None,
    ) -> None:
        task.set_phase(phase, message, tool=tool, detail=detail, ok=ok)
        if on_progress is not None:
            try:
                await on_progress(task)
            except Exception:
                pass

    @staticmethod
    def _tool_detail(name: str, arguments: dict[str, Any]) -> str:
        if name in {"read_file", "apply_edits", "create_file", "delete_file", "rename_file"}:
            return str(arguments.get("path") or arguments.get("from_path") or "")
        if name == "run_command":
            command = str(arguments.get("command") or "")
            args = " ".join(str(item) for item in (arguments.get("args") or []))
            return f"{command} {args}".strip()
        if name == "search_text":
            return str(arguments.get("query") or "")
        if name == "list_files":
            return str(arguments.get("glob") or "")
        if name == "finish_task":
            return str(arguments.get("summary") or "")
        return ""

    @staticmethod
    def _result_detail(result: Any) -> str:
        if not isinstance(result, dict):
            return str(result)[:300]
        if result.get("is_error"):
            return f"{result.get('error', 'ERROR')}: {result.get('message', '')}"[:300]
        parts: list[str] = []
        if result.get("status"):
            parts.append(f"status={result['status']}")
        if result.get("exit_code") is not None:
            parts.append(f"exit={result['exit_code']}")
        changed = result.get("files_changed")
        if isinstance(changed, list) and changed:
            parts.append("files=" + ", ".join(str(item) for item in changed[:6]))
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict):
            parts.append(
                f"diagnostics={diagnostics.get('errors', 0)} errors/"
                f"{diagnostics.get('warnings', 0)} warnings"
            )
        return ("; ".join(parts) or "ok")[:300]

    async def _load_project_files(self, connection: BridgeConnection) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in ("AGENTS.md", "CLAUDE.md", ".editorconfig"):
            value = await self._try_request(
                connection,
                "file.read",
                {"path": path, "maxChars": 20000},
                10.0,
            )
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                result[path] = value["text"]
        return result

    async def _try_request(
        self,
        connection: BridgeConnection,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> Any:
        try:
            return await connection.request(method, params, timeout=timeout)
        except Exception:
            return None

    async def _execute_tool(
        self,
        *,
        connection: BridgeConnection,
        task: TaskRecord,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "workspace_info":
            return await connection.request("workspace.info", timeout=20.0)
        if name == "list_files":
            return await connection.request(
                "workspace.listFiles",
                {
                    "glob": arguments.get("glob") or "**/*",
                    "exclude": arguments.get("exclude") or [],
                    "maxResults": arguments.get("max_results") or 5000,
                },
                timeout=30.0,
            )
        if name == "read_file":
            return await connection.request(
                "file.read",
                {
                    "path": arguments.get("path"),
                    "startLine": arguments.get("start_line"),
                    "endLine": arguments.get("end_line"),
                    "maxChars": arguments.get("max_chars") or 200000,
                },
                timeout=30.0,
            )
        if name == "search_text":
            return await connection.request(
                "search.text",
                {
                    "query": arguments.get("query"),
                    "regex": bool(arguments.get("regex")),
                    "include": arguments.get("include"),
                    "exclude": arguments.get("exclude") or [],
                    "maxResults": arguments.get("max_results") or 200,
                    "contextLines": arguments.get("context_lines") or 1,
                },
                timeout=60.0,
            )
        if name == "active_editor":
            return await connection.request("workspace.activeEditor", timeout=20.0)
        if name == "get_diagnostics":
            return await connection.request(
                "diagnostics.get",
                {
                    "path": arguments.get("path"),
                    "severity": arguments.get("severity") or "all",
                    "maxResults": arguments.get("max_results") or 100,
                },
                timeout=30.0,
            )
        if name == "apply_edits":
            return await self._apply_edits(connection, task, arguments)
        if name == "create_file":
            return await self._propose_change(
                connection,
                task,
                operation="create",
                path=str(arguments.get("path") or ""),
                new_text=str(arguments.get("content") or ""),
                summary=str(arguments.get("summary") or ""),
            )
        if name == "delete_file":
            return await self._propose_change(
                connection,
                task,
                operation="delete",
                path=str(arguments.get("path") or ""),
                base_hash=str(arguments.get("base_hash") or ""),
                summary=str(arguments.get("summary") or ""),
            )
        if name == "rename_file":
            return await self._propose_change(
                connection,
                task,
                operation="rename",
                path=str(arguments.get("to_path") or ""),
                from_path=str(arguments.get("from_path") or ""),
                summary=str(arguments.get("summary") or ""),
            )
        if name == "run_command":
            if not self.settings.allow_terminal:
                return {
                    "is_error": True,
                    "error": "COMMAND_DENIED",
                    "message": "插件设置中已禁用终端命令",
                }
            timeout_seconds = min(
                int(arguments.get("timeout_seconds") or self.settings.command_timeout_seconds),
                self.settings.command_timeout_seconds,
            )
            command = str(arguments.get("command") or "")
            command_args = [str(item) for item in (arguments.get("args") or [])]
            require_approval = command_requires_approval(
                task.permission_mode, command, command_args
            )
            if require_approval:
                task.status = "waiting_approval"
                task.set_phase(
                    "waiting_approval",
                    "等待在 VSCode 中确认命令",
                    tool="run_command",
                    detail=command,
                )
            try:
                return await connection.request(
                    "command.run",
                    {
                        "command": command,
                        "args": command_args,
                        "cwd": arguments.get("cwd"),
                        "timeoutSeconds": timeout_seconds,
                        "reason": arguments.get("reason") or "",
                        "requireApproval": require_approval,
                    },
                    timeout=timeout_seconds + 30.0,
                )
            finally:
                if require_approval:
                    task.status = "running"
        if name == "finish_task":
            return {
                "ok": True,
                "summary": str(arguments.get("summary") or "完成"),
                "files_changed": arguments.get("files_changed") or [],
                "tests": str(arguments.get("tests") or ""),
                "remaining_issues": str(arguments.get("remaining_issues") or ""),
            }
        return {"is_error": True, "error": "UNKNOWN_TOOL", "message": name}

    async def _apply_edits(
        self,
        connection: BridgeConnection,
        task: TaskRecord,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        path = str(arguments.get("path") or "")
        base_hash = str(arguments.get("base_hash") or "")
        edits = arguments.get("edits")
        if not path or not base_hash or not isinstance(edits, list) or not edits:
            return {
                "is_error": True,
                "error": "INVALID_TOOL_ARGUMENTS",
                "message": "path, base_hash and a non-empty edits list are required",
            }

        current = await connection.request(
            "file.read",
            {"path": path, "maxChars": 2_000_000},
            timeout=30.0,
        )
        if current.get("binary"):
            return {"is_error": True, "error": "BINARY_FILE", "message": path}
        if current.get("truncated"):
            return {
                "is_error": True,
                "error": "FILE_TOO_LARGE",
                "message": "请先读取完整文件再修改",
            }
        if current.get("hash") != base_hash:
            return {
                "is_error": True,
                "error": "STALE_FILE",
                "message": "base_hash does not match the current file",
                "current_hash": current.get("hash"),
            }

        text = current.get("text")
        if not isinstance(text, str):
            return {"is_error": True, "error": "FILE_NOT_FOUND", "message": path}

        for edit in edits:
            if not isinstance(edit, dict):
                return {"is_error": True, "error": "INVALID_TOOL_ARGUMENTS", "message": "edit must be an object"}
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
                return {
                    "is_error": True,
                    "error": "INVALID_TOOL_ARGUMENTS",
                    "message": "old_text and new_text must be strings",
                }
            if old_text not in text:
                return {
                    "is_error": True,
                    "error": "EDIT_MISMATCH",
                    "message": "old_text was not found in the current file",
                }
            text = text.replace(old_text, new_text, 1)

        result = await self._propose_change(
            connection,
            task,
            operation="update",
            path=path,
            base_hash=base_hash,
            new_text=text,
            summary=str(arguments.get("summary") or ""),
        )
        if isinstance(result, dict):
            files = result.get("files_changed")
            if not isinstance(files, list):
                result["files_changed"] = [path]
        return result

    async def _propose_change(
        self,
        connection: BridgeConnection,
        task: TaskRecord,
        *,
        operation: str,
        path: str,
        base_hash: str = "",
        new_text: str = "",
        summary: str = "",
        from_path: str = "",
    ) -> dict[str, Any]:
        if not path:
            return {"is_error": True, "error": "INVALID_TOOL_ARGUMENTS", "message": "path is required"}

        item: dict[str, Any] = {
            "path": path,
            "operation": operation,
            "summary": summary,
        }
        if operation in ("update", "delete"):
            item["baseHash"] = base_hash
        if operation in ("update", "create"):
            item["newText"] = new_text
        if operation == "rename":
            item["fromPath"] = from_path

        permission = task.permission_mode
        if permission in ("diff", "readonly"):
            permission = "ask"
        elif permission == "auto":
            permission = "auto_edit"
        if permission not in ("ask", "auto_edit", "full_access"):
            permission = "ask"

        change_set_id = f"cs_{uuid.uuid4().hex[:12]}"
        wait = permission == "ask"
        params = {
            "changeSetId": change_set_id,
            "summary": summary,
            "wait": wait,
            "waitMs": self.settings.approval_timeout_seconds * 1000,
            "autoApply": permission != "ask",
            "silentDecision": True,
            "files": [item],
        }
        timeout = (self.settings.approval_timeout_seconds + 30.0) if wait else 30.0
        if wait:
            task.status = "waiting_approval"
            task.set_phase(
                "waiting_approval",
                "等待在 VSCode 中审阅改动",
                tool="changes.propose",
                detail=path,
            )
        try:
            result = await connection.request("changes.propose", params, timeout=timeout)
        finally:
            if wait:
                task.status = "running"
        if isinstance(result, dict):
            status = result.get("status")
            if status == "applied" and operation != "delete":
                if path not in task.files_changed:
                    task.files_changed.append(path)
            if status in ("rejected", "stale", "failed"):
                result.setdefault("is_error", True)
                result.setdefault(
                    "error",
                    "APPROVAL_REJECTED" if status == "rejected"
                    else ("STALE_FILE" if status == "stale" else "INTERNAL_ERROR"),
                )
            elif status == "pending":
                result.setdefault(
                    "message",
                    "Waiting for the user to apply or reject the change in VSCode.",
                )
        return result if isinstance(result, dict) else {"status": "unknown"}

    def _map_model_error(self, exc: Exception) -> AgentError:
        body = getattr(exc, "body", None)
        code = None
        if isinstance(body, dict):
            code = body.get("code")
            if not code and isinstance(body.get("error"), dict):
                code = body["error"].get("code")
        text = f"{code or ''} {exc}".lower()
        if "model_usage_not_bound" in text or "model_not_bound" in text:
            return AgentError("MODEL_NOT_BOUND", "请先给插件绑定 Coding 模型")
        if "model_capability_mismatch" in text or "unsupported_capability" in text or "tool_calling" in text:
            return AgentError(
                "MODEL_CAPABILITY_MISMATCH",
                "绑定的 Coding 模型不支持工具调用",
            )
        if "timeout" in text:
            return AgentError("MODEL_TIMEOUT", "Coding model request timed out")
        return AgentError("MODEL_ERROR", f"Coding 模型请求失败：{exc}")

    def _trim_messages(self, messages: list[dict[str, Any]]) -> None:
        limit = self.settings.max_context_chars
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and len(content) > 20000:
                message["content"] = content[:20000] + "\n...[truncated]"

        def total_chars() -> int:
            total = 0
            for message in messages:
                total += len(str(message.get("content") or ""))
                total += len(str(message.get("tool_calls") or ""))
            return total

        if total_chars() <= limit:
            return

        # Keep the last few messages intact and shrink older context without
        # breaking assistant tool_calls -> tool result pairing.
        for index, message in enumerate(messages):
            if index < 2 or index >= len(messages) - 4:
                continue
            if message.get("role") == "assistant" and message.get("tool_calls"):
                message["content"] = None
            else:
                message["content"] = "[older context trimmed]"
