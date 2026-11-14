from __future__ import annotations

import pytest

from vscode_bridge.bridge.protocol import BridgeError
from vscode_bridge.bridge.settings import BridgeSettings
from vscode_bridge.bridge.workspace import WorkspaceConnection, WorkspaceRegistry


def _connection(workspace_id: str, connection_id: str | None = None) -> WorkspaceConnection:
    return WorkspaceConnection(
        connection_id=connection_id or f"conn_{workspace_id}",
        workspace_id=workspace_id,
        window_id="window",
        name=workspace_id,
        roots=[{"name": workspace_id, "uri": f"file:///tmp/{workspace_id}", "path": f"/tmp/{workspace_id}"}],
    )


def test_registry_tracks_active_workspace() -> None:
    registry = WorkspaceRegistry()
    registry.add(_connection("a"))
    registry.add(_connection("b"))
    assert registry.active_workspace_id == "b"
    registry.set_active("a")
    assert registry.resolve().workspace_id == "a"
    registry.remove("conn_a")
    assert registry.active_workspace_id == "b"


def test_registry_rejects_unknown_workspace() -> None:
    registry = WorkspaceRegistry()
    with pytest.raises(BridgeError) as error:
        registry.resolve("missing")
    assert error.value.code == "WORKSPACE_NOT_FOUND"


def test_workspace_from_hello_normalises_roots() -> None:
    connection = WorkspaceConnection.from_hello(
        "conn",
        {
            "id": "ws",
            "name": "project",
            "windowId": "window",
            "roots": [{"name": "project", "uri": "file:///tmp/p", "path": "/tmp/p"}],
            "isTrusted": True,
        },
        protocol_version=1,
        extension_version="0.1.0",
    )
    assert connection.workspace_id == "ws"
    assert connection.roots[0]["path"] == "/tmp/p"
    assert connection.protocol_version == 1


def test_settings_reject_invalid_values() -> None:
    with pytest.raises(Exception):
        BridgeSettings(model_mode="invalid")
    with pytest.raises(Exception):
        BridgeSettings(port=70000)
