"""Ядро клиента obs-websocket v5: подключение, запросы, события.

Здесь нет ни HTTP, ни звука — только транспорт к OBS. Всё, что нужно снаружи:
compute_auth() / load_connection() и класс Obs (run/req/close).

Главное отличие от прототипа: при обрыве связи незавершённые запросы падают
ConnectionError сразу и снимаются со счётчика waiters, а не висят до
request_timeout, как было раньше.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import pathlib

import websockets

SUBPROTOCOL = "obswebsocket.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455

# Какие события OBS шлём: биты 0..9 (1023, как раньше) плюс бит 16 — метры
# уровня звука InputVolumeMeters. Отдельного запроса для метроб в obs-websocket
# v5 нет, они приходят только этим событием.
EVENT_SUBSCRIPTIONS = 1023 | (1 << 16)


def compute_auth(password: str, salt: str, challenge: str) -> str:
    """Формула obs-websocket v5: base64(sha256(base64(sha256(pw+salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def load_connection(path: pathlib.Path) -> tuple[str, int, str]:
    """Читает config.json obs-websocket -> (host, server_port, server_password).

    Отсутствующий или битый файл не должен ломать запуск: возвращаем дефолт.
    """
    try:
        cfg = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        port = int(cfg.get("server_port", DEFAULT_PORT))
        password = cfg.get("server_password") or ""
        return (DEFAULT_HOST, port, password)
    except Exception:
        return (DEFAULT_HOST, DEFAULT_PORT, "")


class Obs:
    """Клиент obs-websocket v5 с авто-переподключением.

    on_event вызывается на события op5 и на смену состояния связи
    ({"type": "connected"} / {"type": "disconnected"}); он может быть как
    обычной функцией, так и async-функцией.
    """

    def __init__(self, on_event=None, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 password: str = "", request_timeout: float = 8.0, connect=None,
                 retry_delay: float = 3.0):
        self.host = host
        self.port = port
        self.password = password
        self.request_timeout = float(request_timeout)
        self.retry_delay = float(retry_delay)
        self.on_event = on_event
        self.connected = False

        self._connect = connect or websockets.connect
        self._ws = None
        self._id = 0
        self._waiters: dict[str, asyncio.Future] = {}
        self._event_tasks: set[asyncio.Task] = set()
        self._closed = False

    # ── инспекция (используется тестами) ──
    @property
    def waiters(self) -> int:
        """Число незавершённых ожиданий ответа на запрос."""
        return len(self._waiters)

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    # ── жизненный цикл ──
    async def run(self) -> None:
        """Вечный цикл: подключаемся, обслуживаем, при обрыве — реконнект."""
        while not self._closed:
            try:
                await self._serve()
            except asyncio.CancelledError:
                raise
            except Exception:
                # OBS недоступен или протокол отвалился — просто пробуем снова.
                pass
            if self._closed:
                break
            await asyncio.sleep(self.retry_delay)
        await self._drain_events()

    async def close(self) -> None:
        """Корректно закрыть соединение; после этого run() завершится сам."""
        self._closed = True
        ws, self._ws = self._ws, None
        self.connected = False
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_waiters(ConnectionError("клиент закрыт"))

    # ── запросы ──
    async def req(self, request_type: str, **data) -> dict:
        """Отправить запрос и вернуть responseData (пустой dict, если его нет)."""
        ws = self._ws
        if not self.connected or ws is None:
            raise RuntimeError("OBS офлайн")
        self._id += 1
        rid = f"r{self._id}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        payload = json.dumps({"op": 6, "d": {
            "requestType": request_type, "requestId": rid, "requestData": data}})
        try:
            await ws.send(payload)
        except Exception as exc:
            self._waiters.pop(rid, None)
            raise ConnectionError(f"соединение с OBS оборвалось при отправке: {exc}") from exc
        try:
            reply = await asyncio.wait_for(fut, timeout=self.request_timeout)
        finally:
            self._waiters.pop(rid, None)
        status = reply.get("requestStatus") or {}
        if status.get("result") is False:
            raise RuntimeError(status.get("comment") or f"{request_type}: отказ OBS")
        return reply.get("responseData") or {}

    # ── внутреннее ──
    async def _serve(self) -> None:
        ws = await self._handshake()
        self._ws = ws
        self.connected = True
        self._emit({"type": "connected"})
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(msg)
        finally:
            self._ws = None
            self.connected = False
            # Запросы, не дождавшиеся ответа, падают сразу — это и есть фикс утечки.
            self._fail_waiters(ConnectionError("соединение с OBS потеряно"))
            self._emit({"type": "disconnected"})

    async def _handshake(self):
        ws = await self._connect(self.url, subprotocols=[SUBPROTOCOL])
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.request_timeout))
            data = hello.get("d") or {}
            auth = None
            if data.get("authentication"):
                auth = compute_auth(self.password,
                                    data["authentication"]["salt"],
                                    data["authentication"]["challenge"])
            await ws.send(json.dumps({"op": 1, "d": {
                "rpcVersion": 1, "authentication": auth,
                "eventSubscriptions": EVENT_SUBSCRIPTIONS}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.request_timeout))
            if reply.get("op") != 2:
                raise ConnectionError(f"рукопожатие не удалось: op={reply.get('op')}")
            return ws
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise

    def _dispatch(self, msg: dict) -> None:
        op = msg.get("op")
        if op == 7:  # ответ на запрос
            data = msg.get("d") or {}
            fut = self._waiters.pop(str(data.get("requestId") or ""), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
        elif op == 5:  # событие
            self._emit(msg.get("d") or {})

    def _emit(self, event: dict) -> None:
        """Позвать on_event. Sync-колбэк вызываем сразу, async — задачей.

        Асинхронный колбэк нельзя ждать прямо в цикле чтения: он может сам
        дёрнуть req() и ответ придёт только когда цикл снова начнёт читать.
        """
        cb = self.on_event
        if cb is None:
            return
        try:
            result = cb(event)
        except Exception:
            return
        if asyncio.iscoroutine(result):
            task = asyncio.ensure_future(result)
            self._event_tasks.add(task)
            task.add_done_callback(self._on_event_task_done)

    def _on_event_task_done(self, task: asyncio.Task) -> None:
        self._event_tasks.discard(task)
        with contextlib.suppress(BaseException):
            task.exception()

    def _fail_waiters(self, exc: Exception) -> None:
        waiters = list(self._waiters.values())
        self._waiters.clear()
        for fut in waiters:
            if not fut.done():
                fut.set_exception(type(exc)(str(exc)))

    async def _drain_events(self) -> None:
        """Дать последним событиям (в т.ч. disconnected) дойти при выходе."""
        pending = [t for t in self._event_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=2)
        for task in list(self._event_tasks):
            if not task.done():
                task.cancel()
