"""Клиент obs-websocket v5 против фейкового сервера (integration, локальные сокеты)."""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from server.obs import Obs, compute_auth

pytestmark = pytest.mark.integration


async def _wait_until(pred, timeout: float = 2.0, what: str = "условие"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() >= deadline:
            raise AssertionError(f"не дождались: {what}")
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running(obs: Obs):
    """Гоняет run() в фоне и гарантированно останавливает его при выходе."""
    task = asyncio.create_task(obs.run())
    try:
        yield obs
    finally:
        await obs.close()
        try:
            await asyncio.wait_for(task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# 2. рукопожатие с паролем + connected
async def test_connects_with_password(obs_server):
    srv = await obs_server(password="секрет", salt="s1", challenge="c1")
    async with running(Obs(port=srv.port, password="секрет", retry_delay=0.05)) as obs:
        await _wait_until(lambda: obs.connected, what="подключение")
        assert obs.connected is True
        assert srv.identified[0]["authentication"] == compute_auth("секрет", "s1", "c1")


# 3. requestData/responseData
async def test_req_returns_response_data(obs_server):
    srv = await obs_server(responses={"GetSceneList": {"responseData": {
        "currentProgramSceneName": "Dota 2",
        "scenes": [{"sceneName": "Dota 2"}]}}})
    async with running(Obs(port=srv.port, retry_delay=0.05)) as obs:
        await _wait_until(lambda: obs.connected, what="подключение")
        data = await obs.req("GetSceneList")
        assert data == {"currentProgramSceneName": "Dota 2", "scenes": [{"sceneName": "Dota 2"}]}
        sent = srv.requests_of("GetSceneList")
        assert len(sent) == 1
        assert sent[0]["requestType"] == "GetSceneList"
        assert sent[0]["requestId"]


# 4. отказ OBS
async def test_refusal_raises_runtime_error(obs_server):
    srv = await obs_server(responses={"StopStream": {"requestStatus": {
        "result": False, "code": 500, "comment": "not streaming"}}})
    async with running(Obs(port=srv.port, retry_delay=0.05)) as obs:
        await _wait_until(lambda: obs.connected, what="подключение")
        with pytest.raises(RuntimeError) as err:
            await obs.req("StopStream")
        assert "not streaming" in str(err.value)
        assert obs.waiters == 0


# 5. запрос без подключения
async def test_req_offline_raises():
    obs = Obs(port=1, retry_delay=0.05)
    with pytest.raises(RuntimeError, match="OBS офлайн"):
        await obs.req("GetSceneList")


# 6. обрыв связи во время запроса -> ConnectionError, а не таймаут через 8с
async def test_drop_during_request_is_connection_error(obs_server):
    srv = await obs_server(drop_after=0)
    obs = Obs(port=srv.port, request_timeout=8.0, retry_delay=0.05)
    async with running(obs):
        await _wait_until(lambda: obs.connected, what="подключение")
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(ConnectionError):
            await obs.req("GetSceneList")
        elapsed = loop.time() - start
        assert elapsed < 1.0, f"запрос ждал {elapsed:.2f}с — похоже на таймаут, а не на обрыв"
        assert obs.waiters == 0


# 7. молчащий сервер -> TimeoutError за request_timeout, ожидания не текут
async def test_silent_server_times_out(obs_server):
    srv = await obs_server(silent=True)
    obs = Obs(port=srv.port, request_timeout=0.3, retry_delay=0.05)
    async with running(obs):
        await _wait_until(lambda: obs.connected, what="подключение")
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await obs.req("GetSceneList")
        elapsed = loop.time() - start
        assert 0.2 <= elapsed < 2.0, f"таймаут сработал за {elapsed:.2f}с, ожидали ~0.3с"
        assert obs.waiters == 0


# 8. события op5 доходят в on_event
async def test_events_reach_callback(obs_server):
    srv = await obs_server()
    seen: list[dict] = []
    obs = Obs(port=srv.port, on_event=seen.append, retry_delay=0.05)
    async with running(obs):
        await _wait_until(lambda: obs.connected, what="подключение")
        await srv.emit("CurrentProgramSceneChanged", sceneName="WoW")
        await _wait_until(lambda: any(e.get("eventType") == "CurrentProgramSceneChanged" for e in seen),
                          what="событие")
        event = next(e for e in seen if e.get("eventType") == "CurrentProgramSceneChanged")
        assert event["eventData"]["sceneName"] == "WoW"
        assert event["eventType"] == "CurrentProgramSceneChanged"


# 9. run() подключается, а после close() задача завершается без исключения
async def test_run_stops_cleanly_on_close(obs_server):
    srv = await obs_server()
    seen: list[str] = []

    async def on_event(ev):  # async-коллбэк
        seen.append(ev["type"])

    obs = Obs(port=srv.port, on_event=on_event, retry_delay=0.05)
    task = asyncio.create_task(obs.run())
    await _wait_until(lambda: obs.connected, what="подключение")
    await _wait_until(lambda: "connected" in seen, what="событие connected")
    assert "connected" in seen
    await obs.close()
    await asyncio.wait_for(task, timeout=2)  # не должно бросить
    assert not obs.connected
    assert obs.waiters == 0
