"""N.E.K.O. VSCode bridge plugin."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    message,
    neko_plugin,
    plugin_entry,
    tr,
    ui,
)

from .bridge.agent import AgentError, CodingAgent
from .bridge.intent import claim_looks_like_file_change, looks_like_coding_request
from .bridge.model_status import ModelStatusProvider
from .bridge.permissions import command_requires_approval
from .bridge.protocol import PLUGIN_ID, BridgeError
from .bridge.server import BridgeServer
from .bridge.settings import BridgeSettings
from .bridge.tasks import TaskManager
from .bridge.workspace import WorkspaceConnection


_CODING_RULE = (
    "[VSCode 桥接规则] 当用户要求读取、修改、创建、删除工作区文件或运行命令时："
    "必须先调用 vscode_status，然后调用对应的 vscode_* 工具（vscode_coding_task 或 "
    "vscode_read_file / vscode_apply_edits / vscode_run_command）。"
    "在工具返回 applied/result 之前，禁止说“已修改/已写入/已完成/文件已更新”。"
    "如果本轮没有调用任何 vscode_* 工具，必须明确说“还没有实际修改文件”，不要凭想象描述文件内容。"
)

_SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{8,}\b"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b"), "github_pat_***"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "AKIA***"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE), "Bearer ***"),
]


def _redact_text(value: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def _workspace_param() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "Optional workspace id. Defaults to the focused VSCode window.",
    }


@neko_plugin
class VSCodeBridgePlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = getattr(self, "logger", None) or getattr(ctx, "logger", None)
        self._settings = BridgeSettings()
        self._tasks = TaskManager()
        self._model_status = ModelStatusProvider()
        self._dynamic_tool_names: set[str] = set()
        self._base_tools_ensured = False
        self._tool_loop_task: asyncio.Task[Any] | None = None
        self._tool_loop_loop: asyncio.AbstractEventLoop | None = None
        self._server_autostart_blocked = False
        self._handled_message_ids: set[str] = set()
        self._last_user_request_at = 0.0
        self._last_user_request = ""
        self._last_real_action_at = 0.0
        self._agent = CodingAgent(settings=self._settings, logger=self.logger)
        self._server = self._build_server()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @lifecycle(id="startup")
    async def startup(self, **_):
        try:
            config = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("Could not load plugin configuration: {}", exc)
            config = {}
        section = config.get("vscode_bridge") if isinstance(config, dict) else None
        try:
            self._settings = BridgeSettings.from_config(section)
        except Exception as exc:
            self.logger.warning("Invalid VSCode bridge settings, using defaults: {}", exc)
            self._settings = BridgeSettings()
        self._agent = CodingAgent(settings=self._settings, logger=self.logger)
        self._server = self._build_server()
        self._tool_loop_task = None
        self._tool_loop_loop = None
        await self._sync_llm_tools()
        self.logger.info("VSCode bridge prepared; server starts on first panel/tool use")
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        current_loop = asyncio.get_running_loop()
        if self._tool_loop_task is not None:
            self._tool_loop_task.cancel()
            if self._tool_loop_loop is current_loop:
                try:
                    await self._tool_loop_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._tool_loop_task = None
            self._tool_loop_loop = None
        await self._tasks.cancel_all()
        if self._server.running and self._server.loop is not current_loop:
            self._server.force_close()
        else:
            await self._server.stop()
        self._unregister_dynamic_tools()
        self.logger.info("VSCode bridge stopped")
        return Ok({"status": "shutdown"})

    def _build_server(self) -> BridgeServer:
        fallback = None
        try:
            fallback = str(self.data_path("discovery"))
        except Exception:
            pass
        version = "0.1.3"
        try:
            metadata = getattr(self, "metadata", None)
            if isinstance(metadata, dict):
                version = str(metadata.get("version") or version)
        except Exception:
            pass
        try:
            import tomllib

            manifest = self.plugin_dir / "plugin.toml"
            if manifest.is_file():
                with manifest.open("rb") as stream:
                    data = tomllib.load(stream)
                plugin_section = data.get("plugin") if isinstance(data, dict) else None
                toml_version = plugin_section.get("version") if isinstance(plugin_section, dict) else None
                if isinstance(toml_version, str) and toml_version:
                    version = toml_version
        except Exception:
            pass
        return BridgeServer(
            settings=self._settings,
            logger=self.logger,
            fallback_discovery_dir=fallback,
            plugin_version=version,
            on_connection_lost=self._on_connection_lost,
            on_client_event=self._on_client_event,
        )

    async def _ensure_server_started(self, *, auto: bool = True) -> None:
        if auto and self._server_autostart_blocked:
            return
        current_loop = asyncio.get_running_loop()
        if self._server.running and self._server.loop is not current_loop:
            self.logger.info("VSCode bridge server was bound to a stale event loop; restarting")
            self._server.force_close()
            self._server = self._build_server()
        if not self._server.running:
            await self._server.start()
            self.logger.info("VSCode bridge started on port {}", self._server.port)
        if (
            self._tool_loop_task is None
            or self._tool_loop_task.done()
            or self._tool_loop_loop is not current_loop
        ):
            self._tool_loop_task = asyncio.create_task(
                self._tool_sync_loop(),
                name="neko-vscode-bridge-tool-sync",
            )
            self._tool_loop_loop = current_loop

    async def _on_client_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "user.request":
            request = str(data.get("request") or "").strip()
            if not request:
                return
            workspace_id = data.get("workspaceId")
            try:
                connection = self._server.connection(
                    str(workspace_id) if workspace_id else None
                )
            except BridgeError as exc:
                self.logger.warning("VSCode request has no workspace: {}", exc.message)
                return
            status = await self._model_status.fetch(PLUGIN_ID)
            if self._settings.model_mode == "dedicated" and not status.get("ready"):
                self._push_chat_message(
                    "VSCode 请求未执行：coding 模型未绑定。请在插件配置中绑定 coding 用途，"
                    "或把 model_mode 改为 auto/main。"
                )
                return
            if self._settings.model_mode in ("auto", "dedicated") and status.get("ready"):
                try:
                    self._last_real_action_at = time.time()
                    record = self._tasks.create(
                        workspace_id=connection.workspace.workspace_id,
                        request=request,
                        mode="dedicated",
                        permission_mode=self._settings.permission_mode,
                        max_steps=self._settings.max_steps,
                    )
                except RuntimeError as exc:
                    self.logger.info("VSCode request ignored: {}", exc)
                    self._push_chat_message(f"VSCode 任务未启动：{exc}")
                    return
                asyncio_task = asyncio.create_task(
                    self._run_task(record.task_id, connection.workspace.workspace_id, request),
                    name=f"neko-vscode-task-{record.task_id}",
                )
                self._tasks.attach(record.task_id, asyncio_task)
                asyncio.create_task(
                    self._monitor_task(record.task_id),
                    name=f"neko-vscode-monitor-{record.task_id}",
                )
            else:
                label = connection.workspace.name or connection.workspace.workspace_id
                self._push_chat_message(f"来自 VSCode（{label}）的请求：{request}")
        elif event == "user.cancel":
            await self._tasks.cancel(None)
            await self._server.broadcast_event("task.cancel", {})
        elif event == "task.cancel":
            await self._tasks.cancel(None)
        elif event == "changes.decision":
            await self._handle_change_decision(data)

    async def _handle_change_decision(self, data: dict[str, Any]) -> None:
        status = str(data.get("status") or "")
        change_set_id = str(data.get("changeSetId") or "")
        files = data.get("files") if isinstance(data.get("files"), list) else []
        applied = [
            str(item.get("path"))
            for item in files
            if isinstance(item, dict) and item.get("status") == "applied" and item.get("path")
        ]
        diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
        if status == "applied":
            text = f"VSCode 修改已应用（{change_set_id}）。"
        elif status == "rejected":
            text = f"VSCode 修改已放弃（{change_set_id}）。"
        elif status == "stale":
            text = f"VSCode 修改有文件已过期，未完全应用（{change_set_id}）。"
        elif status == "failed":
            text = f"VSCode 修改应用失败（{change_set_id}）。"
        else:
            return
        if applied:
            text += "\n改动文件：" + "、".join(applied[:8])
        errors = diagnostics.get("errors")
        warnings = diagnostics.get("warnings")
        if errors or warnings:
            text += f"\n诊断：{errors or 0} 个错误，{warnings or 0} 个警告。"
        self._push_chat_message(text)

    def _push_chat_message(self, text: str) -> None:
        text = _redact_text(text)
        payload = {
            "source": "vscode_bridge",
            "visibility": ["chat"],
            "ai_behavior": "respond",
            "parts": [{"type": "text", "text": text}],
            "priority": 5,
        }
        try:
            if hasattr(self, "push_message"):
                self.push_message(**payload)
            elif hasattr(self.ctx, "push_message"):
                self.ctx.push_message(**payload)
        except Exception as exc:
            self.logger.warning("Could not push VSCode request to chat: {}", exc)

    async def _start_coding_task_for_request(
        self,
        request: str,
        *,
        workspace_id: str | None = None,
    ) -> Any:
        request = str(request or "").strip()
        if not request:
            raise BridgeError("INVALID_TOOL_ARGUMENTS", "request is required")
        await self._ensure_server_started(auto=True)
        connection = self._server.connection(workspace_id)
        if self._settings.model_mode == "main":
            raise BridgeError(
                "MAIN_MODE",
                "当前是主模型模式，无法启动专用 coding 任务；请在面板切换模型模式",
            )
        status = await self._model_status.fetch(PLUGIN_ID)
        if not status.get("ready"):
            raise BridgeError("MODEL_NOT_BOUND", "还没有绑定可用的 Coding 模型")
        if self._tasks.is_busy(connection.workspace.workspace_id):
            current = self._tasks.current_for_workspace(connection.workspace.workspace_id)
            raise BridgeError(
                "WORKSPACE_BUSY",
                f"工作区已有任务：{current.task_id if current is not None else 'unknown'}",
            )
        record = self._tasks.create(
            workspace_id=connection.workspace.workspace_id,
            request=request,
            mode="dedicated",
            permission_mode=self._settings.permission_mode,
            max_steps=self._settings.max_steps,
        )
        self._last_real_action_at = time.time()
        asyncio_task = asyncio.create_task(
            self._run_task(record.task_id, connection.workspace.workspace_id, request),
            name=f"neko-vscode-task-{record.task_id}",
        )
        self._tasks.attach(record.task_id, asyncio_task)
        asyncio.create_task(
            self._monitor_task(record.task_id),
            name=f"neko-vscode-monitor-{record.task_id}",
        )
        return record

    @message(
        id="coding_router",
        name="Chat coding router",
        description="Route coding requests from chat into the VSCode bridge and correct false edit claims.",
        source="chat",
    )
    async def coding_router(
        self,
        text: str = "",
        sender: str = "",
        **kwargs: Any,
    ):
        text = str(text or "").strip()
        if not text:
            return Ok({"handled": False})
        sender_l = str(sender or "").strip().lower()
        if any(token in sender_l for token in ("vscode_bridge", "plugin", "system")):
            return Ok({"handled": False})

        message_id = str(kwargs.get("message_id") or kwargs.get("id") or "").strip()
        if message_id:
            if message_id in self._handled_message_ids:
                return Ok({"handled": False})
            self._handled_message_ids.add(message_id)
            if len(self._handled_message_ids) > 200:
                self._handled_message_ids = set(list(self._handled_message_ids)[-100:])

        is_assistant = sender_l in {
            "assistant", "ai", "neko", "n.e.k.o", "n.e.k.o.", "character", "bot",
        } or sender_l.startswith(("assistant:", "neko:"))
        if is_assistant:
            if not claim_looks_like_file_change(text):
                return Ok({"handled": False})
            if self._tasks.active():
                return Ok({"handled": False})
            if self._last_real_action_at >= self._last_user_request_at:
                return Ok({"handled": False})
            self._push_plugin_notice(
                "VSCode 桥接校验：本轮没有实际修改文件，当前工作区文件未变化。"
                "如果你需要我执行，请让我调用桥接，或在插件面板「运行」里直接执行。"
            )
            if (
                self._settings.coding_takeover_mode == "auto"
                and looks_like_coding_request(self._last_user_request)
            ):
                try:
                    record = await self._start_coding_task_for_request(self._last_user_request)
                except BridgeError:
                    return Ok({"handled": True})
                self._push_plugin_notice(
                    f"已补启动 coding 任务 {record.task_id}，实际结果会在完成后显示。"
                )
            return Ok({"handled": True})

        self._last_user_request_at = time.time()
        self._last_user_request = text
        self._push_ai_context(_CODING_RULE)

        if not looks_like_coding_request(text):
            return Ok({"handled": False})

        mode = self._settings.coding_takeover_mode
        if mode == "off":
            return Ok({"handled": False})
        if mode == "suggest":
            self._push_plugin_notice(
                "检测到 coding 请求。可以打开 N.E.K.O. 插件面板 →「运行」→"
                " 直接交给 Coding 模型执行，实际文件修改不会依赖主模型是否调用工具。"
            )
            return Ok({"handled": True})

        if self._tasks.active():
            return Ok({"handled": False})
        try:
            record = await self._start_coding_task_for_request(text)
        except BridgeError as exc:
            self._push_plugin_notice(f"没有自动接管 coding 请求：{exc.message}")
            return Ok({"handled": False})
        self._push_plugin_notice(
            f"已接管 coding 请求，任务 {record.task_id} 正在通过 VSCode 桥接执行。"
            "完成后会直接显示实际改动文件。"
        )
        return Ok({"handled": True})

    async def _on_connection_lost(self, workspace: WorkspaceConnection) -> None:
        failed = self._tasks.fail_workspace(
            workspace.workspace_id,
            "VSCode 连接断开，任务已取消",
        )
        for task_id in failed:
            record = self._tasks.get(task_id)
            if record is not None and not record.notified:
                await self._push_task_message(record)
                record.notified = True

    # ------------------------------------------------------------------
    # tool registration
    # ------------------------------------------------------------------
    def _base_tool_definitions(self) -> dict[str, dict[str, Any]]:
        return {
            "vscode_status": {
                "description": (
                    "Call this FIRST for any VSCode/workspace/coding request. "
                    "Returns connection, active workspace, coding model, task state, permission mode, "
                    "and runtime limits. Never claim files were written without a tool result."
                ),
                "parameters": _empty_schema(),
                "handler": self.vscode_status,
                "timeout": 15.0,
            },
            "vscode_coding_task": {
                "description": (
                    "Start the coding agent that ACTUALLY reads/writes files in the VSCode workspace. "
                    "Use this for every code change. Do not claim files were written unless this tool "
                    "returns applied/pending results. Runs without a step limit by default and supports long tasks."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string", "description": "The user's coding request."},
                        "workspace_id": _workspace_param(),
                        "mode": {"type": "string", "enum": ["auto", "dedicated", "main"]},
                        "permission_mode": {"type": "string", "enum": ["ask", "auto_edit", "full_access"]},
                    },
                    "required": ["request"],
                },
                "handler": self.vscode_coding_task,
                "timeout": 90.0,
            },
            "vscode_cancel_task": {
                "description": "Cancel a running VSCode coding task.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Task id; omit to cancel the latest active task."},
                    },
                },
                "handler": self.vscode_cancel_task,
                "timeout": 15.0,
            },
        }

    def _granular_tool_definitions(self) -> dict[str, dict[str, Any]]:
        return {
            "vscode_read_file": {
                "description": "Read a real file from the VSCode workspace. Never guess file contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "max_chars": {"type": "integer", "minimum": 1, "maximum": 2000000},
                        "workspace_id": _workspace_param(),
                    },
                    "required": ["path"],
                },
                "handler": self._tool_read_file,
                "timeout": 30.0,
            },
            "vscode_search": {
                "description": "Search file contents in the VSCode workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "regex": {"type": "boolean"},
                        "include": {"type": "string"},
                        "exclude": {"type": "array", "items": {"type": "string"}},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                        "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                        "workspace_id": _workspace_param(),
                    },
                    "required": ["query"],
                },
                "handler": self._tool_search,
                "timeout": 60.0,
            },
            "vscode_apply_edits": {
                "description": (
                    "Actually propose text edits in a workspace file. Read the file first. "
                    "Only claim a file was modified after this returns applied; pending means the user "
                    "still has to approve it in VSCode."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "base_hash": {"type": "string"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                },
                                "required": ["old_text", "new_text"],
                            },
                            "minItems": 1,
                        },
                        "summary": {"type": "string"},
                        "workspace_id": _workspace_param(),
                    },
                    "required": ["path", "base_hash", "edits", "summary"],
                },
                "handler": self._tool_apply_edits,
                "timeout": 30.0,
            },
            "vscode_diagnostics": {
                "description": "Return compiler/linter diagnostics from the VSCode workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "severity": {"type": "string", "enum": ["error", "warning", "info", "hint", "all"]},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                        "workspace_id": _workspace_param(),
                    },
                },
                "handler": self._tool_diagnostics,
                "timeout": 30.0,
            },
            "vscode_run_command": {
                "description": (
                    "Actually run a test/build command in the workspace (approval may be required). "
                    "Do not claim tests passed unless this returns a result."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "cwd": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                        "reason": {"type": "string"},
                        "workspace_id": _workspace_param(),
                    },
                    "required": ["command", "reason"],
                },
                "handler": self._tool_run_command,
                "timeout": 300.0,
            },
            "vscode_open_file": {
                "description": "Open a file in VSCode and optionally reveal a line range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "workspace_id": _workspace_param(),
                    },
                    "required": ["path"],
                },
                "handler": self._tool_open_file,
                "timeout": 15.0,
            },
        }

    def _list_tool_names(self) -> set[str]:
        names: set[str] = set()
        try:
            values = self.list_llm_tools()
        except Exception:
            return names
        for item in values or []:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("tool_name")
                if isinstance(name, str):
                    names.add(name)
        return names

    async def _sync_llm_tools(self) -> None:
        try:
            status = await self._model_status.fetch(PLUGIN_ID)
        except Exception:
            status = {"ready": False, "status": "unknown"}

        names = self._list_tool_names()
        required_base = {"vscode_status", "vscode_coding_task", "vscode_cancel_task"}
        if self._base_tools_ensured and not required_base.issubset(names):
            self._base_tools_ensured = False
        if not self._base_tools_ensured:
            for name, spec in self._base_tool_definitions().items():
                if name in names:
                    continue
                try:
                    self.register_llm_tool(
                        name=name,
                        description=spec["description"],
                        parameters=spec["parameters"],
                        handler=spec["handler"],
                        timeout=spec["timeout"],
                    )
                    names.add(name)
                except Exception as exc:
                    self.logger.warning("Could not register {}: {}", name, exc)
            self._base_tools_ensured = required_base.issubset(names)

        main_mode = (
            self._settings.model_mode == "main"
            or (self._settings.model_mode == "auto" and not status.get("ready"))
        )
        if main_mode:
            for name, spec in self._granular_tool_definitions().items():
                if name in names or name in self._dynamic_tool_names:
                    continue
                try:
                    self.register_llm_tool(
                        name=name,
                        description=spec["description"],
                        parameters=spec["parameters"],
                        handler=spec["handler"],
                        timeout=spec["timeout"],
                    )
                    self._dynamic_tool_names.add(name)
                except Exception as exc:
                    self.logger.warning("Could not register {}: {}", name, exc)
        else:
            self._unregister_dynamic_tools()

    def _unregister_dynamic_tools(self) -> None:
        for name in list(self._dynamic_tool_names):
            try:
                self.unregister_llm_tool(name)
            except Exception:
                pass
            finally:
                self._dynamic_tool_names.discard(name)

    async def _tool_sync_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(10.0)
                try:
                    await self._sync_llm_tools()
                except Exception as exc:
                    self.logger.debug("Tool sync failed: {}", exc)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # base LLM tools
    # ------------------------------------------------------------------
    @llm_tool(
        name="vscode_status",
        description=(
            "Call this first for any VSCode/workspace/coding request. Returns connection, workspace, "
            "coding model, task state, permission mode, and runtime limits."
        ),
        parameters=_empty_schema(),
        timeout=15.0,
    )
    async def vscode_status(self, **_):
        await self._ensure_server_started(auto=True)
        status = await self._model_status.fetch(PLUGIN_ID)
        current = self._tasks.current()
        return {
            "running": self._server.running,
            "port": self._server.port,
            "discovery_path": self._server.discovery_path,
            "workspaces": self._server.list_workspaces(),
            "active_workspace_id": self._server.registry.active_workspace_id,
            "coding_model": status,
            "task": current.to_dict() if current is not None else None,
            "model_mode": self._settings.model_mode,
            "permission_mode": self._settings.permission_mode,
            "coding_takeover_mode": self._settings.coding_takeover_mode,
            "max_steps": self._settings.max_steps,
            "task_timeout_seconds": self._settings.task_timeout_seconds,
            "model_timeout_seconds": self._settings.model_timeout_seconds,
        }

    @llm_tool(
        name="vscode_coding_task",
        description=(
            "Start the coding agent that ACTUALLY reads/writes files in the VSCode workspace. "
            "Use this for every code change. Do not claim files were written unless this tool "
            "returns applied/pending results. Runs without a step limit by default."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "workspace_id": _workspace_param(),
                "mode": {"type": "string", "enum": ["auto", "dedicated", "main"]},
                "permission_mode": {"type": "string", "enum": ["ask", "auto_edit", "full_access"]},
            },
            "required": ["request"],
        },
        timeout=90.0,
    )
    async def vscode_coding_task(
        self,
        request: str,
        workspace_id: str | None = None,
        mode: str | None = None,
        permission_mode: str | None = None,
        approval_mode: str | None = None,
        **kwargs: Any,
    ):
        workspace_id = workspace_id or kwargs.get("workspace")
        try:
            connection = self._server.connection(workspace_id)
        except BridgeError as exc:
            return self._tool_error(exc)

        requested_mode = (mode or self._settings.model_mode).lower()
        status = await self._model_status.fetch(PLUGIN_ID)
        if requested_mode == "main":
            return {
                "output": {
                    "error": "MAIN_MODE",
                    "message": "当前是主模型模式，请使用 vscode_read_file、vscode_search、vscode_apply_edits、vscode_diagnostics、vscode_run_command。",
                },
                "is_error": True,
                "error": "MAIN_MODE",
            }
        if requested_mode in ("auto", "dedicated") and not status.get("ready"):
            return {
                "output": {
                    "error": "MODEL_NOT_BOUND",
                    "message": "还没有绑定 Coding 模型。可以继续使用 vscode_read_file/vscode_search/vscode_apply_edits/vscode_diagnostics/vscode_run_command，或者绑定 coding 用途并把模型模式切到专用 Coding 模型。",
                },
                "is_error": True,
                "error": "MODEL_NOT_BOUND",
            }

        self._last_real_action_at = time.time()
        requested_permission = permission_mode or approval_mode or self._settings.permission_mode
        if requested_permission == "diff":
            requested_permission = "ask"
        elif requested_permission == "auto":
            requested_permission = "auto_edit"
        elif requested_permission == "readonly":
            requested_permission = "ask"
        if requested_permission not in ("ask", "auto_edit", "full_access"):
            requested_permission = self._settings.permission_mode

        try:
            record = self._tasks.create(
                workspace_id=connection.workspace.workspace_id,
                request=request,
                mode="dedicated",
                permission_mode=requested_permission,
                max_steps=self._settings.max_steps,
            )
        except RuntimeError as exc:
            current = self._tasks.current_for_workspace(connection.workspace_id)
            return {
                "output": {
                    "error": "WORKSPACE_BUSY",
                    "message": str(exc),
                    "task_id": current.task_id if current else None,
                },
                "is_error": True,
                "error": "WORKSPACE_BUSY",
            }

        record.sync_wait_until = time.time() + float(self._settings.sync_wait_seconds)
        asyncio_task = asyncio.create_task(
            self._run_task(record.task_id, connection.workspace_id, request),
            name=f"neko-vscode-task-{record.task_id}",
        )
        self._tasks.attach(record.task_id, asyncio_task)
        asyncio.create_task(
            self._monitor_task(record.task_id),
            name=f"neko-vscode-monitor-{record.task_id}",
        )

        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio_task),
                timeout=float(self._settings.sync_wait_seconds),
            )
        except asyncio.TimeoutError:
            return {
                "status": "running",
                "task_id": record.task_id,
                "message": "任务已开始，完成后我会告诉你。",
            }

        completed = self._tasks.get(record.task_id)
        if completed is None:
            return {"status": "failed", "task_id": record.task_id, "error": "INTERNAL_ERROR"}
        if completed.notified:
            # The background wrapper already pushed a result; return a compact status.
            return {
                "status": completed.status,
                "task_id": completed.task_id,
                "summary": completed.summary or completed.error or "",
                "files_changed": completed.files_changed,
                "diagnostics": completed.diagnostics,
            }
        completed.notified = True
        if completed.status == "failed":
            return {
                "output": {
                    "error": "TASK_FAILED",
                    "message": completed.error or "Task failed",
                    "task_id": completed.task_id,
                },
                "is_error": True,
                "error": "TASK_FAILED",
            }
        return {
            "status": completed.status,
            "task_id": completed.task_id,
            "summary": completed.summary,
            "files_changed": completed.files_changed,
            "diagnostics": completed.diagnostics,
            "tests": completed.tests,
            "remaining_issues": completed.remaining_issues,
        }

    @llm_tool(
        name="vscode_cancel_task",
        description="Cancel a running VSCode coding task.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
        },
        timeout=15.0,
    )
    async def vscode_cancel_task(self, task_id: str | None = None, **_):
        cancelled = await self._tasks.cancel(task_id)
        try:
            await self._server.broadcast_event("task.cancel", {"taskId": task_id})
        except Exception:
            pass
        return {"cancelled": cancelled, "count": len(cancelled)}

    # ------------------------------------------------------------------
    # main-mode granular tools
    # ------------------------------------------------------------------
    async def _tool_read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_chars: int | None = None,
        workspace_id: str | None = None,
        **kwargs: Any,
    ):
        try:
            result = await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "file.read",
                {
                    "path": path,
                    "startLine": start_line,
                    "endLine": end_line,
                    "maxChars": min(max_chars or 50000, 200000),
                },
                timeout=30.0,
            )
            if isinstance(result, dict) and isinstance(result.get("text"), str):
                if len(result["text"]) > 50000:
                    result["text"] = result["text"][:50000] + "\n...[truncated]"
                    result["truncated"] = True
            return result
        except Exception as exc:
            return self._tool_error(exc)

    async def _tool_search(
        self,
        query: str,
        regex: bool = False,
        include: str | None = None,
        exclude: list[str] | None = None,
        max_results: int | None = None,
        context_lines: int | None = None,
        workspace_id: str | None = None,
        **kwargs: Any,
    ):
        try:
            return await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "search.text",
                {
                    "query": query,
                    "regex": bool(regex),
                    "include": include,
                    "exclude": exclude or [],
                    "maxResults": max_results or 200,
                    "contextLines": context_lines or 1,
                },
                timeout=60.0,
            )
        except Exception as exc:
            return self._tool_error(exc)

    async def _tool_apply_edits(
        self,
        path: str,
        base_hash: str,
        edits: list[dict[str, Any]],
        summary: str,
        workspace_id: str | None = None,
        **kwargs: Any,
    ):
        self._last_real_action_at = time.time()
        try:
            current = await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "file.read",
                {"path": path, "maxChars": 2_000_000},
                timeout=30.0,
            )
            if not isinstance(current, dict):
                return self._tool_error(BridgeError("INTERNAL_ERROR", "Invalid file read result"))
            if current.get("binary"):
                return self._tool_error(BridgeError("BINARY_FILE", path))
            if current.get("truncated"):
                return self._tool_error(BridgeError(
                    "FILE_TOO_LARGE",
                    "Read the entire file before editing it",
                ))
            if current.get("hash") != base_hash:
                return self._tool_error(BridgeError(
                    "STALE_FILE",
                    "base_hash does not match the current file",
                ))
            text = current.get("text")
            if not isinstance(text, str):
                return self._tool_error(BridgeError("FILE_NOT_FOUND", path))
            for edit in edits:
                old_text = edit.get("old_text")
                new_text = edit.get("new_text")
                if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
                    return self._tool_error(BridgeError(
                        "INVALID_TOOL_ARGUMENTS",
                        "old_text 和 new_text 必须是字符串",
                    ))
                if old_text not in text:
                    return self._tool_error(BridgeError(
                        "EDIT_MISMATCH",
                        "当前文件中找不到 old_text",
                    ))
                text = text.replace(old_text, new_text, 1)

            permission = self._settings.permission_mode
            auto_apply = permission != "ask"
            silent = permission != "ask"
            change_set_id = f"cs_{uuid.uuid4().hex[:12]}"
            return await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "changes.propose",
                {
                    "changeSetId": change_set_id,
                    "summary": summary,
                    "wait": False,
                    "waitMs": 0,
                    "autoApply": auto_apply,
                    "silentDecision": silent,
                    "files": [{
                        "path": path,
                        "operation": "update",
                        "baseHash": base_hash,
                        "newText": text,
                        "summary": summary,
                    }],
                },
                timeout=30.0,
            )
        except Exception as exc:
            return self._tool_error(exc)

    async def _tool_diagnostics(
        self,
        path: str | None = None,
        severity: str = "all",
        max_results: int | None = None,
        workspace_id: str | None = None,
        **kwargs: Any,
    ):
        try:
            return await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "diagnostics.get",
                {
                    "path": path,
                    "severity": severity,
                    "maxResults": max_results or 100,
                },
                timeout=30.0,
            )
        except Exception as exc:
            return self._tool_error(exc)

    async def _tool_run_command(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
        reason: str = "",
        workspace_id: str | None = None,
        **kwargs: Any,
    ):
        if not self._settings.allow_terminal:
            return self._tool_error(BridgeError(
                "COMMAND_DENIED",
                "插件设置中已禁用终端命令",
            ))
        self._last_real_action_at = time.time()
        timeout = min(
            int(timeout_seconds or self._settings.command_timeout_seconds),
            self._settings.command_timeout_seconds,
        )
        require_approval = command_requires_approval(
            self._settings.permission_mode, command, args or []
        )
        try:
            result = await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "command.run",
                {
                    "command": command,
                    "args": args or [],
                    "cwd": cwd,
                    "timeoutSeconds": timeout,
                    "reason": reason,
                    "requireApproval": require_approval,
                },
                timeout=timeout + 30.0,
            )
            if isinstance(result, dict):
                for key in ("stdout", "stderr"):
                    value = result.get(key)
                    if isinstance(value, str) and len(value) > 20000:
                        result[key] = value[:20000] + "\n...[truncated]"
                        result["truncated"] = True
            return result
        except Exception as exc:
            return self._tool_error(exc)

    async def _tool_open_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        workspace_id: str | None = None,
        **kwargs: Any,
    ):
        try:
            return await self._server.request(
                workspace_id or kwargs.get("workspace"),
                "editor.open",
                {"path": path, "startLine": start_line, "endLine": end_line},
                timeout=15.0,
            )
        except Exception as exc:
            return self._tool_error(exc)

    # ------------------------------------------------------------------
    # task runner
    # ------------------------------------------------------------------
    async def _run_task(self, task_id: str, workspace_id: str, request: str) -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        try:
            connection = self._server.connection(workspace_id)
        except BridgeError:
            self._tasks.fail(task_id, "VSCode 工作区已断开连接")
            return
        try:
            record.status = "running"
            record.set_phase("preparing", "正在准备 coding agent")
            await self._broadcast_progress(record)
            client = await self.ctx.models.get_client()
            agent = CodingAgent(settings=self._settings, logger=self.logger)
            result = await agent.run(
                connection=connection,
                task=record,
                model_client=client,
                request=request,
                on_progress=self._broadcast_progress,
            )
            self._tasks.complete(
                task_id,
                summary=_redact_text(str(result.get("summary") or "")),
                files_changed=list(result.get("files_changed") or []),
                diagnostics=result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {},
                tests=_redact_text(str(result.get("tests") or "")),
                remaining_issues=_redact_text(str(result.get("remaining_issues") or "")),
            )
        except asyncio.CancelledError:
            self._tasks.mark_cancelled(task_id)
        except AgentError as exc:
            self._tasks.fail(task_id, _redact_text(f"{exc.code}: {exc.message}"))
        except Exception as exc:
            self._tasks.fail(task_id, _redact_text(f"内部错误：{exc}"))
        finally:
            record = self._tasks.get(task_id)
            if record is not None:
                await self._broadcast_progress(record)

    async def _broadcast_progress(self, record: Any) -> None:
        try:
            data = record.to_dict()
            await self._server.broadcast_event("task.progress", {
                "taskId": data["task_id"],
                "status": data["status"],
                "phase": data["phase"],
                "step": data["step"],
                "maxSteps": data["max_steps"],
                "message": data["message"],
                "elapsedSeconds": data["elapsed_seconds"],
                "phaseElapsedSeconds": data["phase_elapsed_seconds"],
                "lastTool": data["last_tool"],
                "filesChanged": data["files_changed"],
            })
        except Exception:
            pass

    async def _monitor_task(self, task_id: str) -> None:
        record = self._tasks.get(task_id)
        if record is None or record.task is None:
            return
        try:
            await asyncio.shield(record.task)
        except (asyncio.CancelledError, Exception):
            pass
        record = self._tasks.get(task_id)
        if record is None:
            return
        now = time.time()
        if record.sync_wait_until > now:
            await asyncio.sleep(record.sync_wait_until - now + 0.2)
            record = self._tasks.get(task_id)
        if record is None or record.notified:
            return
        if record.status in ("completed", "failed", "cancelled"):
            await self._push_task_message(record)
            record.notified = True

    def _push_payload(self, payload: dict[str, Any]) -> None:
        try:
            if hasattr(self, "push_message"):
                self.push_message(**payload)
            elif hasattr(self.ctx, "push_message"):
                self.ctx.push_message(**payload)
        except Exception as exc:
            self.logger.warning("Could not push plugin message: {}", exc)

    def _push_plugin_notice(self, text: str) -> None:
        self._push_payload({
            "source": "vscode_bridge",
            "visibility": ["chat"],
            "ai_behavior": "blind",
            "parts": [{"type": "text", "text": _redact_text(text)}],
            "priority": 8,
        })

    def _push_ai_context(self, text: str) -> None:
        self._push_payload({
            "source": "vscode_bridge",
            "visibility": [],
            "ai_behavior": "read",
            "parts": [{"type": "text", "text": _redact_text(text)}],
            "priority": 20,
            "coalesce_key": "vscode_coding_rule",
        })

    async def _push_task_message(self, record: Any) -> None:
        if record.status == "completed":
            text = f"VSCode 任务完成：{record.summary or '已完成'}"
        elif record.status == "cancelled":
            text = f"VSCode 任务已取消：{record.task_id}"
        else:
            text = f"VSCode 任务失败：{record.error or '未知错误'}"
        if record.files_changed:
            text += "\n改动文件：" + "、".join(record.files_changed[:8])
        diagnostics = record.diagnostics if isinstance(record.diagnostics, dict) else {}
        errors = diagnostics.get("errors")
        warnings = diagnostics.get("warnings")
        if errors or warnings:
            text += f"\n诊断：{errors or 0} 个错误，{warnings or 0} 个警告。"
        if record.tests:
            text += f"\n测试：{record.tests}"
        if record.remaining_issues:
            text += f"\n待处理：{record.remaining_issues}"
        text = _redact_text(text)
        metadata = {
            "task_id": record.task_id,
            "workspace_id": record.workspace_id,
            "files_changed": record.files_changed,
        }
        # The visible bubble is the factual task result; do not let the main
        # model paraphrase or replace it.
        self._push_payload({
            "source": "vscode_bridge",
            "visibility": ["chat"],
            "ai_behavior": "blind",
            "parts": [{"type": "text", "text": text}],
            "priority": 8,
            "metadata": metadata,
        })
        # Also put the real result into the next LLM context.
        self._push_payload({
            "source": "vscode_bridge",
            "visibility": [],
            "ai_behavior": "read",
            "parts": [{"type": "text", "text": "[VSCode 任务结果]\n" + text}],
            "priority": 8,
            "metadata": metadata,
        })

    @staticmethod
    def _tool_error(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, BridgeError):
            return {
                "output": {"error": exc.code, "message": exc.message, "details": exc.details},
                "is_error": True,
                "error": exc.code,
            }
        return {
            "output": {"error": "INTERNAL_ERROR", "message": _redact_text(str(exc))},
            "is_error": True,
            "error": "INTERNAL_ERROR",
        }

    # ------------------------------------------------------------------
    # Hosted UI context
    # ------------------------------------------------------------------
    @ui.context(id="dashboard", title=tr("panel.title", default="VSCode Bridge"))
    async def get_dashboard_ui_context(self) -> dict[str, Any]:
        await self._ensure_server_started(auto=True)
        status = await self._model_status.fetch(PLUGIN_ID)
        current = self._tasks.current()
        return {
            "server": {
                "running": self._server.running,
                "port": self._server.port,
                "discovery_dir": self._settings.discovery_dir or "~/.neko-vscode/ide",
                "discovery_path": self._server.discovery_path,
                "token_preview": self._server.token_preview,
                "error": self._server.discovery_error,
            },
            "workspaces": self._server.list_workspaces(),
            "active_workspace_id": self._server.registry.active_workspace_id,
            "coding_model": status,
            "settings": self._settings.to_dict(),
            "task": current.to_dict() if current is not None else None,
            "history": self._tasks.history_dicts(limit=5),
            "links": {
                "plugin_detail": "/plugins/vscode_bridge",
                "model_api": "/model-api?plugin_id=vscode_bridge",
            },
        }

    # ------------------------------------------------------------------
    # Hosted UI actions
    # ------------------------------------------------------------------
    @ui.action(
        label=tr("actions.save_settings.label", default="Save settings"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_settings",
        name=tr("entries.save_settings.name", default="Save VSCode Bridge settings"),
        description=tr("entries.save_settings.description", default="Persist VSCode bridge settings."),
        input_schema={
            "type": "object",
            "properties": {
                "settings": {"type": "object", "description": "Partial or complete bridge settings."},
            },
        },
    )
    async def save_settings(self, settings: dict[str, Any] | None = None, **kwargs: Any):
        incoming = settings if isinstance(settings, dict) else kwargs
        current = self._settings.model_dump()
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                if key in current:
                    current[key] = value
        try:
            new_settings = BridgeSettings.model_validate(current)
        except Exception as exc:
            return Err(SdkError(f"Invalid settings: {exc}"))
        old_settings = self._settings
        self._settings = new_settings
        self._agent = CodingAgent(settings=new_settings, logger=self.logger)
        try:
            await self.ctx.update_own_config({"vscode_bridge": new_settings.model_dump(mode="json")})
        except Exception as exc:
            self.logger.warning("Could not persist settings: {}", exc)
            return Err(SdkError(f"Could not persist settings: {exc}"))

        needs_restart = (
            old_settings.port != new_settings.port
            or old_settings.discovery_dir != new_settings.discovery_dir
            or old_settings.heartbeat_seconds != new_settings.heartbeat_seconds
            or old_settings.stale_after_seconds != new_settings.stale_after_seconds
        )
        if needs_restart:
            try:
                was_running = self._server.running
                current_loop = asyncio.get_running_loop()
                if was_running and self._server.loop is current_loop:
                    await self._server.stop()
                else:
                    self._server.force_close()
                self._server = self._build_server()
                if was_running:
                    await self._ensure_server_started(auto=False)
            except Exception as exc:
                self.logger.warning("Could not restart bridge server: {}", exc)
                return Err(SdkError(f"Settings saved, but the server could not restart: {exc}"))
        await self._sync_llm_tools()
        return Ok({"message": "设置已保存", "settings": new_settings.to_dict()})

    @ui.action(
        label=tr("actions.regenerate_token.label", default="Regenerate token"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="regenerate_token",
        name=tr("entries.regenerate_token.name", default="Regenerate bridge token"),
        description=tr("entries.regenerate_token.description", default="Rotate the local bridge token."),
        input_schema=_empty_schema(),
    )
    async def regenerate_token(self, **_):
        preview = self._server.regenerate_token()
        await self._server.close_all()
        return Ok({"token_preview": preview, "message": "Token rotated; VSCode will reconnect."})

    @ui.action(
        label=tr("actions.start_server.label", default="Start server"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="start_server",
        name=tr("entries.start_server.name", default="Start bridge server"),
        description=tr("entries.start_server.description", default="Start the local WebSocket server."),
        input_schema=_empty_schema(),
    )
    async def start_server(self, **_):
        self._server_autostart_blocked = False
        await self._ensure_server_started(auto=False)
        return Ok({
            "running": self._server.running,
            "port": self._server.port,
            "discovery_path": self._server.discovery_path,
        })

    @ui.action(
        label=tr("actions.stop_server.label", default="Stop server"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="stop_server",
        name=tr("entries.stop_server.name", default="Stop bridge server"),
        description=tr("entries.stop_server.description", default="Stop the local WebSocket server."),
        input_schema=_empty_schema(),
    )
    async def stop_server(self, **_):
        self._server_autostart_blocked = True
        current_loop = asyncio.get_running_loop()
        if self._server.running and self._server.loop is not current_loop:
            self._server.force_close()
        else:
            await self._server.stop()
        return Ok({"running": self._server.running})

    @ui.action(
        label=tr("actions.select_workspace.label", default="Select workspace"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="select_workspace",
        name=tr("entries.select_workspace.name", default="Select active workspace"),
        description=tr("entries.select_workspace.description", default="Choose the VSCode workspace used by default."),
        input_schema={
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    )
    async def select_workspace(self, workspace_id: str, **_):
        try:
            connection = self._server.set_active(workspace_id)
        except BridgeError as exc:
            return Err(SdkError(exc.message))
        return Ok({"active_workspace_id": connection.workspace_id})

    @ui.action(
        label=tr("actions.start_coding_task.label", default="Start coding task"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="start_coding_task",
        name=tr("entries.start_coding_task.name", default="Start coding task from panel"),
        description=tr(
            "entries.start_coding_task.description",
            default="Run a coding request directly through the bound Coding model, without the main model.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "Coding request to execute directly."},
                "workspace_id": {"type": "string"},
            },
            "required": ["request"],
        },
    )
    async def start_coding_task(
        self,
        request: str = "",
        workspace_id: str | None = None,
        **_,
    ):
        request = str(request or "").strip()
        if not request:
            return Err(SdkError("request is required"))
        try:
            record = await self._start_coding_task_for_request(
                request,
                workspace_id=workspace_id,
            )
        except BridgeError as exc:
            return Err(SdkError(f"{exc.code}: {exc.message}"))
        return Ok({"task_id": record.task_id, "status": record.status})

    @ui.action(
        label=tr("actions.test_coding_model.label", default="Test coding model"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="test_coding_model",
        name=tr("entries.test_coding_model.name", default="Test coding model"),
        description=tr("entries.test_coding_model.description", default="Send a short request to the bound coding model."),
        input_schema=_empty_schema(),
    )
    async def test_coding_model(self, **_):
        started = time.monotonic()
        try:
            client = await self.ctx.models.get_client()
            response = await client.chat.completions.create(
                model="coding",
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_completion_tokens=16,
                temperature=0,
            )
            latency = round((time.monotonic() - started) * 1000, 1)
            model = getattr(response, "model", None)
            return Ok({"ok": True, "latency_ms": latency, "model": model})
        except Exception as exc:
            return Ok({
                "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": _redact_text(str(exc)),
            })

    @ui.action(
        label=tr("actions.cancel_task.label", default="Cancel task"),
        tone="danger",
        refresh_context=True,
    )
    @plugin_entry(
        id="cancel_task",
        name=tr("entries.cancel_task.name", default="Cancel coding task"),
        description=tr("entries.cancel_task.description", default="Cancel a running VSCode coding task."),
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    )
    async def cancel_task(self, task_id: str | None = None, **_):
        cancelled = await self._tasks.cancel(task_id)
        try:
            await self._server.broadcast_event("task.cancel", {"taskId": task_id})
        except Exception:
            pass
        return Ok({"cancelled": cancelled, "count": len(cancelled)})

    @ui.action(
        label=tr("actions.clear_history.label", default="Clear history"),
        tone="warning",
        refresh_context=True,
    )
    @plugin_entry(
        id="clear_history",
        name=tr("entries.clear_history.name", default="Clear task history"),
        description=tr("entries.clear_history.description", default="Clear recent VSCode task records."),
        input_schema=_empty_schema(),
    )
    async def clear_history(self, **_):
        self._tasks.clear_history()
        return Ok({"message": "Task history cleared"})
