"""Protocol constants and small helpers shared by the bridge modules."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
SERVER_NAME = "neko-vscode-bridge"
PLUGIN_ID = "vscode_bridge"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_PENDING_REQUESTS = 32


class BridgeError(Exception):
    """An error that can be returned over the bridge or shown in the panel."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def parse_message(raw: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("INTERNAL_ERROR", "Message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BridgeError("INTERNAL_ERROR", "Message must be a JSON object")
    return value


def make_request(request_id: str, method: str, params: dict[str, Any] | None = None,
                 workspace_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    if workspace_id:
        payload["workspaceId"] = workspace_id
    return payload


def make_response(request_id: str, result: Any) -> dict[str, Any]:
    return {"type": "response", "id": request_id, "ok": True, "result": result}


def make_error_response(request_id: str, error: BridgeError) -> dict[str, Any]:
    return {"type": "response", "id": request_id, "ok": False, "error": error.to_dict()}


def make_event(event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "event", "event": event, "data": data or {}}
