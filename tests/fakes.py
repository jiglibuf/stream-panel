"""Тестовые двойники: фейковый obs-websocket v5 и фейковый процесс для pw-play.

Фейковый сервер проверен против настоящего клиента OBS: рукопожатие, auth, ответы, события.
"""
from __future__ import annotations

import asyncio
import base64
import copy
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
                spec = self._reply(req["requestType"])
                await ws.send(json.dumps({"op": 7, "d": {
                    "requestType": req["requestType"],
                    "requestId": req["requestId"], **spec}}))
        finally:
            self._clients.discard(ws)

    # Ответ настоящего obs-websocket v5 на неизвестный тип запроса.
    UNKNOWN_REQUEST = {"requestStatus": {"result": False, "code": 204,
                                         "comment": "Your request type is not valid"}}

    def _reply(self, request_type: str) -> dict:
        """Готовый ответ на запрос; неизвестный тип — отказ 204, как у OBS.

        Раньше на любой запрос отвечали успехом: фейк и код могли разделить одну
        выдумку (например несуществующий запрос GetInputVolumeMeters), и баг
        доживал до живого прогона.
        """
        if request_type not in self.responses:
            return copy.deepcopy(self.UNKNOWN_REQUEST)
        spec = dict(self.responses[request_type])
        spec.setdefault("requestStatus", {"result": True})
        return spec

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
    """Стаб клиента OBS для тестов Panel: записывает вызовы, отдаёт заданные ответы.

    responses: {requestType: dict | callable}. dict — готовый responseData;
        callable(**data) -> dict — чтобы ответ зависел от аргументов запроса
        (например, от inputName у GetInputMute/GetInputVolume).
    failures: элементы — либо request-type (строка), либо пара
        (request-type, inputName); на таких вызовах req() поднимает RuntimeError.
    """

    def __init__(self, responses: dict | None = None, connected: bool = True,
                 failures: set | None = None):
        self.responses = responses or {}
        self.connected = connected
        self.failures = failures or set()
        self.calls: list[tuple[str, dict]] = []

    def fails(self, request_type: str, data: dict) -> bool:
        if request_type in self.failures:
            return True
        name = data.get("inputName")
        return name is not None and (request_type, name) in self.failures

    async def req(self, request_type: str, **data):
        self.calls.append((request_type, data))
        if self.fails(request_type, data):
            raise RuntimeError(f"OBS отказал: {request_type}")
        if not self.connected:
            raise RuntimeError("OBS офлайн")
        response = self.responses.get(request_type, {})
        if callable(response):
            response = response(**data)
        return response

    def called(self, request_type: str) -> list[dict]:
        return [d for r, d in self.calls if r == request_type]


class FakeAudioOps:
    """Подставной системный провайдер звука: pactl/pw-link/systemctl/OBS — всё в памяти.

    Живых pactl, PipeWire, systemctl и OBS не касается: владелец может слушать
    музыку или быть в эфире, тесты не должны ничего менять в системе. Каждый вызов
    пишется в `calls` (метод, аргументы) — тесты проверяют и результат, и шаги.
    """

    # Методы-мутаторы: их отсутствие в calls — доказательство «к системе не ходили».
    MUTATORS = frozenset({
        "set_default_sink", "create_sink", "remove_sink",
        "create_loop", "remove_loops", "drop_loop_links",
        "move_stream", "enable_service", "disable_service",
        "write_device", "write_state", "remove_state",
        "set_input_device", "remove_input",
    })

    def __init__(self, *, sinks=None, loops=None, streams=None, enabled: bool = False,
                 active: bool = False, default_sink: str = "", device: str = "",
                 state=None, obs_online: bool = True, inputs=None, failures=None):
        self.sinks = [dict(s) for s in (sinks or [])]
        self.loops = [{"source": l["source"], "targets": list(l["targets"])}
                      for l in (loops or [])]
        self.streams = [dict(s) for s in (streams or [])]
        self.enabled = bool(enabled)
        self.active = bool(active)
        self.default = default_sink
        self.device = device
        self.state = dict(state) if state else None
        self._obs_online = bool(obs_online)
        self.inputs = dict(inputs or {})
        self.failures = set(failures or ())
        self.calls: list[tuple[str, tuple]] = []

    def _log(self, method: str, *args) -> None:
        self.calls.append((method, args))
        if method in self.failures:
            raise RuntimeError(f"{method}: отказ фейка")

    def mutations(self) -> set:
        return {m for m, _ in self.calls if m in self.MUTATORS}

    # ── выходы ──
    def list_sinks(self):
        self._log("list_sinks")
        return [dict(s) for s in self.sinks]

    def default_sink(self):
        self._log("default_sink")
        return self.default

    def set_default_sink(self, name):
        self._log("set_default_sink", name)
        self.default = name

    def create_sink(self, name, description=""):
        self._log("create_sink", name)
        if name not in [s["name"] for s in self.sinks]:
            self.sinks.append({"name": name, "state": "IDLE", "description": description})

    def remove_sink(self, name):
        self._log("remove_sink", name)
        self.sinks = [s for s in self.sinks if s["name"] != name]

    # ── петли ──
    def list_loops(self):
        self._log("list_loops")
        return [{"source": l["source"], "targets": list(l["targets"])} for l in self.loops]

    def create_loop(self, source, target):
        self._log("create_loop", source, target)
        for loop in self.loops:
            if loop["source"] == source:
                if target not in loop["targets"]:
                    loop["targets"].append(target)
                return
        self.loops.append({"source": source, "targets": [target]})

    def remove_loops(self, sources):
        self._log("remove_loops", tuple(sources))
        want = set(sources)
        self.loops = [l for l in self.loops if l["source"] not in want]

    def drop_loop_links(self, target):
        self._log("drop_loop_links", target)
        for loop in self.loops:
            if target in loop["targets"]:
                loop["targets"] = [t for t in loop["targets"] if t != target]

    # ── потоки ──
    def list_streams(self):
        self._log("list_streams")
        return [dict(s) for s in self.streams]

    def move_stream(self, sid, target):
        self._log("move_stream", sid, target)
        for stream in self.streams:
            if stream.get("id") == sid:
                stream["sink"] = target

    # ── служба ──
    def service(self):
        self._log("service")
        return {"enabled": self.enabled, "active": self.active}

    def enable_service(self):
        self._log("enable_service")
        self.enabled = self.active = True

    def disable_service(self):
        self._log("disable_service")
        self.enabled = self.active = False

    # ── файлы ──
    def read_device(self):
        self._log("read_device")
        return self.device

    def write_device(self, name):
        self._log("write_device", name)
        self.device = name

    def read_state(self):
        self._log("read_state")
        return dict(self.state) if self.state else {}

    def write_state(self, data):
        self._log("write_state")
        self.state = dict(data)

    def remove_state(self):
        self._log("remove_state")
        self.state = None

    # ── OBS ──
    def obs_online(self):
        return self._obs_online

    async def set_input_device(self, name, device):
        self._log("set_input_device", name, device)
        self.inputs[name] = device

    async def remove_input(self, name):
        self._log("remove_input", name)
        self.inputs.pop(name, None)
