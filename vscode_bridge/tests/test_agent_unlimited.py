from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from vscode_bridge.bridge.agent import CodingAgent
from vscode_bridge.bridge.settings import BridgeSettings
from vscode_bridge.bridge.tasks import TaskManager


class FakeConnection:
    async def request(self, method: str, params: dict | None = None, timeout: float = 30.0):
        if method == "file.read":
            return {"path": (params or {}).get("path"), "text": "print('x')\n", "hash": "hash"}
        return {}


class FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_: object):
        self.calls += 1
        if self.calls <= 30:
            call = SimpleNamespace(
                id=f"call_{self.calls}",
                function=SimpleNamespace(
                    name="read_file",
                    arguments='{"path":"src/App.tsx"}',
                ),
            )
            message = SimpleNamespace(content=None, tool_calls=[call])
        else:
            message = SimpleNamespace(content="done", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


@pytest.mark.asyncio
async def test_agent_runs_beyond_twenty_four_steps_when_unlimited() -> None:
    tasks = TaskManager()
    task = tasks.create(
        workspace_id="ws_test",
        request="loop",
        mode="dedicated",
        permission_mode="ask",
        max_steps=0,
    )
    settings = BridgeSettings(
        max_steps=0,
        task_timeout_seconds=0,
        model_timeout_seconds=0,
        heartbeat_seconds=60,
    )
    agent = CodingAgent(settings=settings, logger=None)  # type: ignore[arg-type]
    result = await agent.run(
        connection=FakeConnection(),  # type: ignore[arg-type]
        task=task,
        model_client=FakeClient(),
        request="loop",
    )
    assert task.step == 31
    assert result["summary"] == "done"
    assert task.activity
    assert len(task.activity) <= 40
    assert any(event["phase"] == "tool_result" for event in task.activity)
    assert task.phase == "finishing"


def test_task_phase_and_elapsed_snapshot() -> None:
    tasks = TaskManager()
    task = tasks.create(
        workspace_id="ws_test",
        request="x",
        mode="dedicated",
        permission_mode="ask",
        max_steps=0,
    )
    task.set_phase("thinking", "模型正在思考", tool="read_file", detail="src/App.tsx")
    data = task.to_dict()
    assert data["phase"] == "thinking"
    assert data["max_steps"] == 0
    assert data["last_tool"] == "read_file"
    assert data["elapsed_seconds"] >= 0
    assert data["phase_elapsed_seconds"] >= 0
    assert data["activity"][-1]["message"] == "模型正在思考"

    tasks.complete(task.task_id, summary="ok", files_changed=["src/App.tsx"])
    completed = task.to_dict()
    assert completed["phase"] == "completed"
    assert completed["finished_at"] is not None
    assert completed["activity"][-1]["phase"] == "completed"
