"""Проверка самой тестовой инфраструктуры: фейковый obs-websocket ведёт себя как протокол v5.

Если эти тесты красные — любой красный тест продукта ничего не значит: сначала чиним двойника.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from tests.fakes import compute_auth

pytestmark = pytest.mark.integration


async def _handshake(url: str, password: str, salt: str, challenge: str, rpc: int = 1):
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"])
    hello = json.loads(await ws.recv())
    assert hello["op"] == 0
    auth = None
    if hello["d"].get("authentication"):
        auth = compute_auth(password, salt, challenge)
    await ws.send(json.dumps({"op": 1, "d": {"rpcVersion": rpc, "authentication": auth}}))
    return ws, json.loads(await asyncio.wait_for(ws.recv(), timeout=2))


async def test_handshake_with_auth(obs_server):
    srv = await obs_server(password="секрет", salt="s1", challenge="c1")
    ws, identified = await _handshake(srv.url, "секрет", "s1", "c1")
    assert identified["op"] == 2
    assert srv.identified[0]["authentication"] == compute_auth("секрет", "s1", "c1")
    await ws.close()
    await srv.stop()


async def test_handshake_without_password(obs_server):
    srv = await obs_server()
    ws, identified = await _handshake(srv.url, "", "", "")
    assert identified["op"] == 2
    assert srv.identified[0]["authentication"] is None
    await ws.close()
    await srv.stop()


async def test_wrong_password_is_rejected(obs_server):
    srv = await obs_server(password="верный", salt="s", challenge="c")
    with pytest.raises(websockets.exceptions.ConnectionClosed):
        ws, _ = await _handshake(srv.url, "неверный", "s", "c")
        await asyncio.wait_for(ws.recv(), timeout=2)
    await srv.stop()


async def test_request_response_and_event(obs_server):
    srv = await obs_server(responses={"GetSceneList": {"responseData": {
        "currentProgramSceneName": "Dota 2",
        "scenes": [{"sceneName": "Dota 2"}]}}})
    ws, _ = await _handshake(srv.url, "", "", "")
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "GetSceneList", "requestId": "r1"}}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
    assert reply["op"] == 7
    assert reply["d"]["requestStatus"]["result"] is True
    assert reply["d"]["responseData"]["currentProgramSceneName"] == "Dota 2"
    assert srv.requests_of("GetSceneList") == [{"requestType": "GetSceneList", "requestId": "r1"}]

    await srv.emit("CurrentProgramSceneChanged", sceneName="WoW")
    event = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
    assert event["op"] == 5 and event["d"]["eventType"] == "CurrentProgramSceneChanged"
    await ws.close()
    await srv.stop()


async def test_refusal_carries_result_false(obs_server):
    srv = await obs_server(responses={"StopStream": {"requestStatus": {
        "result": False, "code": 500, "comment": "not streaming"}}})
    ws, _ = await _handshake(srv.url, "", "", "")
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "StopStream", "requestId": "r2"}}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
    assert reply["d"]["requestStatus"]["result"] is False
    assert reply["d"]["requestStatus"]["comment"] == "not streaming"
    await ws.close()
    await srv.stop()


async def test_drop_after_request_kills_connection(obs_server):
    srv = await obs_server(drop_after=0)
    ws, _ = await _handshake(srv.url, "", "", "")
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "GetSceneList", "requestId": "r3"}}))
    with pytest.raises(websockets.exceptions.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout=2)
    await srv.stop()


async def test_silent_server_does_not_answer(obs_server):
    srv = await obs_server(silent=True)
    ws, _ = await _handshake(srv.url, "", "", "")
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "GetSceneList", "requestId": "r4"}}))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.recv(), timeout=0.5)
    await ws.close()
    await srv.stop()
