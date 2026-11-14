"""Validated runtime settings for the VSCode bridge plugin."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BridgeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    port: int = Field(default=45821, ge=0, le=65535)
    discovery_dir: str = ""
    heartbeat_seconds: int = Field(default=15, ge=5, le=120)
    stale_after_seconds: int = Field(default=45, ge=15, le=600)
    sync_wait_seconds: int = Field(default=20, ge=5, le=60)
    model_mode: Literal["auto", "dedicated", "main"] = "auto"
    permission_mode: Literal["ask", "auto_edit", "full_access"] = "ask"
    allow_terminal: bool = True
    coding_takeover_mode: Literal["off", "suggest", "auto"] = "auto"
    max_steps: int = Field(default=0, ge=0, le=100000)
    max_context_chars: int = Field(default=120000, ge=10000, le=500000)
    max_repair_rounds: int = Field(default=3, ge=0, le=10)
    task_timeout_seconds: int = Field(default=0, ge=0, le=86400)
    model_timeout_seconds: int = Field(default=1800, ge=0, le=86400)
    command_timeout_seconds: int = Field(default=120, ge=5, le=600)
    approval_timeout_seconds: int = Field(default=900, ge=60, le=7200)
    allow_sensitive_files: bool = False
    exclude_globs: list[str] = Field(default_factory=lambda: [
        "**/.git/**",
        "**/node_modules/**",
        "**/dist/**",
        "**/build/**",
        "**/.venv/**",
    ])

    @field_validator("discovery_dir")
    @classmethod
    def _strip_discovery_dir(cls, value: str) -> str:
        return value.strip()

    @classmethod
    def from_config(cls, value: object) -> "BridgeSettings":
        if isinstance(value, BridgeSettings):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
