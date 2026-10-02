"""Запуск сервиса «как в бою»: `python -m server`.

Один цикл событий: приложение поднимается через AppRunner/TCPSite, а клиент OBS и
цикл снапшота крутятся рядом в одной TaskGroup. Живого OBS при запуске может не
быть: клиент молча реконнектится, снапшот отдаёт `obs: false` и `error: «OBS
офлайн»`, сервис продолжает работать. Токен не печатается и не логируется — он
лежит в config.json.

Ctrl+C: asyncio отменяет главную задачу, TaskGroup отменяет и дожидается
дочерних задач, затем finally закрывает OBS и приложение — без «Task was
destroyed» и трейсбеков.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib

from aiohttp import web

from .app import create_app
from .commands import Commands
from .config import DEFAULT_CONFIG_PATH, load_or_create
from .journal import Journal
from .obs import Obs, load_connection
from .panel import Panel

_log = logging.getLogger("panel.main")

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _journal_path(cfg: dict) -> pathlib.Path:
    """Путь журнала: параметр journal_path из конфига, иначе journal.jsonl в корне проекта."""
    value = cfg.get("journal_path")
    if not value:
        return ROOT / "journal.jsonl"
    path = pathlib.Path(value)
    return path if path.is_absolute() else ROOT / path


def _obs_config_path() -> pathlib.Path:
    """Путь к config.json obs-websocket (можно переопределить через OBS_WS_CONFIG)."""
    override = os.environ.get("OBS_WS_CONFIG")
    if override:
        return pathlib.Path(override).expanduser()
    return (pathlib.Path.home() / ".config" / "obs-studio" / "plugin_config"
            / "obs-websocket" / "config.json")


async def _watch_obs(obs) -> None:
    """Логировать состояние связи с OBS без спама: только на смене."""
    _log.info("OBS: %s", "подключён" if obs.connected else "офлайн")
    seen = bool(obs.connected)
    while True:
        await asyncio.sleep(1.0)
        if bool(obs.connected) != seen:
            seen = bool(obs.connected)
            _log.info("OBS: %s", "подключён" if seen else "офлайн")


async def main() -> None:
    cfg = load_or_create(DEFAULT_CONFIG_PATH)
    host, port, password = load_connection(_obs_config_path())

    obs = Obs(host=host, port=port, password=password)
    panel = Panel(obs)
    commands = Commands(obs, panel)
    journal = Journal(_journal_path(cfg))
    app = create_app(obs=obs, panel=panel, commands=commands,
                     token=cfg["token"], config_path=DEFAULT_CONFIG_PATH,
                     journal=journal)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg["host"], cfg["port"])
    await site.start()
    _log.info("Пульт стрима: http://%s:%s (OBS %s:%s)", cfg["host"], cfg["port"], host, port)

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(obs.run(), name="obs")
            group.create_task(panel.run(), name="panel")
            group.create_task(_watch_obs(obs), name="obs-watch")
    finally:
        # Сюда попадаем и по Ctrl+C: сначала гасим клиент OBS, затем приложение
        # (on_cleanup приложения останавливает рассылку WebSocket).
        await obs.close()
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
