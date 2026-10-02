"""Конфиг пульта и токен доступа.

Токен — единственный секрет сервиса: он лежит в `config.json` (в .gitignore),
выдаётся клиенту один раз и переиспользуется при каждом запуске. Файл создаётся
при первом запуске с правами 0600, отсутствие или порча JSON не роняют сервис —
конфиг просто пересоздаётся.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import secrets

# Путь по умолчанию — корень проекта (каталог выше пакета server/).
DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config.json"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8090

_TOKEN_BYTES = 32
_FILE_MODE = 0o600


def _write(path: pathlib.Path, cfg: dict) -> None:
    """Записать конфиг и гарантировать права 0600 (независимо от umask)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg, ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(Exception):
            os.close(fd)
        raise
    with contextlib.suppress(OSError):
        os.chmod(path, _FILE_MODE)


def _read(path: pathlib.Path) -> dict | None:
    """Прочитать конфиг; битый JSON, не-объект или пустой токен — это «нет файла»."""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    token = cfg.get("token")
    if not isinstance(token, str) or not token:
        return None
    return cfg


def load_or_create(path: pathlib.Path = DEFAULT_CONFIG_PATH) -> dict:
    """Вернуть конфиг из файла, при отсутствии/порче — создать новый с токеном.

    Возвращаемый словарь всегда содержит `token`, `host`, `port`. Существующий
    токен переиспользуется: генерация происходит только при создании/пересоздании.
    """
    path = pathlib.Path(path)
    cfg = _read(path)
    if cfg is None:
        cfg = {"token": secrets.token_urlsafe(_TOKEN_BYTES),
               "host": DEFAULT_HOST, "port": DEFAULT_PORT}
        _write(path, cfg)
        return dict(cfg)

    # Файл валиден, но мог быть создан старой версией — добиваем недостающие ключи.
    cfg.setdefault("host", DEFAULT_HOST)
    cfg.setdefault("port", DEFAULT_PORT)
    return {"token": cfg["token"], "host": cfg["host"], "port": cfg["port"]}
