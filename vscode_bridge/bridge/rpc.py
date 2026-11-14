"""Request/response correlation for one WebSocket connection."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .protocol import BridgeError


class RpcTimeoutError(BridgeError):
    def __init__(self, message: str = "Request timed out"):
        super().__init__("CANCELLED", message)


@dataclass
class PendingRequest:
    future: asyncio.Future[Any]
    method: str
    created_at: float


class RpcRegistry:
    """Owns the futures for requests sent to one extension connection."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingRequest] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def create(self, request_id: str, method: str) -> asyncio.Future[Any]:
        if request_id in self._pending:
            raise BridgeError("INTERNAL_ERROR", f"Duplicate request id: {request_id}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = PendingRequest(future=future, method=method, created_at=time.time())
        return future

    def pop(self, request_id: str) -> PendingRequest | None:
        return self._pending.pop(request_id, None)

    def resolve(self, request_id: str, result: Any) -> bool:
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(result)
        return True

    def reject(self, request_id: str, error: BridgeError) -> bool:
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_exception(error)
        return True

    def cancel(self, request_id: str, message: str = "Request cancelled") -> bool:
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.cancel()
        return True

    def cancel_all(self, message: str = "Connection closed") -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_exception(BridgeError("CANCELLED", message))
