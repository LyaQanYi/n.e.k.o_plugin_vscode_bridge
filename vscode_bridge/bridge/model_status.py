"""Read-only lookup of the plugin's model-slot binding."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx


def _resolve_base_url() -> str:
    try:
        from config.network import resolve_user_plugin_base

        return resolve_user_plugin_base()
    except Exception:
        port = os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "").strip()
        if port.isdigit():
            return f"http://127.0.0.1:{port}"
        return "http://127.0.0.1:48916"


class ModelStatusProvider:
    def __init__(self, *, ttl_seconds: float = 5.0):
        self.ttl_seconds = ttl_seconds
        self._cache_at = 0.0
        self._cache: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cache_at = 0.0
        self._cache = None

    async def fetch(self, plugin_id: str = "vscode_bridge",
                    base_url: str | None = None) -> dict[str, Any]:
        async with self._lock:
            now = time.time()
            if self._cache is not None and now - self._cache_at < self.ttl_seconds:
                return dict(self._cache)
            result = await self._fetch_uncached(plugin_id, base_url or _resolve_base_url())
            self._cache = result
            self._cache_at = time.time()
            return dict(result)

    async def is_ready(self, plugin_id: str = "vscode_bridge") -> bool:
        status = await self.fetch(plugin_id)
        return bool(status.get("ready")) and status.get("status") == "bound"

    async def _fetch_uncached(self, plugin_id: str, base_url: str) -> dict[str, Any]:
        base = base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3.0) as client:
                bindings_response = await client.get(
                    f"{base}/api/model-config/plugins/{plugin_id}/bindings"
                )
                bindings_response.raise_for_status()
                bindings = bindings_response.json()
                slots_response = await client.get(f"{base}/api/model-config/slots")
                slots_response.raise_for_status()
                slots = slots_response.json()
        except Exception as exc:
            return {
                "usage": "coding",
                "status": "unknown",
                "ready": False,
                "slot_id": None,
                "slot_name": None,
                "model": None,
                "capabilities": [],
                "error": str(exc),
            }

        requirements = bindings.get("requirements") if isinstance(bindings, dict) else {}
        coding = requirements.get("coding") if isinstance(requirements, dict) else None
        if not isinstance(coding, dict):
            return {
                "usage": "coding",
                "status": "unbound",
                "ready": False,
                "slot_id": None,
                "slot_name": None,
                "model": None,
                "capabilities": [],
                "error": None,
            }

        status = str(coding.get("status") or "unbound")
        slot_id = coding.get("slot_id")
        slot_info: dict[str, Any] = {}
        if isinstance(slots, dict):
            for item in slots.get("slots") or []:
                if isinstance(item, dict) and item.get("id") == slot_id:
                    slot_info = item
                    break

        error = None
        if status == "unbound":
            error = "还没有绑定 Coding 模型"
        elif status == "incompatible":
            error = "绑定的模型不满足 Coding 模型能力要求"

        return {
            "usage": "coding",
            "status": status,
            "ready": bool(bindings.get("ready")) and status == "bound",
            "slot_id": slot_id,
            "slot_name": slot_info.get("name"),
            "model": slot_info.get("model"),
            "capabilities": list(slot_info.get("capabilities") or coding.get("capabilities") or []),
            "error": error,
        }
