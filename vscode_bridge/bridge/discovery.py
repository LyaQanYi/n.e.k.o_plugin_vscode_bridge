"""Discovery-file handling for the local WebSocket server."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import PROTOCOL_VERSION


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_discovery_dir() -> Path:
    return Path.home() / ".neko-vscode" / "ide"


@dataclass
class DiscoveryInfo:
    port: int
    token: str
    pid: int
    instance_id: str
    plugin_version: str
    started_at: str
    updated_at: str
    protocol: int = PROTOCOL_VERSION
    v: int = 1
    remote_authority: str | None = None
    external_uri: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "protocol": self.protocol,
            "port": self.port,
            "token": self.token,
            "pid": self.pid,
            "instance_id": self.instance_id,
            "plugin_version": self.plugin_version,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "remote_authority": self.remote_authority,
            "external_uri": self.external_uri,
        }


class DiscoveryWriter:
    """Writes one discovery file per plugin process, atomically and with 0600."""

    def __init__(
        self,
        *,
        directory: str | Path | None,
        token: str,
        instance_id: str,
        plugin_version: str,
    ):
        self.directory = Path(directory).expanduser().resolve() if directory else default_discovery_dir()
        self.token = token
        self.instance_id = instance_id
        self.plugin_version = plugin_version
        self.started_at = utc_now_iso()
        self.path = self.directory / f"neko-{os.getpid()}.json"
        self._last_port: int | None = None

    def write(self, port: int) -> Path:
        self._last_port = port
        self.directory.mkdir(parents=True, exist_ok=True)
        info = DiscoveryInfo(
            port=port,
            token=self.token,
            pid=os.getpid(),
            instance_id=self.instance_id,
            plugin_version=self.plugin_version,
            started_at=self.started_at,
            updated_at=utc_now_iso(),
        )
        payload = json.dumps(info.to_dict(), ensure_ascii=False, separators=(",", ":"))
        fd, temp_name = tempfile.mkstemp(prefix=".neko-discovery-", dir=str(self.directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, self.path)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass
        return self.path

    def touch(self) -> None:
        if self._last_port is not None:
            self.write(self._last_port)

    def remove(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass


def read_discovery_file(path: str | Path) -> dict[str, Any] | None:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def is_stale(info: dict[str, Any], *, stale_after_seconds: float = 45.0) -> bool:
    raw = info.get("updated_at")
    if not isinstance(raw, str):
        return True
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (time.time() - parsed.timestamp()) > stale_after_seconds
