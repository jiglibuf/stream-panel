"""Общие помощники HTTP-тестов PR4: сборка стека и клиент TestServer/TestClient.

Здесь нет живого OBS: Panel/Commands работают поверх FakeObs, поэтому весь
HTTP-слой проверяется на локальных сокетах (127.0.0.1) без сети наружу.
"""
from __future__ import annotations

import contextlib

from aiohttp.test_utils import TestClient, TestServer

from server.app import create_app
from server.commands import Commands
from server.panel import Panel
from tests.fakes import FakeObs

TOKEN = "test-token-123abc"

# Ответы FakeObs, достаточные для полного снапшота Panel.
RESPONSES = {
    "GetSceneList": {
        "currentProgramSceneName": "Dota 2",
        "scenes": [{"sceneName": "Dota 2"}, {"sceneName": "WoW"}],
    },
    "GetStreamStatus": {
        "outputActive": False, "outputTimecode": "00:00:00.000", "outputReconnecting": False,
    },
    "GetRecordStatus": {"outputActive": False, "outputTimecode": "00:00:00.000"},
    "GetReplayBufferStatus": {"outputActive": True},
    "GetInputMute": lambda **data: {"inputMuted": False},
    "GetInputVolume": lambda **data: {"inputVolumeMul": 1.0},
}


async def make_stack(*, token: str = TOKEN, interval: float = 0.05,
                     frontend_dir=None, refresh: bool = True, journal=None):
    """Собрать (app, obs, panel) и, по умолчанию, наполнить снапшот."""
    obs = FakeObs(responses=RESPONSES)
    panel = Panel(obs, refresh_interval=5.0, meter_interval=5.0)
    commands = Commands(obs, panel)
    app = create_app(obs=obs, panel=panel, commands=commands, token=token,
                     frontend_dir=frontend_dir, push_interval=interval, journal=journal)
    if refresh:
        await panel.refresh()
    return app, obs, panel


def auth(token: str = TOKEN) -> dict:
    """Заголовок доступа для /api/*."""
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def running(app):
    """Поднять приложение на loopback-сокете и отдать TestClient."""
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
