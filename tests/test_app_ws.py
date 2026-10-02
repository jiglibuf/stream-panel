"""WebSocket пульта: доступ, первый снапшот, ping/pong, рассылка (integration)."""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from server.app import HUB_KEY
from tests.http_support import TOKEN, auth, make_stack, running

pytestmark = pytest.mark.integration


async def _recv_until(ws, kind: str, timeout: float = 2.0):
    async def loop():
        while True:
            data = await ws.receive_json()
            if data.get("type") == kind:
                return data

    return await asyncio.wait_for(loop(), timeout=timeout)


async def test_ws_without_token_rejected():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await session.ws_connect(cl.server.make_url("/ws"))


async def test_ws_wrong_token_rejected():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await session.ws_connect(cl.server.make_url("/ws?token=не-тот"))


async def test_ws_first_state_ping_pong_and_command_broadcast():
    # Большой интервал: после первого снапшота состояние может прийти только
    # от немедленной рассылки после команды, а не от циклa.
    app, obs, panel = await make_stack(interval=5.0)
    async with running(app) as cl:
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(cl.server.make_url("/ws?token=" + TOKEN))

            first = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert first["type"] == "state"
            assert first["state"]["obs"] is True
            assert {"scenes", "stream", "audio", "stale"} <= set(first["state"])

            await ws.send_json({"type": "ping", "t": 17})
            pong = await _recv_until(ws, "pong")
            assert pong["t"] == 17

            resp = await cl.post("/api/cmd",
                                 json={"name": "audio.toggleMute", "args": {"track": "mic"}},
                                 headers=auth())
            assert resp.status == 200

            broadcast = await _recv_until(ws, "state", timeout=2.0)
            assert broadcast["type"] == "state" and "state" in broadcast

            await ws.close()


async def test_ws_periodic_push():
    """Цикл рассылки сам присылает свежий снапшот без запросов клиента."""
    app, obs, panel = await make_stack(interval=0.05)
    async with running(app) as cl:
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(cl.server.make_url("/ws?token=" + TOKEN))
            first = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert first["type"] == "state"
            pushed = await _recv_until(ws, "state", timeout=1.0)
            assert pushed["type"] == "state"
            await ws.close()


async def test_broadcast_survives_client_drop():
    app, obs, panel = await make_stack(interval=0.02)
    async with running(app) as cl:
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(cl.server.make_url("/ws?token=" + TOKEN))
            await asyncio.wait_for(ws.receive_json(), timeout=2)
            await ws.close()
            await asyncio.sleep(0.15)  # несколько циклов рассылки уже без клиента

            hub = app[HUB_KEY]
            assert hub._task is not None and not hub._task.done()

            ws2 = await session.ws_connect(cl.server.make_url("/ws?token=" + TOKEN))
            first = await asyncio.wait_for(ws2.receive_json(), timeout=2)
            assert first["type"] == "state"
            await ws2.close()
