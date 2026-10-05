"""Синхронный клиент obs-websocket v5 для модуля RedZone.

Намеренно синхронный: сборщик раскладки, патч коллекции и снимок сетки — короткие
разовые операции, которым не нужен цикл событий. Ядро пульта (`server/obs.py`)
остаётся асинхронным и работает само по себе; здесь свой маленький транспорт,
чтобы модуль запускался и без сервиса пульта.

Импорт `websockets` ленивый: без него отвечают `--help`, разбор конфига и часть
подкоманд, которые к OBS не ходят.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
SUBPROTOCOL = "obswebsocket.json"


class ObsError(RuntimeError):
    """OBS недоступен или отклонил запрос."""


def compute_auth(password: str, salt: str, challenge: str) -> str:
    """Формула obs-websocket v5: base64(sha256(base64(sha256(pw+salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def load_ws_config(path: Path | str) -> tuple[int, str]:
    """Читает config.json obs-websocket -> (порт, пароль).

    Отсутствующий или битый файл не роняет модуль: остаются умолчания, а пароль
    пустой (OBS с выключенной авторизацией).
    """
    try:
        cfg = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        return int(cfg.get("server_port", DEFAULT_PORT)), str(cfg.get("server_password") or "")
    except (OSError, ValueError, TypeError):
        return DEFAULT_PORT, ""


class Obs:
    """Клиент OBS: подключение, запросы, удобные обёртки над чтением состояния."""

    def __init__(self, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 password: str = "", request_timeout: float = 10.0, connect=None):
        self.host = host
        self.port = port
        self.password = password
        self.request_timeout = request_timeout
        self._connect_factory = connect
        self._ws = None

    @classmethod
    def from_config(cls, cfg) -> "Obs":
        """Подключается по конфигу модуля: адрес/порт можно переопределить, пароль — из OBS."""
        port, password = load_ws_config(cfg.obs_ws_config)
        return cls(host=cfg.obs_host, port=cfg.obs_port or port, password=password)

    # ── соединение ──
    def connect(self) -> "Obs":
        if self._connect_factory is None:
            try:
                from websockets.sync.client import connect as _connect
            except ImportError as exc:  # pragma: no cover - зависит от окружения
                raise ObsError(
                    "нет модуля websockets — запусти модуль питоном из окружения пульта "
                    "(.venv/bin/python3) или установи websockets") from exc
            self._connect_factory = _connect
        try:
            self._ws = self._connect_factory(
                f"ws://{self.host}:{self.port}", subprotocols=[SUBPROTOCOL], max_size=None)
        except Exception as exc:  # noqa: BLE001 - сообщение важнее типа
            raise ObsError(f"OBS недоступен по ws://{self.host}:{self.port}: {exc}") from exc
        if self._ws is None:
            raise ObsError("соединение с OBS не открылось")
        hello = json.loads(self._ws.recv()).get("d") or {}
        ident: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("authentication")
        if auth:
            ident["authentication"] = compute_auth(
                self.password, auth["salt"], auth["challenge"])
        self._ws.send(json.dumps({"op": 1, "d": ident}))
        reply = json.loads(self._ws.recv())
        if reply.get("op") != 2:
            raise ObsError(f"рукопожатие obs-websocket не удалось: op={reply.get('op')}")
        return self

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    def __enter__(self) -> "Obs":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── запросы ──
    def req(self, request_type: str, *, silent_codes: tuple = (), **data) -> tuple[bool, dict]:
        """Запрос к OBS. Возвращает (успех, responseData).

        Ошибка с кодом из `silent_codes` не поднимается исключением — вызывающий
        сам решает, что это значит.
        """
        rid = str(uuid.uuid4())
        if self._ws is None:
            raise ObsError("клиент OBS не подключён")
        self._ws.send(json.dumps({"op": 6, "d": {
            "requestType": request_type, "requestId": rid, "requestData": data}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                status = msg["d"].get("requestStatus") or {}
                ok = bool(status.get("result"))
                if not ok and status.get("code") not in silent_codes:
                    raise ObsError(f"{request_type}: {status.get('comment')} "
                                   f"(код {status.get('code')})")
                return ok, msg["d"].get("responseData") or {}

    def get(self, request_type: str, **data) -> dict:
        """Запрос, результат которого обязателен: возвращает только responseData."""
        return self.req(request_type, **data)[1]

    def try_get(self, request_type: str, **data) -> dict:
        """Как `get`, но отказ OBS не поднимает исключение — возвращает {}."""
        try:
            return self.get(request_type, **data)
        except ObsError:
            return {}

    # ── обёртки над состоянием ──
    def inputs(self) -> set[str]:
        return {i["inputName"] for i in self.get("GetInputList").get("inputs", [])}

    def scenes(self) -> list[str]:
        return [s["sceneName"] for s in self.get("GetSceneList").get("scenes", [])]

    def items(self, scene: str) -> list[dict]:
        return self.get("GetSceneItemList", sceneName=scene).get("sceneItems", [])

    def item(self, scene: str, source: str) -> dict | None:
        return next((i for i in self.items(scene) if i["sourceName"] == source), None)

    def input_settings(self, name: str) -> dict:
        return self.get("GetInputSettings", inputName=name).get("inputSettings", {})

    def streaming(self) -> bool:
        return bool(self.try_get("GetStreamStatus").get("outputActive"))

    def recording(self) -> bool:
        return bool(self.try_get("GetRecordStatus").get("outputActive"))

    def busy(self) -> bool:
        """Идёт эфир или запись: правки сцен и коллекции в этот момент запрещены."""
        return self.streaming() or self.recording()

    def obs_version(self) -> str:
        return str(self.try_get("GetVersion").get("obsVersion", "?"))
