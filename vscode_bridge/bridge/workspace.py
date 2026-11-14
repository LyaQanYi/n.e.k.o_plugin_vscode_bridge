"""Workspace registration and active-workspace routing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import BridgeError


def _normalise_roots(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    roots: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        roots.append({
            "name": str(item.get("name") or ""),
            "uri": str(item.get("uri") or ""),
            "path": str(item.get("path") or ""),
        })
    return roots


@dataclass
class WorkspaceConnection:
    connection_id: str
    workspace_id: str
    window_id: str
    name: str
    roots: list[dict[str, Any]] = field(default_factory=list)
    is_trusted: bool = True
    remote_name: str | None = None
    extension_version: str = ""
    protocol_version: int = 0
    last_focus_at: float = 0.0
    last_seen_at: float = 0.0

    @classmethod
    def from_hello(cls, connection_id: str, payload: object, *, protocol_version: int,
                   extension_version: str = "") -> "WorkspaceConnection":
        data = payload if isinstance(payload, dict) else {}
        workspace_id = str(data.get("id") or data.get("workspaceId") or connection_id)
        return cls(
            connection_id=connection_id,
            workspace_id=workspace_id,
            window_id=str(data.get("windowId") or ""),
            name=str(data.get("name") or workspace_id),
            roots=_normalise_roots(data.get("roots")),
            is_trusted=bool(data.get("isTrusted", True)),
            remote_name=(str(data["remoteName"]) if data.get("remoteName") else None),
            extension_version=extension_version,
            protocol_version=protocol_version,
            last_focus_at=time.time(),
            last_seen_at=time.time(),
        )

    def to_dict(self, *, active: bool = False) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "window_id": self.window_id,
            "name": self.name,
            "roots": [root.get("path") or root.get("uri") or "" for root in self.roots],
            "root_details": self.roots,
            "active": active,
            "is_trusted": self.is_trusted,
            "remote_name": self.remote_name,
            "extension_version": self.extension_version,
            "protocol": self.protocol_version,
            "last_focus_at": self.last_focus_at,
            "last_seen_at": self.last_seen_at,
        }


class WorkspaceRegistry:
    """Keeps every connected VSCode window and selects the active one."""

    def __init__(self) -> None:
        self._connections: dict[str, WorkspaceConnection] = {}
        self._active_workspace_id: str | None = None

    @property
    def active_workspace_id(self) -> str | None:
        return self._active_workspace_id

    def add(self, connection: WorkspaceConnection) -> None:
        self._connections[connection.connection_id] = connection
        self._active_workspace_id = connection.workspace_id
        connection.last_focus_at = time.time()
        connection.last_seen_at = time.time()

    def remove(self, connection_id: str) -> WorkspaceConnection | None:
        removed = self._connections.pop(connection_id, None)
        if removed is not None and self._active_workspace_id == removed.workspace_id:
            self._active_workspace_id = self._most_recent_workspace_id()
        return removed

    def get(self, connection_id: str) -> WorkspaceConnection | None:
        return self._connections.get(connection_id)

    def list(self) -> list[WorkspaceConnection]:
        return sorted(
            self._connections.values(),
            key=lambda item: item.last_focus_at,
            reverse=True,
        )

    def find(self, workspace_id: str) -> WorkspaceConnection | None:
        for connection in self._connections.values():
            if connection.workspace_id == workspace_id:
                return connection
        return None

    def set_active(self, workspace_id: str) -> WorkspaceConnection:
        connection = self.find(workspace_id)
        if connection is None:
            raise BridgeError("WORKSPACE_NOT_FOUND", f"Unknown workspace: {workspace_id}")
        self._active_workspace_id = workspace_id
        connection.last_focus_at = time.time()
        return connection

    def update_focus(self, workspace_id: str, timestamp: float | None = None) -> None:
        connection = self.find(workspace_id)
        if connection is None:
            return
        connection.last_focus_at = timestamp or time.time()
        self._active_workspace_id = workspace_id

    def resolve(self, workspace_id: str | None = None) -> WorkspaceConnection:
        if workspace_id:
            connection = self.find(workspace_id)
            if connection is None:
                raise BridgeError("WORKSPACE_NOT_FOUND", f"Unknown workspace: {workspace_id}")
            return connection

        if self._active_workspace_id:
            connection = self.find(self._active_workspace_id)
            if connection is not None:
                return connection

        connections = self.list()
        if len(connections) == 1:
            return connections[0]
        if not connections:
            raise BridgeError("WORKSPACE_NOT_FOUND", "No VSCode workspace is connected")
        names = ", ".join(connection.workspace_id for connection in connections)
        raise BridgeError(
            "WORKSPACE_NOT_FOUND",
            f"Multiple VSCode workspaces are connected; choose one: {names}",
        )

    def to_list(self) -> list[dict[str, Any]]:
        active = self._active_workspace_id
        return [
            connection.to_dict(active=connection.workspace_id == active)
            for connection in self.list()
        ]

    def _most_recent_workspace_id(self) -> str | None:
        connections = self.list()
        return connections[0].workspace_id if connections else None
