#!/usr/bin/env python3
"""Убирает у игры «глушить звук при потере фокуса».

Зачем: у Dota 2 стоит `snd_mute_losefocus = true`, поэтому она замолкает, как только её
окно теряет фокус — а при стриме это почти всегда. Правка возможна только при закрытой
игре: пока игра запущена, она перезапишет файл своими значениями при выходе.

Режимы:

    dota_audio_fix.py          сторожить постоянно: раз в 10 с смотрит, идёт ли игра,
                               и при закрытой приводит конвар к нужному значению (с бэкапом);
    dota_audio_fix.py --once   один проход и выход (для запуска из модуля).

Параметры — из окружения: REDZONE_DOTA_CFG_GLOB (образец пути к machine_convars.vcfg),
REDZONE_DOTA_CONVAR (имя конвара), REDZONE_DOTA_VALUE (нужное значение).
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

POLL = 10
KEY = os.environ.get("REDZONE_DOTA_CONVAR", "snd_mute_losefocus")
WANT = os.environ.get("REDZONE_DOTA_VALUE", "false")
PATTERN = os.environ.get("REDZONE_DOTA_CFG_GLOB",
                         "~/.local/share/Steam/userdata/*/570/local/cfg/machine_convars.vcfg")


def dota_running():
    r = subprocess.run(["pgrep", "-f", "dota2|dota 2 beta"], capture_output=True, text=True)
    return r.returncode == 0


def cfg_paths():
    return glob.glob(os.path.expanduser(PATTERN))


def fix_file(path):
    """Возвращает True, если что-то поправили."""
    text = open(path, encoding="utf-8", errors="surrogateescape").read()
    m = re.search(rf'("{re.escape(KEY)}"\s*)"([^"]*)"', text)
    if not m:
        print(f"  ключа {KEY} нет в {path}", flush=True)
        return False
    if m.group(2) == WANT:
        return False
    backup = path + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    new = text[:m.start(2)] + WANT + text[m.end(2):]
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(new)
    print(f"  {KEY}: {m.group(2)} → {WANT} в {path} (бэкап {backup})", flush=True)
    return True


def run_once():
    if dota_running():
        print("  игра запущена — файл не трогаю", flush=True)
        return 0
    changed = False
    for path in cfg_paths():
        changed = fix_file(path) or changed
    if not changed:
        print("  править нечего (ключ уже в нужном значении или файлов нет)", flush=True)
    return 0


def main():
    once = "--once" in sys.argv
    if once:
        print("проверка аудио-настроек игры (один проход)", flush=True)
        return run_once()
    print("сторож аудио-настроек игры запущен (правит только при закрытой игре)", flush=True)
    reported_wait = False
    while True:
        try:
            if dota_running():
                if not reported_wait:
                    print("  игра запущена — жду её закрытия, файл не трогаю", flush=True)
                    reported_wait = True
            else:
                reported_wait = False
                for path in cfg_paths():
                    fix_file(path)
        except Exception as exc:                      # сторож не должен падать
            print(f"  !! ошибка сторожа: {exc}", file=sys.stderr, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    raise SystemExit(main())
