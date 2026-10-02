"""Общие фикстуры тестов: путь до пакета, фейковый OBS, временный конфиг."""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fakes import FakeObs, FakeObsServer, FakeProc  # noqa: E402


@pytest.fixture
def fake_proc(monkeypatch):
    """Подменяет subprocess.Popen фейком и очищает список созданных процессов."""
    FakeProc.instances.clear()
    return FakeProc


@pytest.fixture
async def obs_server():
    """Поднятый фейковый obs-websocket; по умолчанию без пароля."""
    servers: list[FakeObsServer] = []

    async def make(**kwargs) -> FakeObsServer:
        srv = FakeObsServer(**kwargs)
        await srv.start()
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        await srv.stop()


@pytest.fixture
def fake_obs() -> FakeObs:
    """Стаб клиента OBS (для тестов Panel/команд, без сети)."""
    return FakeObs()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Изолированный каталог состояния (config.json, journal.jsonl)."""
    return tmp_path
