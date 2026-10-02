"""Живые уровни звука: только через настоящее событие InputVolumeMeters.

В obs-websocket v5 нет запроса для метроб — уровни приходят событием op5
`InputVolumeMeters` (интент 1 << 16). Эти тесты гоняют настоящий клиент `Obs`
против честного фейкового obs-websocket: на неизвестный запрос тот отвечает
отказом 204, как настоящий OBS, поэтому выдуманный `GetInputVolumeMeters` здесь
не пройдёт — именно так баг и всплыл на живом прогоне.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from server.obs import Obs
from server.panel import MIC, SYS, Panel

pytestmark = pytest.mark.integration

METER_INTENT = 1 << 16


async def _wait_until(pred, timeout: float = 2.0, what: str = "условие"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() >= deadline:
            raise AssertionError(f"не дождались: {what}")
        await asyncio.sleep(0.01)


def _levels(name: str, *magnitudes) -> dict:
    """Вход метроб: на канал по три числа (магнитуда, пик, inputPeak)."""
    return {"inputName": name,
            "inputLevelsMul": [[m, m, m] for m in magnitudes]}


async def _run(obs: Obs):
    """Поднять run() в фоне и гарантированно погасить при выходе."""
    task = asyncio.create_task(obs.run())
    try:
        await _wait_until(lambda: obs.connected, what="подключение")
    except BaseException:
        await obs.close()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    return task


async def _stop(obs: Obs, task) -> None:
    await obs.close()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


# 1. рукопожатие подписывает на бит метроб, не теряя прежние события
async def test_handshake_subscribes_to_meter_intent(obs_server):
    srv = await obs_server()
    obs = Obs(port=srv.port, retry_delay=0.05)
    task = await _run(obs)
    try:
        subs = srv.identified[0].get("eventSubscriptions")
        assert subs & METER_INTENT == METER_INTENT, subs
        assert subs & 1023 == 1023, subs
    finally:
        await _stop(obs, task)


# 2. событие InputVolumeMeters доезжает через on_event до снапшота Panel
async def test_event_from_socket_sets_levels(obs_server):
    srv = await obs_server()
    obs = Obs(port=srv.port, retry_delay=0.05)
    panel = Panel(obs, refresh_interval=10.0, meter_stale_after=5.0)
    obs.on_event = panel.on_event
    task = await _run(obs)
    try:
        await srv.emit("InputVolumeMeters", inputs=[
            _levels(MIC, 0.3), _levels(SYS, 0.6)])
        await _wait_until(
            lambda: panel.state["audio"]["mic"]["level"] == 0.3,
            what="уровень микрофона из события")
        snap = panel.state
        assert snap["audio"]["mic"]["level"] == 0.3
        assert snap["audio"]["sys"]["level"] == 0.6
    finally:
        await _stop(obs, task)


# 3. честный фейк, как настоящий OBS, не знает запроса метроб -> отказ 204
async def test_unknown_meter_request_is_rejected(obs_server):
    srv = await obs_server()
    obs = Obs(port=srv.port, retry_delay=0.05)
    task = await _run(obs)
    try:
        with pytest.raises(RuntimeError, match="not valid"):
            await obs.req("GetInputVolumeMeters")
    finally:
        await _stop(obs, task)
