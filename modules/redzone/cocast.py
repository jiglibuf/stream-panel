"""Совместный каст через хаб MediaMTX.

Соведущим нужна та же картинка, что в эфире, с задержкой меньше секунды — на самом
Twitch это невозможно (там буфер в несколько секунд). Схема: OBS отдаёт микс в хаб
(путь `program`), отдельный релей копирует поток в Twitch без перекодирования, а
друзья читают `program` прямо из хаба.

`on`/`off` прерывают эфир на несколько секунд (OBS перезапускает публикацию),
поэтому при активном эфире команда отказывается работать без `--yes`.

Ключ Twitch здесь не печатается и не хранится в коде: он уходит в файл с правами
600 (`cocast.target`) и оттуда возвращается в OBS; релей берёт адрес из того же файла.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .obsclient import Obs

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"


def _curl_json(url: str, timeout: float = 8.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def hub_paths(cfg) -> dict[str, dict]:
    """Что сейчас в хабе: имя пути -> запись. Пусто, если хаб не отвечает."""
    data = _curl_json(f"{cfg.hub_api.rstrip('/')}/v3/paths/list")
    if not data:
        return {}
    return {item.get("name"): item for item in data.get("items", [])}


def hub_line(cfg, path: str) -> str:
    item = hub_paths(cfg).get(path)
    if not item:
        return "пути нет (никто не публикует)"
    tracks = ",".join(item.get("tracks") or []) or "нет"
    return (f"ready={item.get('ready')} треки={tracks} "
            f"читателей={len(item.get('readers') or [])} "
            f"источник={item.get('source') or 'нет'}")


def _systemctl(cfg, action: str, unit: str | None = None) -> int:
    unit = unit or cfg.cocast_relay_unit
    return subprocess.run(["systemctl", "--user", action, unit],
                          capture_output=True, text=True).returncode


def _unit_active(cfg, unit: str | None = None) -> bool:
    unit = unit or cfg.cocast_relay_unit
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                          capture_output=True).returncode == 0


def _saved_target(cfg) -> bool:
    return Path(cfg.cocast_target).exists()


# ── OBS: куда отдаётся эфир ──────────────────────────────────────────────────

def _service(obs: Obs) -> dict:
    svc = obs.get("GetStreamServiceSettings")
    settings = svc.get("streamServiceSettings") or {}
    return {"type": svc.get("streamServiceType", ""), "settings": settings,
            "server": (settings.get("server") or "").rstrip("/")}


def snapshot(obs: Obs, cfg) -> dict:
    stream = obs.get("GetStreamStatus")
    svc = _service(obs)
    hub_server = f"{cfg.hub_rtmp.rstrip('/')}/{cfg.hub_program}"
    return {"streaming": bool(stream.get("outputActive")),
            "server": svc["server"], "to_hub": svc["server"] == hub_server,
            "has_key": bool(svc["settings"].get("key")),
            "saved_target": _saved_target(cfg)}


def obs_status(cfg, as_json: bool = False) -> int:
    with Obs.from_config(cfg) as obs:
        s = snapshot(obs, cfg)
    if as_json:
        print(json.dumps(s, ensure_ascii=False))
        return 0
    print(f"  эфир: {'идёт' if s['streaming'] else 'не идёт'}")
    print(f"  OBS отдаёт в: {s['server'] or '(не задано)'}")
    print(f"  режим: {'через хаб (совместный каст)' if s['to_hub'] else 'напрямую в Twitch'}")
    print(f"  адрес Twitch сохранён: {'да' if s['saved_target'] else 'нет'}")
    return 0


def _switch(obs: Obs, cfg, target: str) -> None:
    """Переключение службы OBS c остановкой/запуском эфира (рвёт эфир на секунды)."""
    if target == "hub":
        svc = _service(obs)
        if svc["server"] != f"{cfg.hub_rtmp.rstrip('/')}/{cfg.hub_program}":
            if not svc["settings"].get("key"):
                raise RuntimeError("в настройках OBS нет ключа Twitch — сохранять нечего")
            path = Path(cfg.cocast_target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"type": svc["type"], "settings": svc["settings"],
                 "server": svc["server"], "key": svc["settings"]["key"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(path, 0o600)
            print(f"  адрес и ключ Twitch сохранены в {path} (права 600, не печатаю)")
        if obs.streaming():
            obs.get("StopStream")
            time.sleep(2.5)
        obs.get("SetStreamServiceSettings", streamServiceType="rtmp_custom",
                streamServiceSettings={"server": f"{cfg.hub_rtmp.rstrip('/')}/{cfg.hub_program}",
                                       "key": ""})
        obs.get("StartStream")
    else:
        path = Path(cfg.cocast_target)
        if not path.exists():
            raise RuntimeError(f"нет {path} — восстанавливать нечего")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if obs.streaming():
            obs.get("StopStream")
            time.sleep(2.5)
        obs.get("SetStreamServiceSettings", streamServiceType=saved.get("type", "rtmp_custom"),
                streamServiceSettings=saved.get("settings", {}))
        obs.get("StartStream")
    time.sleep(2.0)


def on(cfg, *, yes: bool = False) -> int:
    """Перевести OBS на хаб и поднять релей в Twitch."""
    if not hub_paths(cfg):
        print(f"хаб не отвечает по {cfg.hub_api} — проверь контейнер MediaMTX", flush=True)
        return 1
    with Obs.from_config(cfg) as obs:
        before = snapshot(obs, cfg)
        occupied = (hub_paths(cfg).get(cfg.hub_program) or {}).get("source")
        if not before["to_hub"] and occupied:
            print(f"путь {cfg.hub_program} занят посторонним публикатором ({occupied}). "
                  f"Скорее всего остался тестовый поток — останови его и повтори.", flush=True)
            return 1
        if before["streaming"] and not before["to_hub"] and not yes:
            print("СЕЙЧАС ИДЁТ ЭФИР. Перевод на хаб прервёт его на несколько секунд.\n"
                  "  Зрители увидят короткий перерыв (~5–10 с), сам эфир и запись продолжатся.\n\n"
                  f"Если это то, что нужно — повтори с флагом:  cocast on --yes", flush=True)
            return 2
        print("1/3 OBS → хаб")
        _switch(obs, cfg, "hub")
    print("2/3 жду поток в хабе")
    for _ in range(20):
        if (hub_paths(cfg).get(cfg.hub_program) or {}).get("ready"):
            break
        time.sleep(1)
    print(f"     хаб: {hub_line(cfg, cfg.hub_program)}")
    print("3/3 поднимаю релей в Twitch")
    _systemctl(cfg, "restart")
    time.sleep(5)
    readers = len((hub_paths(cfg).get(cfg.hub_program) or {}).get("readers") or [])
    if readers < 1:
        print(f"ВНИМАНИЕ: релей не подключился к хабу. Смотри: "
              f"journalctl --user -u {cfg.cocast_relay_unit} -n 20", flush=True)
        return 1
    print("готово: эфир идёт через хаб, друзья смотрят путь " + cfg.hub_program)
    return 0


def off(cfg, *, yes: bool = False) -> int:
    """Вернуть прямой Twitch и погасить релей."""
    with Obs.from_config(cfg) as obs:
        before = snapshot(obs, cfg)
        if before["streaming"] and not yes:
            print("СЕЙЧАС ИДЁТ ЭФИР. Возврат на прямой Twitch тоже прервёт его на несколько "
                  f"секунд.\nПовтори с флагом:  cocast off --yes", flush=True)
            return 2
        print("1/2 гашу релей")
        _systemctl(cfg, "stop")
        print("2/2 возвращаю OBS на прямой Twitch")
        _switch(obs, cfg, "twitch")
    print("готово: эфир снова идёт напрямую в Twitch")
    return 0


def status(cfg) -> int:
    """Полное состояние совместного каста."""
    print("── хаб ──")
    print(f"  общий эфир: {hub_line(cfg, cfg.hub_program)}")
    for name, item in sorted(hub_paths(cfg).items()):
        if name == cfg.hub_program:
            continue
        tracks = ",".join(item.get("tracks") or []) or "нет"
        print(f"  {name}: ready={item.get('ready')} треки={tracks} "
              f"читателей={len(item.get('readers') or [])}")
    print("── релей ──")
    print(f"  служба: {'работает' if _unit_active(cfg) else 'не запущена'}")
    print(f"  адрес Twitch: {'сохранён' if _saved_target(cfg) else 'нет'}")
    print("── OBS ──")
    try:
        obs_status(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"  OBS недоступен: {exc}")
    return 0


def viewer_key(cfg) -> int:
    """Добавляет в конфиг хаба ключ зрителя (только чтение общего эфира).

    Перезагрузка — рестартом контейнера: MediaMTX не перечитывает конфиг на `up -d`,
    а `kill -s HUP` его останавливает. Уже идущие трансляции друзей при рестарте
    прерываются на несколько секунд.
    """
    path = Path(cfg.hub_config)
    if not path.exists():
        print(f"нет {path} — укажи hub.config в конфиге")
        return 1
    text = path.read_text(encoding="utf-8")
    user = "rzview"
    if f"user: {user}" in text:
        print(f"ключ {user} уже есть в конфиге — ничего не делаю")
        return 0
    before = set(hub_paths(cfg))
    password = secrets.token_urlsafe(18)
    block = (f"  # ключ для друзей: только чтение — смотреть общий эфир в низкой задержке\n"
             f"  - user: {user}\n"
             f"    pass: {password}\n"
             f"    ips: []\n"
             f"    permissions:\n"
             f"      - action: read\n"
             f"        path: {cfg.hub_program}\n")
    anchor = "# Пути создаются на лету"
    if anchor not in text:
        print("не нашёл точку вставки в конфиге — правлю вручную не буду")
        return 1
    backup = path.with_suffix(path.suffix + ".bak-viewer")
    shutil.copy2(path, backup)
    path.write_text(text.replace(anchor, block + "\n" + anchor, 1), encoding="utf-8")
    print(f"конфиг обновлён, бэкап: {backup}")
    rc = subprocess.run(["docker", "compose", "restart"], cwd=str(cfg.hub_compose_dir),
                        capture_output=True, text=True).returncode
    if rc:
        shutil.copy2(backup, path)
        print("рестарт хаба не прошёл — конфиг возвращён из бэкапа")
        return 1
    time.sleep(3)
    after = set(hub_paths(cfg))
    lost = sorted(before - after)
    print(f"ключ записан: {user} (пароль — в {cfg.hub_credentials})" if lost == []
          else f"!! после рестарта пропали пути: {lost}")
    cred = Path(cfg.hub_credentials)
    cred.parent.mkdir(parents=True, exist_ok=True)
    with cred.open("a", encoding="utf-8") as fh:
        fh.write(f"\n# ключ зрителя (только чтение, для друзей и проверок)\n{user}: {password}\n")
    print(f"ключ добавлен в {cred}")
    return 0


def _run_script(cfg, name: str, args: list[str]) -> int:
    """Запускает вспомогательный скрипт модуля с окружением из конфига."""
    script = SCRIPT_DIR / name
    if not script.exists():
        print(f"нет вспомогательного скрипта {script}")
        return 1
    return subprocess.run(["bash", str(script), *args], env=cfg.script_env()).returncode


def testfeed(cfg, minutes: int = 15) -> int:
    """Раздаёт в хаб тестовую картинку с часами: проверить VLC и сеть у друзей."""
    if _saved_target(cfg):
        print("включён боевой режим совместного каста — сначала: cocast off")
        return 1
    return _run_script(cfg, "cocast_testfeed.sh", [str(minutes)])


def selftest(cfg) -> int:
    """Самопроверка схемы целиком, не выходя в эфир (тестовая картинка + чтение)."""
    return _run_script(cfg, "cocast_selftest.sh", [])
