"""Звук RedZone: разделение игры и голосового чата, режимы, проверки.

Модуль читает OBS через obs-websocket v5 (`split`, `fix-scenes`) и управляет
виртуальными выходами PipeWire через вспомогательные скрипты (`enable`, `disable`,
`watch`, проверки). Сами скрипты лежат в `scripts/` — это низкоуровневая часть,
которая работает с `pactl`/`systemctl`/`ffmpeg`; вручную их запускать не нужно.

Схема (подробнее — в README модуля):

    игра    → game_mix    → источник OBS «Звук системы»  (глушится фокусом на чужой карте)
    Discord → discord_mix → источник OBS «Дискорд»       (не глушится никогда)
    прочее  → rz_mix      → в эфир не идёт

`rz_mix` остаётся выходом по умолчанию и не читается ни одним источником OBS —
именно поэтому удаётся читать мониторы двух других выходов (OBS блокирует захват
только устройства, совпадающего с устройством мониторинга).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .obsclient import Obs

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"

# режим → (скрипт, как объяснить в README)
_SCRIPTS = {
    "enable": "rz_mix_on.sh",
    "disable": "rz_mix_off.sh",
    "watch": "rz_mix_watch.py",
    "check": "rz_mix_check.sh",
    "route": "check_audio_route.sh",
    "phones": "check_phones.sh",
    "rescue": "mic_rescue.py",
    "watch-mic": "watch_mic_mute.py",
    "dota-fix": "dota_audio_fix.py",
    "split": "split_obs_sources.py",
    "fix-scenes": "fix_rz_audio_scenes.py",
    "scene-report": "rz_scene_audio.py",
}


def _run(cfg, name: str, args: list[str]) -> int:
    script = SCRIPT_DIR / name
    if not script.exists():
        print(f"нет вспомогательного скрипта {script}")
        return 1
    interpreter = ["bash", str(script)] if script.suffix == ".sh" else ["python3", str(script)]
    return subprocess.run([*interpreter, *args], env=cfg.script_env()).returncode


def enable(cfg) -> int:
    """Включает режим «звук в эфир»: миксы, петли, сторож и развод источников OBS."""
    rc = _run(cfg, _SCRIPTS["enable"], [])
    if rc:
        return rc
    if cfg.obs_ws_config.exists():
        rc = _run(cfg, _SCRIPTS["split"], [])
    else:
        print("конфиг obs-websocket не найден — шаг развода источников пропущен")
    return rc


def disable(cfg) -> int:
    """Выключает режим и возвращает звук как было."""
    return _run(cfg, _SCRIPTS["disable"], [])


def watch(cfg) -> int:
    """Сторож звука в переднем плане (в бою его держит служба пользователя)."""
    return _run(cfg, _SCRIPTS["watch"], [])


def check(cfg, *, probe: bool = False) -> int:
    """Проверка звука: захват в OBS, маршрут потоков, уровни в миксе."""
    rc = 0
    if probe:
        rc = _run(cfg, _SCRIPTS["route"], ["--probe"]) or rc
    else:
        rc = _run(cfg, _SCRIPTS["route"], []) or rc
    rc = _run(cfg, _SCRIPTS["check"], []) or rc
    return rc


def route(cfg, *, probe: bool = False) -> int:
    """Отчёт «приложение → выход»: кто перехватывает звук."""
    return _run(cfg, _SCRIPTS["route"], ["--probe"] if probe else [])


def phones(cfg, device: str = "") -> int:
    """Проверка «звук реально доходит до наушников»."""
    return _run(cfg, _SCRIPTS["phones"], [device] if device else [])


def rescue(cfg) -> int:
    """«Пропал звук в эфире»: перещёлкнуть мьют и измерить уровень микса."""
    return _run(cfg, _SCRIPTS["rescue"], [])


def watch_mic(cfg, seconds: int = 40) -> int:
    """Понаблюдать за мьютом микрофона и событиями OBS."""
    return _run(cfg, _SCRIPTS["watch-mic"], [str(seconds)])


def dota_fix(cfg, *, watch: bool = False) -> int:
    """Снимает у Dota «глушить звук при потере фокуса» (правит только при закрытой игре)."""
    return _run(cfg, _SCRIPTS["dota-fix"], [] if watch else ["--once"])


def split(cfg) -> int:
    """В OBS: «Звук системы» → монитор game_mix, источник «Дискорд» → монитор discord_mix."""
    return _run(cfg, _SCRIPTS["split"], [])


def fix_scenes(cfg) -> int:
    """Возвращает «Звук системы» в сцены, где его пропустил сборщик."""
    return _run(cfg, _SCRIPTS["fix-scenes"], [])


def scene_report(cfg) -> int:
    """Аудит: какие звуковые источники лежат в каждой сцене и не заглушены ли."""
    return _run(cfg, _SCRIPTS["scene-report"], [])


def device(cfg, name: str | None = None) -> int:
    """Показывает или задаёт устройство вывода (файл в каталоге состояния)."""
    path = cfg.audio_device_file
    if name:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name.strip() + "\n", encoding="utf-8")
        print(f"устройство вывода: {name.strip()} ({path})")
        return 0
    if path.exists():
        print(path.read_text(encoding="utf-8").strip())
    else:
        print("(не задано — сторож ищет гарнитуру по образцу)")
    return 0


def levels(cfg) -> int:
    """Громкости и мьюты ключевых источников OBS (фейдер — частая причина «тишины»)."""
    names = (cfg.src("system_audio"), cfg.src("microphone"), cfg.card("hub", 2))
    with Obs.from_config(cfg) as obs:
        for name in names:
            if name not in obs.inputs():
                print(f"  {name}: источника нет")
                continue
            volume = obs.try_get("GetInputVolume", inputName=name).get("inputVolumeDb")
            muted = obs.try_get("GetInputMute", inputName=name).get("inputMuted")
            flag = ""
            if muted:
                flag = "  ← ЗАГЛУШЕН"
            elif volume is not None and volume < -40:
                flag = "  ← фейдер почти в тишине"
            print(f"  {name:16} {volume} дБ{flag}")
    return 0
