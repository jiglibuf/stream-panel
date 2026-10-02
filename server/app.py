"""HTTP-слой пульта: API, WebSocket-рассылка и раздача фронта.

Приложение собирается целиком через `create_app` — obs, panel, commands и токен
внедряются, поэтому слой тестируется на фейках без живого OBS и без сети наружу.

Доступ (F6) — два рубежа. Сначала allowlist адреса клиента (чистая функция
`is_allowed`): запрос с чужого адреса получает 403 независимо от токена. Затем
токен: `/api/*` — заголовок `Authorization: Bearer`, `/ws` — параметр `?token=`
(браузерный WebSocket не умеет заголовки). Сравнение — только
`secrets.compare_digest`.

Токен не должен попадать в логи: стандартный access-лог aiohttp пишет строку
запроса целиком (включая `?token=`), поэтому Application создаётся с
`handler_args={"access_log": None}` — access-лог выключен.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import pathlib
import secrets

from aiohttp import WSMsgType, web

from .commands import CommandError

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND_DIR = ROOT / "frontend" / "dist"

# Приватные сети и tailnet (CGNAT-диапазон 100.64.0.0/10) — второй рубеж доступа.
_ALLOWED_NETS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
))

_STUB_HTML = (
    "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>Пульт стрима</title></head><body>"
    "<h1>Пульт стрима</h1>"
    "<p>фронт ещё не собран</p>"
    "</body></html>"
)

_log = logging.getLogger("panel.http")


# ── allowlist ──
def _address(ip: str):
    """Строка -> ip-адрес; IPv4-mapped IPv6 приводится к IPv4. None при мусоре."""
    if not isinstance(ip, str) or not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0].strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_allowed(ip: str) -> bool:
    """Пропускать ли клиента с таким адресом (loopback, приватные, tailnet)."""
    addr = _address(ip)
    if addr is None:
        return False
    if addr.is_loopback:
        return True
    return any(addr in net for net in _ALLOWED_NETS if net.version == addr.version)


# ── токен ──
def _token_equal(supplied, expected: str) -> bool:
    """Сравнение токена только через secrets.compare_digest (тайминг-безопасно)."""
    if not isinstance(supplied, str) or not supplied or not expected:
        return False
    try:
        return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def _bearer(request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):]
    return None


def _supplied_token(request, path: str) -> str | None:
    """Токен запроса: query для /ws, заголовок для всего остального."""
    if path == "/ws":
        token = request.query.get("token")
        return token if token else _bearer(request)
    return _bearer(request)


def make_guard(token: str):
    """Middleware доступа: allowlist (403), затем токен (401)."""

    @web.middleware
    async def guard(request, handler):
        if not is_allowed(request.remote):
            return _error(403, "адрес клиента не в списке разрешённых")
        path = request.path
        if path == "/ws" or path.startswith("/api/"):
            if not _token_equal(_supplied_token(request, path), token):
                if path == "/ws":
                    return web.Response(status=401, text="требуется токен")
                return _error(401, "требуется токен")
        return await handler(request)

    return guard


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


# ── WebSocket-рассылка ──
class WsHub:
    """Набор WS-клиентов и цикл рассылки свежего снапшота.

    Первое сообщение клиенту шлёт обработчик (сразу после рукопожатия), дальше
    снапшот рассылается каждые `interval` секунд (VU-метры, ~5 раз/с) и немедленно
    — после успешной команды. Отключение клиента посреди цикла не роняет рассылку:
    неудачная отправка просто выкидывает соединение из набора.
    """

    def __init__(self, panel, interval: float):
        self.panel = panel
        self.interval = float(interval)
        self.clients: set = set()
        self._locks: dict = {}
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._closed = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for ws in list(self.clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self.clients.clear()
        self._locks.clear()

    def add(self, ws) -> None:
        self.clients.add(ws)
        self._locks[ws] = asyncio.Lock()

    def discard(self, ws) -> None:
        self.clients.discard(ws)
        self._locks.pop(ws, None)

    def snapshot(self) -> dict:
        return self.panel.state

    async def send(self, ws, message: dict) -> None:
        """Отправить одно сообщение под локом соединения (без гонок с рассылкой)."""
        lock = self._locks.get(ws)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ws] = lock
        async with lock:
            await ws.send_str(json.dumps(message, ensure_ascii=False))

    async def broadcast(self, state: dict | None = None) -> None:
        payload = state if state is not None else self.snapshot()
        message = {"type": "state", "state": payload}
        for ws in list(self.clients):
            try:
                await self.send(ws, message)
            except Exception:
                self.discard(ws)

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(self.interval)
                await self.broadcast()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue


# Ключи приложения (web.AppKey вместо строковых ключей — рекомендация aiohttp).
HUB_KEY = web.AppKey("hub", WsHub)
PANEL_KEY = web.AppKey("panel", object)
COMMANDS_KEY = web.AppKey("commands", object)
TOKEN_KEY = web.AppKey("token", str)
CONFIG_PATH_KEY = web.AppKey("config_path", object)


# ── статика фронта ──
def _index_file(frontend_dir: pathlib.Path):
    index = frontend_dir / "index.html"
    return index if index.is_file() else None


def _safe_file(frontend_dir: pathlib.Path, tail: str):
    """Файл внутри frontend_dir по относительному пути tail, иначе None (без ..)."""
    if not tail:
        return None
    candidate = (frontend_dir / tail).resolve()
    try:
        candidate.relative_to(frontend_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def create_app(*, obs, panel, commands, token: str, config_path=None,
               frontend_dir=None, push_interval: float = 0.2) -> web.Application:
    """Собрать HTTP-приложение пульта. Все зависимости внедряются."""
    if frontend_dir is None:
        frontend_dir = DEFAULT_FRONTEND_DIR
    frontend_dir = pathlib.Path(frontend_dir).resolve()

    hub = WsHub(panel, push_interval)
    app = web.Application(
        middlewares=[make_guard(token)],
        logger=logging.getLogger("panel.app"),
        # Дефолтный access-лог пишет строку запроса целиком (там ?token=) —
        # выключаем его на уровне Server, не трогая логирование приложения.
        handler_args={"access_log": None},
    )
    app[HUB_KEY] = hub
    app[PANEL_KEY] = panel
    app[COMMANDS_KEY] = commands
    app[TOKEN_KEY] = token
    app[CONFIG_PATH_KEY] = config_path

    # ── обработчики ──
    async def state_handler(request):
        return web.json_response(panel.state)

    async def cmd_handler(request):
        try:
            raw = await request.text()
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            return _error(400, "тело запроса не является JSON")
        if not isinstance(body, dict):
            return _error(400, "тело запроса должно быть объектом JSON")
        name = body.get("name")
        if not isinstance(name, str):
            return _error(400, "поле name должно быть строкой")
        args = body.get("args")
        if args is not None and not isinstance(args, dict):
            return _error(400, "поле args должно быть объектом или null")
        try:
            envelope = await commands.execute(name, args)
        except CommandError as exc:
            return _error(getattr(exc, "code", 400), str(exc))
        # Успешная команда — немедленная рассылка свежего снапшота всем клиентам.
        await hub.broadcast(envelope.get("state"))
        return web.json_response(envelope)

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hub.add(ws)
        try:
            await hub.send(ws, {"type": "state", "state": panel.state})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "ping":
                    await hub.send(ws, {"type": "pong", "t": data.get("t")})
        finally:
            hub.discard(ws)
        return ws

    async def index_handler(request):
        index = _index_file(frontend_dir)
        if index is not None:
            return web.FileResponse(index)
        return web.Response(text=_STUB_HTML, content_type="text/html")

    async def static_handler(request):
        target = _safe_file(frontend_dir, request.match_info.get("tail", ""))
        if target is not None:
            return web.FileResponse(target)
        raise web.HTTPNotFound()

    async def on_startup(app):
        hub.start()

    async def on_cleanup(app):
        await hub.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/api/state", state_handler)
    app.router.add_post("/api/cmd", cmd_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/{tail:.*}", static_handler)
    return app
