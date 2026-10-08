"""HTTP-слой пульта: API, WebSocket-рассылка и раздача фронта.

Приложение собирается целиком через `create_app` — obs, panel, commands и токен
внедряются, поэтому слой тестируется на фейках без живого OBS и без сети наружу.

Доступ — два рубежа. Сначала allowlist адреса клиента (чистая функция
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
import time

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
_journal_log = logging.getLogger("panel.journal")


async def _record(journal, **fields) -> None:
    """Записать событие в журнал; сбой журнала не должен ломать ответ клиенту."""
    if journal is None:
        return
    try:
        await journal.write(journal.entry(**fields))
    except Exception:
        _journal_log.exception("не удалось записать в журнал: %s", fields.get("action"))


def _duration_ms(started: float) -> int:
    """Длительность операции в целых миллисекундах."""
    return int((time.monotonic() - started) * 1000)


def _journal_result(envelope: dict):
    """Разложить конверт команды на (result, error, changed, extra) для журнала.

    result: `ok` — команда что-то изменила, `noop` — выполнена без изменений,
    `error` — OBS/выполнение вернули отказ (ok: false). `steps` — шаги «Паники».
    """
    changed = list(envelope.get("changed") or [])
    extra = {}
    if "steps" in envelope:
        extra["steps"] = envelope["steps"]
    if not envelope.get("ok"):
        return "error", envelope.get("error"), changed, extra
    return ("ok" if changed else "noop"), envelope.get("error"), changed, extra


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


def make_guard(token: str, journal=None):
    """Middleware доступа с записью отказов в журнал.

    Отказы доступа тоже попадают в журнал (`action: "auth"`): `denied` — нет
    верного токена (401), `foreign` — адрес вне allowlist (403). Пишем только
    адрес клиента и путь БЕЗ query-строки: в query у `/ws` едет токен, которого
    в журнале быть не должно.
    """

    @web.middleware
    async def guard(request, handler):
        if not is_allowed(request.remote):
            await _record(journal, action="auth", result="foreign",
                          source=request.remote, path=request.path)
            return _error(403, "адрес клиента не в списке разрешённых")
        path = request.path
        if path == "/ws" or path.startswith("/api/"):
            if not _token_equal(_supplied_token(request, path), token):
                await _record(journal, action="auth", result="denied",
                              source=request.remote, path=path)
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

    async def broadcast_audio(self, state: dict) -> None:
        """Разослать состояние звука отдельным типом сообщения.

        Снапшот звука несовместим по форме со снапшотом панели, поэтому его нельзя
        сунуть в обычный «state» — фронт получил бы чужую структуру. Шлём своим
        типом `audio`: клиенты, которые его не знают, просто игнорируют.
        """
        message = {"type": "audio", "audio": state}
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
AUDIO_KEY = web.AppKey("audio", object)
TOKEN_KEY = web.AppKey("token", str)
CONFIG_PATH_KEY = web.AppKey("config_path", object)
JOURNAL_KEY = web.AppKey("journal", object)


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
               frontend_dir=None, push_interval: float = 0.2, journal=None,
               audio=None, audio_auto=None) -> web.Application:
    """Собрать HTTP-приложение пульта. Все зависимости внедряются.

    `journal` необязателен: без него слой работает как раньше, без записей в
    журнал. Сбой самого журнала не влияет на ответы клиенту.

    `audio` — контроллер звука (системный провайдер + режимы). Необязателен: без
    него ручек `/api/audio*` в приложении нет вовсе, и остальной пульт работает
    как прежде.

    `audio_auto` — автоматика режима звука по состоянию эфира. Тоже необязательна:
    без неё нет ручки `/api/audio/auto`, а состояние звука отдаётся как раньше.
    """
    if frontend_dir is None:
        frontend_dir = DEFAULT_FRONTEND_DIR
    frontend_dir = pathlib.Path(frontend_dir).resolve()

    hub = WsHub(panel, push_interval)
    app = web.Application(
        middlewares=[make_guard(token, journal)],
        logger=logging.getLogger("panel.app"),
        # Дефолтный access-лог пишет строку запроса целиком (там ?token=) —
        # выключаем его на уровне Server, не трогая логирование приложения.
        handler_args={"access_log": None},
    )
    app[HUB_KEY] = hub
    app[PANEL_KEY] = panel
    app[COMMANDS_KEY] = commands
    app[AUDIO_KEY] = audio
    app[TOKEN_KEY] = token
    app[CONFIG_PATH_KEY] = config_path
    app[JOURNAL_KEY] = journal

    # Уровни звука приходят событием InputVolumeMeters, а не запросом: связываем
    # клиент OBS с панелью здесь, чтобы событие доезжало до снапшота и до /ws,
    # даже если сборщик (E2E) не сделал этого сам.
    if getattr(obs, "on_event", None) is None and hasattr(panel, "on_event"):
        obs.on_event = panel.on_event

    # ── обработчики ──
    async def state_handler(request):
        return web.json_response(panel.state)

    async def cmd_handler(request):
        started = time.monotonic()
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
            code = getattr(exc, "code", 400)
            await _record(journal, action=name,
                          result="refused" if code == 409 else "error",
                          source=request.remote, path=request.path,
                          duration_ms=_duration_ms(started), changed=[],
                          error=str(exc) or exc.__class__.__name__)
            return _error(code, str(exc))
        # После любой выполненной команды — одна запись: результат, клиент,
        # длительность и (у «Паники») шаги. Токен сюда не попадает.
        result, error, changed, extra = _journal_result(envelope)
        await _record(journal, action=name, result=result, source=request.remote,
                      path=request.path, duration_ms=_duration_ms(started),
                      changed=changed, error=error, **extra)
        # Успешная команда — немедленная рассылка свежего снапшота всем клиентам.
        await hub.broadcast(envelope.get("state"))
        return web.json_response(envelope)

    async def _audio_body(request):
        """Разобрать тело звуковых ручек; при мусоре вернуть готовый отказ 400."""
        try:
            raw = await request.text()
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None, _error(400, "тело запроса не является JSON")
        if not isinstance(body, dict):
            return None, _error(400, "тело запроса должно быть объектом JSON")
        return body, None

    async def audio_get_handler(request):
        snapshot = audio.snapshot()
        if audio_auto is not None:
            snapshot["auto"] = {"enabled": bool(audio_auto.enabled)}
        return web.json_response(snapshot)

    async def audio_mode_handler(request):
        started = time.monotonic()
        body, bad = await _audio_body(request)
        if bad is not None:
            return bad
        mode = body.get("mode")
        if not isinstance(mode, str):
            return _error(400, "поле mode должно быть строкой")
        confirm = body.get("confirm", False)
        if not isinstance(confirm, bool):
            return _error(400, "поле confirm должно быть булевым")
        try:
            envelope = await audio.set_mode(mode, confirm=confirm)
        except CommandError as exc:
            code = getattr(exc, "code", 400)
            await _record(journal, action="audio.mode",
                          result="refused" if code == 409 else "error",
                          source=request.remote, path=request.path,
                          duration_ms=_duration_ms(started), changed=[],
                          error=str(exc) or exc.__class__.__name__)
            return _error(code, str(exc))
        result, error, changed, extra = _journal_result(envelope)
        await _record(journal, action="audio.mode", result=result, source=request.remote,
                      path=request.path, duration_ms=_duration_ms(started),
                      changed=changed, error=error, **extra)
        if envelope.get("ok"):
            # Состояние звука — отдельным типом сообщения (см. broadcast_audio).
            await hub.broadcast_audio(envelope["state"])
        # Сбой шага — это 500 с конвертом: в error текст, в steps что успело сделаться.
        return web.json_response(envelope, status=200 if envelope.get("ok") else 500)

    async def audio_device_handler(request):
        started = time.monotonic()
        body, bad = await _audio_body(request)
        if bad is not None:
            return bad
        name = body.get("name")
        if not isinstance(name, str):
            return _error(400, "поле name должно быть строкой")
        try:
            envelope = await audio.set_device(name)
        except CommandError as exc:
            code = getattr(exc, "code", 400)
            await _record(journal, action="audio.device",
                          result="refused" if code == 409 else "error",
                          source=request.remote, path=request.path,
                          duration_ms=_duration_ms(started), changed=[],
                          error=str(exc) or exc.__class__.__name__)
            return _error(code, str(exc))
        result, error, changed, extra = _journal_result(envelope)
        await _record(journal, action="audio.device", result=result, source=request.remote,
                      path=request.path, duration_ms=_duration_ms(started),
                      changed=changed, error=error, **extra)
        if envelope.get("ok"):
            await hub.broadcast_audio(envelope["state"])
        return web.json_response(envelope, status=200 if envelope.get("ok") else 500)

    async def audio_auto_handler(request):
        """Тумблер автоматики режима звука: и в памяти, и в файле."""
        started = time.monotonic()
        body, bad = await _audio_body(request)
        if bad is not None:
            return bad
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _error(400, "поле enabled должно быть булевым")
        audio_auto.set_enabled(enabled)
        snapshot = audio.snapshot()
        snapshot["auto"] = {"enabled": bool(audio_auto.enabled)}
        envelope = {
            "ok": True, "name": "audio.auto", "changed": ["auto"],
            "steps": ["auto: автоматика " + ("включена" if enabled else "выключена")],
            "state": snapshot, "error": None,
        }
        await _record(journal, action="audio.auto", result="ok", source=request.remote,
                      path=request.path, duration_ms=_duration_ms(started),
                      changed=["auto"], error=None)
        await hub.broadcast_audio(envelope["state"])
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
    if audio is not None:
        app.router.add_get("/api/audio", audio_get_handler)
        app.router.add_post("/api/audio/mode", audio_mode_handler)
        app.router.add_post("/api/audio/device", audio_device_handler)
        if audio_auto is not None:
            app.router.add_post("/api/audio/auto", audio_auto_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index_handler)
    app.router.add_get("/{tail:.*}", static_handler)
    return app
