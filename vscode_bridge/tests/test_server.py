from __future__ import annotations

import asyncio
import json
import logging

import pytest
import websockets

from vscode_bridge.bridge.discovery import read_discovery_file
from vscode_bridge.bridge.server import BridgeServer
from vscode_bridge.bridge.settings import BridgeSettings


def _hello(token: str) -> dict:
    return {
        "type": "hello",
        "v": 1,
        "token": token,
        "client": {"name": "vscode", "version": "0.1.0", "platform": "test"},
        "workspace": {
            "id": "ws_test",
            "name": "test",
            "windowId": "window_test",
            "roots": [{"name": "test", "uri": "file:///tmp/test", "path": "/tmp/test"}],
            "workspaceFile": None,
            "isTrusted": True,
            "remoteName": None,
        },
    }


@pytest.mark.asyncio
async def test_handshake_and_request(tmp_path):
    settings = BridgeSettings(
        port=0,
        discovery_dir=str(tmp_path),
        heartbeat_seconds=60,
        stale_after_seconds=120,
    )
    server = BridgeServer(
        settings=settings,
        logger=logging.getLogger("test.bridge"),
        fallback_discovery_dir=str(tmp_path),
    )
    await server.start()
    try:
        assert server.discovery_path is not None
        info = read_discovery_file(server.discovery_path)
        assert info is not None
        token = info["token"]
        ready = asyncio.Event()

        async def client() -> None:
            async with websockets.connect(f"ws://127.0.0.1:{server.port}") as websocket:
                await websocket.send(json.dumps(_hello(token)))
                ack = json.loads(await websocket.recv())
                assert ack["type"] == "hello_ack"
                ready.set()
                message = json.loads(await asyncio.wait_for(websocket.recv(), 5))
                assert message["type"] == "request"
                assert message["method"] == "workspace.info"
                await websocket.send(json.dumps({
                    "type": "response",
                    "id": message["id"],
                    "ok": True,
                    "result": {"roots": ["/tmp/test"]},
                }))

        client_task = asyncio.create_task(client())
        await asyncio.wait_for(ready.wait(), 5)
        result = await server.request(None, "workspace.info", {}, timeout=5)
        assert result == {"roots": ["/tmp/test"]}
        await asyncio.wait_for(client_task, 5)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_bad_token_is_rejected(tmp_path):
    settings = BridgeSettings(port=0, discovery_dir=str(tmp_path), heartbeat_seconds=60)
    server = BridgeServer(
        settings=settings,
        logger=logging.getLogger("test.bridge"),
        fallback_discovery_dir=str(tmp_path),
    )
    await server.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{server.port}") as websocket:
            await websocket.send(json.dumps(_hello("wrong-token")))
            message = json.loads(await asyncio.wait_for(websocket.recv(), 5))
            assert message["type"] == "hello_error"
            assert message["code"] == "AUTH_FAILED"
    finally:
        await server.stop()


def test_stale_loop_server_can_be_recreated(tmp_path):
    logger = logging.getLogger("test.bridge")

    async def create_stale():
        server = BridgeServer(
            settings=BridgeSettings(
                port=0,
                discovery_dir=str(tmp_path),
                heartbeat_seconds=60,
            ),
            logger=logger,
            fallback_discovery_dir=str(tmp_path),
        )
        await server.start()
        return server

    stale = asyncio.run(create_stale())
    assert stale.running
    assert stale.loop is not None
    assert stale.loop.is_closed()

    async def recover():
        assert stale.loop is not asyncio.get_running_loop()
        stale.force_close()
        assert not stale.running
        fresh = BridgeServer(
            settings=BridgeSettings(
                port=0,
                discovery_dir=str(tmp_path),
                heartbeat_seconds=60,
            ),
            logger=logger,
            fallback_discovery_dir=str(tmp_path),
        )
        await fresh.start()
        try:
            info = read_discovery_file(fresh.discovery_path)
            assert info is not None
            async with websockets.connect(f"ws://127.0.0.1:{fresh.port}") as websocket:
                await websocket.send(json.dumps(_hello(info["token"])))
                ack = json.loads(await asyncio.wait_for(websocket.recv(), 5))
                assert ack["type"] == "hello_ack"
        finally:
            await fresh.stop()

    asyncio.run(recover())
