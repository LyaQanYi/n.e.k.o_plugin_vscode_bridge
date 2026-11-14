"""Local WebSocket server and per-window connections."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from .discovery import DiscoveryWriter
from .protocol import (
    MAX_MESSAGE_BYTES,
    MAX_PENDING_REQUESTS,
    PROTOCOL_VERSION,
    SERVER_NAME,
    BridgeError,
    make_error_response,
    make_event,
    make_request,
    make_response,
    parse_message,
)
from .rpc import RpcRegistry, RpcTimeoutError
from .settings import BridgeSettings
from .workspace import WorkspaceConnection, WorkspaceRegistry


class BridgeConnection:
    """One VSCode window."""

    def __init__(
        self,
        *,
        server: "BridgeServer",
        websocket: Any,
        workspace: WorkspaceConnection,
        session_id: str,
    ):
        self.server = server
        self.websocket = websocket
        self.workspace = workspace
        self.session_id = session_id
        self.pending = RpcRegistry()
        self.last_pong = time.time()
        self.closed = False
        self._send_lock = asyncio.Lock()

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.closed:
            raise BridgeError("CANCELLED", "Connection is closed")
        async with self._send_lock:
            try:
                await self.websocket.send(json.dumps(payload, ensure_ascii=False))
            except ConnectionClosed as exc:
                self.closed = True
                raise BridgeError("CANCELLED", "Connection is closed") from exc

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = 30.0) -> Any:
        if len(self.pending) >= MAX_PENDING_REQUESTS:
            raise BridgeError("INTERNAL_ERROR", "Too many pending bridge requests")
        request_id = f"r_{uuid.uuid4().hex[:12]}"
        future = self.pending.create(request_id, method)
        try:
            await self.send_json(make_request(
                request_id,
                method,
                params or {},
                workspace_id=self.workspace.workspace_id,
            ))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(request_id)
            try:
                await self.send_json({"type": "cancel", "id": request_id})
            except BridgeError:
                pass
            raise RpcTimeoutError(f"Bridge request timed out: {method}") from exc
        except asyncio.CancelledError:
            self.pending.pop(request_id)
            try:
                await self.send_json({"type": "cancel", "id": request_id})
            except BridgeError:
                pass
            raise
        except BridgeError:
            self.pending.pop(request_id)
            raise

    @property
    def workspace_id(self) -> str:
        return self.workspace.workspace_id

    def resolve(self, request_id: str, result: Any) -> None:
        self.pending.resolve(request_id, result)

    def reject(self, request_id: str, error: BridgeError) -> None:
        self.pending.reject(request_id, error)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pending.cancel_all("Connection closed")
        try:
            await self.websocket.close()
        except Exception:
            pass


class BridgeServer:
    """Hosts the loopback WebSocket server and routes requests by workspace."""

    def __init__(
        self,
        *,
        settings: BridgeSettings,
        logger: Any,
        fallback_discovery_dir: str | Path | None = None,
        plugin_version: str = "0.1.0",
        on_connection_lost: Callable[[WorkspaceConnection], Awaitable[None]] | None = None,
        on_client_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.settings = settings
        self.logger = logger
        self.fallback_discovery_dir = fallback_discovery_dir
        self.plugin_version = plugin_version
        self.on_connection_lost = on_connection_lost
        self.on_client_event = on_client_event

        self.registry = WorkspaceRegistry()
        self._token = secrets.token_urlsafe(32)
        self._instance_id = uuid.uuid4().hex
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._port: int | None = None
        self._discovery: DiscoveryWriter | None = None
        self.discovery_path: str | None = None
        self.discovery_error: str | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._clients: dict[str, BridgeConnection] = {}
        self._started = False

    @property
    def running(self) -> bool:
        return self._started and self._server is not None

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def token_preview(self) -> str:
        token = self._token
        if len(token) <= 8:
            return "****"
        return f"{token[:4]}…{token[-4:]}"

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start(self) -> None:
        if self._started:
            return
        if self.settings.port == 0:
            candidates = [0]
        else:
            candidates = [self.settings.port] + [
                self.settings.port + offset for offset in range(1, 10)
            ] + [0]

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                server = await websockets.serve(
                    self._handler,
                    "127.0.0.1",
                    candidate,
                    max_size=MAX_MESSAGE_BYTES,
                    ping_interval=None,
                    ping_timeout=None,
                )
            except OSError as exc:
                last_error = exc
                continue
            self._server = server
            self._loop = asyncio.get_running_loop()
            self._port = self._bound_port(server, candidate)
            break

        if self._server is None:
            raise BridgeError(
                "INTERNAL_ERROR",
                f"Could not bind the bridge server: {last_error}",
            )

        self._started = True
        self._write_discovery()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="neko-vscode-bridge-heartbeat",
        )
        self.logger.info("VSCode bridge listening on 127.0.0.1:{}", self._port)

    async def stop(self) -> None:
        self._started = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None

        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()

        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

        if self._discovery is not None:
            self._discovery.remove()
        self._discovery = None
        self.discovery_path = None
        self._port = None

    def force_close(self) -> None:
        """Close a server whose event loop is no longer running.

        ``asyncio.run`` is used for plugin lifecycle callbacks.  A websocket
        server created there keeps its listening socket open, but its accept
        and handshake tasks die with the loop.  Closing it synchronously lets
        the plugin recreate the server on the persistent command loop without
        leaving a dead listener behind.
        """
        server = self._server
        self._server = None
        self._loop = None
        self._started = False
        self._port = None
        if self._heartbeat_task is not None:
            try:
                self._heartbeat_task.cancel()
            except Exception:
                pass
            self._heartbeat_task = None
        self._clients.clear()
        self.registry = WorkspaceRegistry()
        if server is not None:
            for sock in (getattr(server, "sockets", None) or []):
                try:
                    sock.close()
                except Exception:
                    pass
        if self._discovery is not None:
            try:
                self._discovery.remove()
            except Exception:
                pass
            self._discovery = None
        self.discovery_path = None

    async def close_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()

    def set_active(self, workspace_id: str) -> WorkspaceConnection:
        return self.registry.set_active(workspace_id)

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self.registry.to_list()

    def connection(self, workspace_id: str | None = None) -> BridgeConnection:
        workspace = self.registry.resolve(workspace_id)
        client = self._clients.get(workspace.connection_id)
        if client is None or client.closed:
            raise BridgeError(
                "WORKSPACE_NOT_FOUND",
                f"VSCode workspace is no longer connected: {workspace.workspace_id}",
            )
        return client

    async def request(self, workspace_id: str | None, method: str,
                      params: dict[str, Any] | None = None,
                      timeout: float = 30.0) -> Any:
        connection = self.connection(workspace_id)
        return await connection.request(method, params, timeout=timeout)

    async def broadcast_event(self, event: str, data: dict[str, Any] | None = None) -> None:
        payload = make_event(event, data)
        for client in list(self._clients.values()):
            try:
                await client.send_json(payload)
            except BridgeError:
                continue

    def regenerate_token(self) -> str:
        self._token = secrets.token_urlsafe(32)
        self._write_discovery()
        return self.token_preview

    def _write_discovery(self) -> None:
        if self._port is None:
            return
        try:
            writer = DiscoveryWriter(
                directory=self.settings.discovery_dir or None,
                token=self._token,
                instance_id=self._instance_id,
                plugin_version=self.plugin_version,
            )
            path = writer.write(self._port)
            self._discovery = writer
            self.discovery_path = str(path)
            self.discovery_error = None
            return
        except OSError as exc:
            self.discovery_error = str(exc)
            self.logger.warning("Could not write discovery file: {}", exc)

        if self.fallback_discovery_dir:
            try:
                writer = DiscoveryWriter(
                    directory=self.fallback_discovery_dir,
                    token=self._token,
                    instance_id=self._instance_id,
                    plugin_version=self.plugin_version,
                )
                path = writer.write(self._port)
                self._discovery = writer
                self.discovery_path = str(path)
                self.discovery_error = f"Primary discovery directory unavailable: {self.discovery_error}"
                self.logger.info("Using fallback discovery file: {}", path)
            except OSError as exc:
                self.discovery_error = f"{self.discovery_error}; fallback failed: {exc}"
                self.logger.warning("Could not write fallback discovery file: {}", exc)

    @staticmethod
    def _bound_port(server: Any, requested: int) -> int:
        sockets = getattr(server, "sockets", None)
        if sockets:
            try:
                return int(sockets[0].getsockname()[1])
            except (OSError, IndexError, TypeError, ValueError):
                pass
        return int(requested)

    async def _handler(self, websocket: Any) -> None:
        request = getattr(websocket, "request", None)
        origin = request.headers.get("Origin") if request is not None else None
        if origin and not (
            origin.startswith("vscode-")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://localhost")
            or origin.startswith("https://127.0.0.1")
        ):
            await self._send_hello_error(websocket, "AUTH_FAILED", "Unsupported Origin")
            return

        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        except (asyncio.TimeoutError, ConnectionClosed):
            await websocket.close()
            return

        try:
            hello = parse_message(raw)
        except BridgeError:
            await self._send_hello_error(websocket, "INTERNAL_ERROR", "Expected a JSON hello")
            return

        if hello.get("type") != "hello":
            await self._send_hello_error(websocket, "AUTH_FAILED", "Expected hello")
            return

        token = hello.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            await self._send_hello_error(websocket, "AUTH_FAILED", "Invalid token")
            return

        version = hello.get("v")
        if version != PROTOCOL_VERSION:
            await self._send_hello_error(
                websocket,
                "PROTOCOL_MISMATCH",
                f"Bridge protocol {PROTOCOL_VERSION} required, got {version!r}",
            )
            return

        client_info = hello.get("client") if isinstance(hello.get("client"), dict) else {}
        extension_version = str(client_info.get("version") or "")
        connection_id = uuid.uuid4().hex
        workspace = WorkspaceConnection.from_hello(
            connection_id,
            hello.get("workspace"),
            protocol_version=PROTOCOL_VERSION,
            extension_version=extension_version,
        )
        session_id = uuid.uuid4().hex
        client = BridgeConnection(
            server=self,
            websocket=websocket,
            workspace=workspace,
            session_id=session_id,
        )
        self._clients[connection_id] = client
        self.registry.add(workspace)

        try:
            await client.send_json({
                "type": "hello_ack",
                "v": PROTOCOL_VERSION,
                "server": {
                    "name": SERVER_NAME,
                    "version": self.plugin_version,
                    "pluginVersion": self.plugin_version,
                },
                "sessionId": session_id,
                "capabilities": [
                    "read", "write", "search", "diagnostics",
                    "diff", "terminal", "multiRoot",
                ],
            })
            self.logger.info(
                "VSCode connected: workspace={} window={} extension={}",
                workspace.workspace_id,
                workspace.window_id,
                extension_version,
            )
            async for raw in websocket:
                try:
                    message = parse_message(raw)
                except BridgeError as exc:
                    self.logger.warning("Ignoring malformed bridge message: {}", exc.message)
                    continue
                await self._handle_client_message(client, message)
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.logger.warning("Bridge connection error: {}", exc)
        finally:
            self._clients.pop(connection_id, None)
            removed = self.registry.remove(connection_id)
            client.pending.cancel_all("Connection closed")
            if removed is not None and self.on_connection_lost is not None:
                try:
                    await self.on_connection_lost(removed)
                except Exception as exc:
                    self.logger.warning("Connection-lost callback failed: {}", exc)

    async def _handle_client_message(self, client: BridgeConnection, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "response":
            request_id = message.get("id")
            if not isinstance(request_id, str):
                return
            if message.get("ok") is True:
                client.resolve(request_id, message.get("result"))
                return
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            client.reject(request_id, BridgeError(
                str(error.get("code") or "INTERNAL_ERROR"),
                str(error.get("message") or "Bridge request failed"),
                error.get("details") if isinstance(error.get("details"), dict) else None,
            ))
            return

        if message_type == "cancel":
            request_id = message.get("id")
            if isinstance(request_id, str):
                client.pending.cancel(request_id)
            return

        if message_type == "ping":
            await client.send_json({"type": "pong", "ts": message.get("ts", time.time())})
            return

        if message_type == "pong":
            client.last_pong = time.time()
            return

        if message_type == "event":
            event = message.get("event")
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            if event == "window.focus":
                workspace_id = data.get("workspaceId") or client.workspace.workspace_id
                timestamp = data.get("timestamp")
                self.registry.update_focus(
                    str(workspace_id),
                    float(timestamp) if isinstance(timestamp, (int, float)) else None,
                )
            elif event == "workspace.update":
                payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
                if payload:
                    old = client.workspace
                    updated = WorkspaceConnection.from_hello(
                        old.connection_id,
                        payload,
                        protocol_version=old.protocol_version,
                        extension_version=old.extension_version,
                    )
                    updated.last_focus_at = old.last_focus_at
                    updated.last_seen_at = time.time()
                    self.registry.remove(old.connection_id)
                    self.registry.add(updated)
                    client.workspace = updated
            elif event == "changes.decision":
                self.logger.info(
                    "Change set decision from VSCode: {} {}",
                    data.get("changeSetId"),
                    data.get("status"),
                )
            elif event == "task.progress":
                self.logger.debug("Task progress from VSCode: {}", data)

            if self.on_client_event is not None and isinstance(event, str):
                try:
                    await self.on_client_event(event, data)
                except Exception as exc:
                    self.logger.warning("Client event callback failed: {}", exc)
            return

    @staticmethod
    async def _send_hello_error(websocket: Any, code: str, message: str) -> None:
        try:
            await websocket.send(json.dumps({
                "type": "hello_error",
                "code": code,
                "message": message,
            }, ensure_ascii=False))
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass

    async def _heartbeat_loop(self) -> None:
        try:
            while self._started:
                await asyncio.sleep(max(5, self.settings.heartbeat_seconds))
                if not self._started:
                    return
                if self._discovery is not None:
                    try:
                        self._discovery.touch()
                    except OSError as exc:
                        self.logger.warning("Discovery heartbeat failed: {}", exc)
                now = time.time()
                for client in list(self._clients.values()):
                    if now - client.last_pong > self.settings.stale_after_seconds:
                        self.logger.info(
                            "Closing stale VSCode connection: {}",
                            client.workspace.workspace_id,
                        )
                        await client.close()
                        continue
                    try:
                        await client.send_json({"type": "ping", "ts": now})
                    except BridgeError:
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("Bridge heartbeat loop stopped: {}", exc)
