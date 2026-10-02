"""Цикл Panel.run(): метры и полный рефреш крутятся параллельно и чисто отменяются.

Без сети и живого OBS: только FakeObs. Все ожидания — с явными таймаутами.
"""
from __future__ import annotations

import asyncio

import pytest

from server.panel import Panel
from tests.fakes import FakeObs

pytestmark = pytest.mark.unit


async def test_run_refreshes_then_cancels_cleanly():
    fake = FakeObs(responses={"GetSceneList": {
        "currentProgramSceneName": "Dota 2",
        "scenes": [{"sceneName": "Dota 2"}]}})
    panel = Panel(fake, refresh_interval=0.05)

    task = asyncio.create_task(panel.run())
    await asyncio.wait_for(asyncio.sleep(0.3), timeout=1.0)
    task.cancel()
    done = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)

    assert task.cancelled() or isinstance(done[0], asyncio.CancelledError)
    assert len(fake.called("GetSceneList")) >= 2
    assert fake.connected is True  # живой OBS в тесте не участвует


async def test_run_stops_promptly_on_cancel():
    fake = FakeObs(responses={"GetSceneList": {
        "currentProgramSceneName": "Dota 2", "scenes": [{"sceneName": "Dota 2"}]}})
    panel = Panel(fake, refresh_interval=0.5)

    task = asyncio.create_task(panel.run())
    await asyncio.wait_for(asyncio.sleep(0.02), timeout=1.0)
    task.cancel()
    done = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)

    assert task.cancelled() or isinstance(done[0], asyncio.CancelledError)
