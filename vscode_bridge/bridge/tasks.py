"""In-memory task records and lifecycle helpers."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


ACTIVE_STATUSES = {"queued", "running", "waiting_approval"}


@dataclass
class TaskRecord:
    task_id: str
    workspace_id: str
    request: str
    mode: str
    permission_mode: str
    status: str = "queued"
    step: int = 0
    max_steps: int = 0
    phase: str = "queued"
    phase_started_at: float = field(default_factory=time.time)
    last_tool: str = ""
    last_tool_detail: str = ""
    activity: list[dict[str, Any]] = field(default_factory=list)
    event_seq: int = 0
    message: str = ""
    files_changed: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    summary: str = ""
    tests: str = ""
    remaining_issues: str = ""
    notified: bool = False
    sync_wait_until: float = 0.0
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None

    def set_phase(
        self,
        phase: str,
        message: str = "",
        *,
        tool: str = "",
        detail: str = "",
        ok: bool | None = None,
    ) -> None:
        now = time.time()
        self.phase = phase
        self.phase_started_at = now
        self.updated_at = now
        self.message = message
        if tool:
            self.last_tool = tool
            self.last_tool_detail = detail[:300]
        self.event_seq += 1
        self.activity.append({
            "id": f"evt_{self.event_seq}",
            "at": now,
            "phase": phase,
            "message": message,
            "tool": tool,
            "detail": detail[:300],
            "ok": ok,
        })
        if len(self.activity) > 40:
            del self.activity[:-40]

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        end = self.finished_at if self.finished_at is not None else now
        return {
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "request": self.request[:500],
            "mode": self.mode,
            "permission_mode": self.permission_mode,
            "status": self.status,
            "phase": self.phase,
            "step": self.step,
            "max_steps": self.max_steps,
            "message": self.message,
            "elapsed_seconds": max(0.0, end - self.started_at),
            "phase_elapsed_seconds": max(0.0, end - self.phase_started_at),
            "last_tool": self.last_tool,
            "last_tool_detail": self.last_tool_detail,
            "activity": [dict(item) for item in self.activity],
            "files_changed": list(self.files_changed),
            "diagnostics": dict(self.diagnostics),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "summary": self.summary,
            "tests": self.tests,
            "remaining_issues": self.remaining_issues,
        }


class TaskManager:
    def __init__(self, *, max_history: int = 20):
        self.max_history = max_history
        self._records: dict[str, TaskRecord] = {}
        self._history: list[str] = []

    def create(self, *, workspace_id: str, request: str, mode: str,
               permission_mode: str, max_steps: int) -> TaskRecord:
        if self.is_busy(workspace_id):
            current = self.current_for_workspace(workspace_id)
            raise RuntimeError(
                f"工作区 {workspace_id} 正在执行任务 {current.task_id if current else 'unknown'}"
            )
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        record = TaskRecord(
            task_id=task_id,
            workspace_id=workspace_id,
            request=request,
            mode=mode,
            permission_mode=permission_mode,
            max_steps=max_steps,
        )
        record.set_phase("queued", "排队等待执行")
        self._records[task_id] = record
        return record

    def attach(self, task_id: str, task: asyncio.Task[Any]) -> None:
        record = self.get(task_id)
        if record is not None:
            record.task = task

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def current_for_workspace(self, workspace_id: str) -> TaskRecord | None:
        for record in self._records.values():
            if record.workspace_id == workspace_id and record.status in ACTIVE_STATUSES:
                return record
        return None

    def active(self) -> list[TaskRecord]:
        return [record for record in self._records.values() if record.status in ACTIVE_STATUSES]

    def current(self) -> TaskRecord | None:
        active = self.active()
        if not active:
            return None
        return max(active, key=lambda item: item.updated_at)

    def is_busy(self, workspace_id: str) -> bool:
        return self.current_for_workspace(workspace_id) is not None

    def update(self, task_id: str, **fields: Any) -> TaskRecord | None:
        record = self.get(task_id)
        if record is None:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = time.time()
        return record

    def complete(self, task_id: str, *, summary: str = "", files_changed: list[str] | None = None,
                 diagnostics: dict[str, Any] | None = None, tests: str = "",
                 remaining_issues: str = "") -> TaskRecord | None:
        record = self.get(task_id)
        if record is None:
            return None
        record.status = "completed"
        record.summary = summary
        record.files_changed = files_changed or record.files_changed
        record.diagnostics = diagnostics or record.diagnostics
        record.tests = tests
        record.remaining_issues = remaining_issues
        record.finished_at = time.time()
        record.set_phase("completed", "完成", ok=True)
        self._remember(task_id)
        return record

    def fail(self, task_id: str, error: str) -> TaskRecord | None:
        record = self.get(task_id)
        if record is None:
            return None
        record.status = "failed"
        record.error = error
        record.finished_at = time.time()
        record.set_phase("failed", error, ok=False)
        self._remember(task_id)
        return record

    def mark_cancelled(self, task_id: str) -> TaskRecord | None:
        record = self.get(task_id)
        if record is None:
            return None
        record.status = "cancelled"
        record.finished_at = time.time()
        record.set_phase("cancelled", "任务已取消", ok=False)
        self._remember(task_id)
        return record

    async def cancel(self, task_id: str | None = None) -> list[str]:
        targets: list[TaskRecord]
        if task_id:
            record = self.get(task_id)
            targets = [record] if record is not None else []
        else:
            targets = self.active()
        cancelled: list[str] = []
        for record in targets:
            if record is None or record.status not in ACTIVE_STATUSES:
                continue
            record.cancel_event.set()
            if record.task is not None and not record.task.done():
                record.task.cancel()
            self.mark_cancelled(record.task_id)
            cancelled.append(record.task_id)
        return cancelled

    async def cancel_all(self) -> None:
        await self.cancel(None)

    def fail_workspace(self, workspace_id: str, message: str) -> list[str]:
        failed: list[str] = []
        for record in self.active():
            if record.workspace_id != workspace_id:
                continue
            record.cancel_event.set()
            if record.task is not None and not record.task.done():
                record.task.cancel()
            self.fail(record.task_id, message)
            failed.append(record.task_id)
        return failed

    def history(self, limit: int = 5) -> list[TaskRecord]:
        records = [self._records[task_id] for task_id in self._history if task_id in self._records]
        return records[-limit:][::-1]

    def history_dicts(self, limit: int = 5) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.history(limit)]

    def clear_history(self) -> None:
        self._history.clear()

    def _remember(self, task_id: str) -> None:
        if task_id in self._history:
            self._history.remove(task_id)
        self._history.append(task_id)
        while len(self._history) > self.max_history:
            oldest = self._history.pop(0)
            if oldest not in self._history:
                self._records.pop(oldest, None)
