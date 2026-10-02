"""Тестовые двойники: фейковый obs-websocket v5 и фейковый процесс для pw-play.

Фейковый сервер проверен против настоящего клиента OBS: рукопожатие, auth, ответы, события.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import websockets


def compute_auth(password: str, salt: str, challenge: str) -> str:
    """Эталон obs-websocket v5: base64(sha256(base64(sha256(pw+salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class FakeObsServer:
    """Минимальный obs-websocket v5: op0 hello -> op1 identify -> op2 identified.

    responses: {requestType: {...}} — то, что вернётся в responseData/requestStatus.
    drop_after: закрыть сокет, не ответив, после N запросов (проверка обрыва).
    silent: принимать запросы и молчать (проверка таймаута).
    """

    def __init__(self, password: str = "", responses: dict | None = None,
                 salt: str = "testsalt", challenge: str = "testchallenge",
                 drop_after: int | None = None, bad_auth: bool = False,
                 silent: bool = False, version: str = "5.5.2"):
        self.password = password
        self.responses = responses or {}
        self.salt, self.challenge = salt, challenge
        self.drop_after = drop_after
        self.bad_auth = bad_auth
        self.silent = silent
        self.version = version
        self.requests: list[dict] = []
        self.identified: list[dict] = []
        self._clients: set = set()
        self._srv = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def _handler(self, ws):
        self._clients.add(ws)
        hello = {"obsWebSocketVersion": self.version, "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"challenge": self.challenge, "salt": self.salt}
        await ws.send(json.dumps({"op": 0, "d": hello}))
        try:
            ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        except Exception:
            self._clients.discard(ws)
            return
        if ident.get("op") != 1:
            self._clients.discard(ws)
            return
        payload = ident.get("d") or {}
        self.identified.append(payload)
        if self.password and payload.get("authentication") != compute_auth(
                self.password, self.salt, self.challenge):
            await ws.close(code=4009, reason="authentication failed")
            self._clients.discard(ws)
            return
        if self.bad_auth:
            await ws.close(code=4009, reason="authentication failed")
            self._clients.discard(ws)
            return
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        n = 0
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("op") != 6:
                    continue
                req = msg["d"]
                self.requests.append(req)
                n += 1
                if self.drop_after is not None and n > self.drop_after:
                    await ws.close()
                    return
                if self.silent:
                    continue
                spec = dict(self.responses.get(req["requestType"], {}))
                spec.setdefault("requestStatus", {"result": True})
                await ws.send(json.dumps({"op": 7, "d": {
                    "requestType": req["requestType"],
                    "requestId": req["requestId"], **spec}}))
        finally:
            self._clients.discard(ws)

    async def emit(self, event_type: str, **data):
        """Послать событие op5 всем подключённым клиентам."""
        payload = {"op": 5, "d": {"eventType": event_type, "eventIntent": 1, "eventData": data}}
        for ws in list(self._clients):
            await ws.send(json.dumps(payload))

    def requests_of(self, request_type: str) -> list[dict]:
        return [r for r in self.requests if r.get("requestType") == request_type]

    async def start(self) -> int:
        self._srv = await websockets.serve(self._handler, "127.0.0.1", 0,
                                           subprotocols=["obswebsocket.json"])
        self.port = self._srv.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self._srv is not None:
            self._srv.close()
            await self._srv.wait_closed()
            self._srv = None


class FakeProc:
    """Замена subprocess.Popen: помнит аргументы, умеет «завершаться»."""

    instances: list["FakeProc"] = []

    def __init__(self, args, **kwargs):
        self.args = list(args)
        self.kwargs = kwargs
        self.pid = 1000 + len(FakeProc.instances)
        self._returncode = None
        self.terminated = False
        FakeProc.instances.append(self)

    def poll(self):
        return self._returncode

    def finish(self, code: int = 0):
        self._returncode = code

    def terminate(self):
        self.terminated = True
        self._returncode = -15


class FakeObs:
    """Стаб клиента OBS для тестов Panel: записывает вызовы, отдаёт заданные ответы."""

    def __init__(self, responses: dict | None = None, connected: bool = True,
                 failures: set[str] | None = None):
        self.responses = responses or {}
        self.connected = connected
        self.failures = failures or set()
        self.calls: list[tuple[str, dict]] = []

    async def req(self, request_type: str, **data):
        self.calls.append((request_type, data))
        if request_type in self.failures:
            raise RuntimeError(f"OBS отказал: {request_type}")
        if not self.connected:
            raise RuntimeError("OBS офлайн")
        return self.responses.get(request_type, {})

    def called(self, request_type: str) -> list[dict]:
        return [d for r, d in self.calls if r == request_type]
