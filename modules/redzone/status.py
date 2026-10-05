"""Состояние модуля RedZone: что поднято, что нет, куда смотреть.

Собирает картину по кусочкам, каждый из которых может быть недоступен: OBS,
чат-релей, хаб MediaMTX, службы звука. Ничего не меняет — только читает.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import chat, cocast
from .config import Config
from .obsclient import Obs, ObsError


def _unit_state(name: str) -> str:
    """Состояние службы пользователя: active / inactive / не установлена."""
    try:
        state = subprocess.run(["systemctl", "--user", "is-active", name],
                               capture_output=True, text=True).stdout.strip()
        if state and state != "unknown":
            return state
        files = subprocess.run(["systemctl", "--user", "list-unit-files", name],
                               capture_output=True, text=True).stdout.strip()
        return "не установлена" if not files else "unknown"
    except OSError:
        return "нет systemctl"


def _sinks(cfg: Config) -> list[str]:
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                             text=True, env={**cfg.script_env(), "LANG": "C"}).stdout
    except OSError:
        return []
    return [line.split("\t")[1] for line in out.splitlines() if "\t" in line]


def report(cfg: Config) -> int:
    print(f"модуль RedZone")
    print(f"  конфиг:    {cfg.config_file}"
          f"{'' if cfg.config_file.exists() else '  (нет — работаю на умолчаниях)'}")
    print(f"  состояние: {cfg.state_dir}")

    print("── OBS ──")
    try:
        with Obs.from_config(cfg) as obs:
            print(f"  версия:  {obs.obs_version()} (ws://{cfg.obs_host}:{cfg.obs_port})")
            print(f"  эфир:    {'идёт' if obs.streaming() else 'не идёт'}"
                  f"   запись: {'идёт' if obs.recording() else 'не идёт'}")
            scene = obs.try_get("GetCurrentProgramScene").get("currentProgramSceneName", "?")
            print(f"  сцена:   {scene}")
            present = obs.scenes()
            wanted = list(cfg.mosaic_scenes) + list(cfg.solo_scenes)
            missing = [s for s in wanted if s not in present]
            print(f"  сцены раскладки: {len(wanted) - len(missing)}/{len(wanted)}"
                  + (f"   нет: {', '.join(missing)}" if missing else "   все на месте"))
    except (ObsError, OSError) as exc:
        print(f"  недоступен: {exc}")

    print("── чат ──")
    print(f"  служба: {_unit_state(cfg.chat_unit)}")
    health = chat.health(cfg)
    if health:
        ago = health.get("last_message_ago")
        print(f"  релей:  канал #{health.get('channel')} "
              f"{'подключён' if health.get('connected') else 'не подключён'}"
              f", читателей {health.get('subscribers')}"
              + (f", последнее сообщение {ago} с назад" if ago else ", сообщений ещё не было"))
    else:
        print(f"  релей:  http://{cfg.chat_host}:{cfg.chat_port}/ не отвечает")

    print("── хаб / каст ──")
    paths = cocast.hub_paths(cfg)
    if paths:
        print(f"  хаб:    отвечает, путей {len(paths)}; "
              f"{cfg.hub_program}: {cocast.hub_line(cfg, cfg.hub_program)}")
    else:
        print(f"  хаб:    {cfg.hub_api} не отвечает")
    print(f"  релей в Twitch: {_unit_state(cfg.cocast_relay_unit)}"
          f"   адрес сохранён: {'да' if Path(cfg.cocast_target).exists() else 'нет'}")
    if cfg.hub_users:
        print(f"  ключи хаба объявлены для: {', '.join(sorted(cfg.hub_users))}")

    print("── звук ──")
    print(f"  сторож: {cfg.audio_unit}: {_unit_state(cfg.audio_unit)}")
    sinks = _sinks(cfg)
    ours = [s for s in (cfg.audio_all_sink, cfg.audio_game_sink, cfg.audio_discord_sink)
            if s in sinks]
    print(f"  миксы:  {', '.join(ours) if ours else 'нет (режим «обычный»)'}")
    if cfg.audio_device_file.exists():
        print(f"  устройство вывода: {cfg.audio_device_file.read_text().strip()}")

    print("── сетка ──")
    out = cfg.bracket_output
    if out.exists():
        age = int(time.time() - out.stat().st_mtime)
        print(f"  картинка: {out} ({out.stat().st_size} байт, обновлена {age} с назад)")
    else:
        print(f"  картинки нет ({out}) — снимет `bracket`")
    print(f"  таймер: {_unit_state(cfg.bracket_timer)}")
    return 0
