"""Единая точка входа модуля RedZone.

    python3 -m modules.redzone статус/действие [ключи]

Подкоманды:
    up            поднять рабочие части модуля (чат, звук, таймер сетки)
    down          погасить их и вернуть звук как было
    build         собрать/обновить раскладку сцены
    column        пересобрать правую колонку (сетка + чат)
    point-cards   перенаправить источники карт (заглушка / хаб / своя ссылка)
    collection    хоткеи и порядок сцен в файле коллекции
    chat          локальный чат-релей (порт из конфига) или его состояние
    bracket       снять турнирную сетку и положить в OBS
    cocast        совместный каст через хаб (on/off/status/…)
    audio         звук: режим, сторож, проверки, развод источников
    audio-check   быстрая проверка звука (маршрут + захват + микс)
    status        что поднято и где смотреть
    config        какие пути и порты взяты и откуда

Общий ключ `--config ПУТЬ` (можно и через `REDZONE_CONFIG`) задаёт файл конфига.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from . import audio, bracket, chat, cocast, collection, column, layout, status
from .config import DEFAULT_CONFIG_PATH, Config, load
from .layout import LayoutError
from .obsclient import ObsError


def _systemctl(action: str, unit: str) -> int:
    """Действие над службой пользователя; отсутствие службы — не ошибка команды."""
    try:
        rc = subprocess.run(["systemctl", "--user", action, unit],
                            capture_output=True, text=True).returncode
    except OSError as exc:
        print(f"systemctl недоступен: {exc}")
        return 1
    if rc:
        print(f"служба {unit}: {action} не прошёл (может быть не установлена)")
    else:
        print(f"служба {unit}: {action} ok")
    return 0


# ── подкоманды ───────────────────────────────────────────────────────────────

def cmd_up(cfg: Config, args) -> int:
    """Поднять модуль: чат-релей, звук, таймер сетки; при `--build` ещё раскладку."""
    rc = 0
    print("чат-релей:")
    rc |= _systemctl("start", cfg.chat_unit)
    print("звук:")
    rc |= audio.enable(cfg)
    print("таймер сетки:")
    rc |= _systemctl("start", cfg.bracket_timer)
    if args.build:
        print("раскладка:")
        try:
            layout.build(cfg)
        except (ObsError, LayoutError, RuntimeError) as exc:
            print(f"  не собрать: {exc}")
            rc |= 1
    print("\nготово. Полное состояние — `status`.")
    return rc


def cmd_down(cfg: Config, args) -> int:
    """Погасить модуль: звук как было, чат и таймер остановлены."""
    rc = 0
    rc |= _systemctl("stop", cfg.bracket_timer)
    rc |= audio.disable(cfg)
    rc |= _systemctl("stop", cfg.chat_unit)
    return rc


def cmd_build(cfg: Config, args) -> int:
    if args.styles:
        layout.write_tiles(cfg, force=True)
    layout.build(cfg, hide=args.hide)
    return 0


def cmd_column(cfg: Config, args) -> int:
    return column.column(cfg)


def cmd_point_cards(cfg: Config, args) -> int:
    return layout.point_cards(cfg, cards=args.cards, mode=args.mode, url=args.url)


def cmd_collection(cfg: Config, args) -> int:
    return collection.patch(cfg, dry_run=args.dry_run)


def cmd_chat(cfg: Config, args) -> int:
    if args.test is not None:
        import json
        import urllib.parse
        import urllib.request
        query = urllib.parse.urlencode({"text": args.test})
        try:
            with urllib.request.urlopen(
                    f"http://{cfg.chat_host}:{cfg.chat_port}/test?{query}", timeout=5) as r:
                print("вброшено:", r.read().decode("utf-8"))
        except OSError as exc:
            print(f"релей не отвечает ({exc}) — запусти `chat` или службу "
                  f"{cfg.chat_unit}")
            return 1
        return 0
    if args.status:
        health = chat.health(cfg)
        if not health:
            print(f"релей на http://{cfg.chat_host}:{cfg.chat_port}/ не отвечает")
            return 1
        print(f"канал #{health.get('channel')}, "
              f"{'подключён' if health.get('connected') else 'не подключён'}, "
              f"читателей {health.get('subscribers')}, сообщений {health.get('kept')}")
        return 0
    return chat.serve(cfg)


def cmd_bracket(cfg: Config, args) -> int:
    return bracket.run(cfg, place_it=args.place)


def cmd_cocast(cfg: Config, args) -> int:
    action = args.action
    if action == "on":
        return cocast.on(cfg, yes=args.yes)
    if action == "off":
        return cocast.off(cfg, yes=args.yes)
    if action == "status":
        return cocast.status(cfg)
    if action == "testfeed":
        return cocast.testfeed(cfg, args.minutes)
    if action == "selftest":
        return cocast.selftest(cfg)
    if action == "viewer-key":
        return cocast.viewer_key(cfg)
    return 2


def cmd_audio(cfg: Config, args) -> int:
    action = args.action
    table = {
        "enable": lambda: audio.enable(cfg),
        "disable": lambda: audio.disable(cfg),
        "watch": lambda: audio.watch(cfg),
        "check": lambda: audio.check(cfg),
        "route": lambda: audio.route(cfg, probe=args.probe),
        "phones": lambda: audio.phones(cfg, device=args.device or ""),
        "rescue": lambda: audio.rescue(cfg),
        "watch-mic": lambda: audio.watch_mic(cfg, args.seconds),
        "dota-fix": lambda: audio.dota_fix(cfg, watch=args.watch),
        "split": lambda: audio.split(cfg),
        "fix-scenes": lambda: audio.fix_scenes(cfg),
        "scene-report": lambda: audio.scene_report(cfg),
        "device": lambda: audio.device(cfg, args.name),
        "levels": lambda: audio.levels(cfg),
    }
    return table[action]()


def cmd_audio_check(cfg: Config, args) -> int:
    return audio.check(cfg, probe=args.probe)


def cmd_status(cfg: Config, args) -> int:
    return status.report(cfg)


def cmd_config(cfg: Config, args) -> int:
    """Показывает разрешённые пути и порты: удобно проверять, что конфиг подхватился."""
    rows = [
        ("конфиг", cfg.config_file),
        ("состояние", cfg.state_dir),
        ("плитки", cfg.tiles_dir),
        ("картинка сетки", cfg.bracket_output),
        ("OBS websocket", f"{cfg.obs_host}:{cfg.obs_port}"),
        ("конфиг obs-websocket", cfg.obs_ws_config),
        ("каталог сцен", cfg.scenes_dir),
        ("коллекция", cfg.collection),
        ("чат", f"{cfg.chat_host}:{cfg.chat_port} канал={cfg.chat_channel or '—'}"),
        ("хаб API", cfg.hub_api),
        ("хаб RTSP", cfg.hub_rtsp),
        ("хаб config", cfg.hub_config),
        ("миксы", f"{cfg.audio_all_sink}/{cfg.audio_game_sink}/{cfg.audio_discord_sink}"),
        ("сторож звука", cfg.audio_unit),
        ("релей каста", cfg.cocast_relay_unit),
    ]
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        print(f"  {name:<{width}}  {value}")
    return 0


# ── разбор аргументов ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modules.redzone",
        description="Модуль RedZone: мультитрансляция карт Dota 2 в OBS, чат, "
                    "турнирная сетка, совместный каст, звук.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Запуск из корня репозитория: python3 -m modules.redzone <подкоманда> …")
    parser.add_argument("--config", metavar="ПУТЬ",
                        help=f"файл конфига (по умолчанию {DEFAULT_CONFIG_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("up", help="поднять рабочие части модуля")
    p.add_argument("--build", action="store_true", help="заодно собрать раскладку")
    p.set_defaults(func=cmd_up)

    sub.add_parser("down", help="погасить модуль и вернуть звук как было").set_defaults(func=cmd_down)

    p = sub.add_parser("build", help="собрать/обновить раскладку сцены")
    p.add_argument("--hide", action="store_true", help="собрать с погашенными картами")
    p.add_argument("--styles", action="store_true", help="перерисовать заглушки карт")
    p.set_defaults(func=cmd_build)

    sub.add_parser("column", help="пересобрать правую колонку (сетка + чат)").set_defaults(
        func=cmd_column)

    p = sub.add_parser("point-cards", help="чем смотрят источники карт")
    p.add_argument("mode", choices=["show", "hub", "tiles", "url"], help="режим")
    p.add_argument("--cards", type=int, nargs="+", choices=[1, 2, 3, 4],
                   help="номера карт (по умолчанию 2–4)")
    p.add_argument("--url", help="ссылка для режима url")
    p.set_defaults(func=cmd_point_cards)

    p = sub.add_parser("collection", help="хоткеи и порядок сцен в файле коллекции")
    p.add_argument("--dry-run", action="store_true", help="показать, не менять файл")
    p.set_defaults(func=cmd_collection)

    p = sub.add_parser("chat", help="локальный чат-релей")
    p.add_argument("--status", action="store_true", help="показать состояние релея")
    p.add_argument("--test", metavar="ТЕКСТ", help="вбросить тестовое сообщение")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("bracket", help="снять турнирную сетку и положить в OBS")
    p.add_argument("--place", action="store_true", help="заново задать положение в сценах")
    p.set_defaults(func=cmd_bracket)

    p = sub.add_parser("cocast", help="совместный каст через хаб")
    p.add_argument("action", choices=["on", "off", "status", "testfeed", "selftest", "viewer-key"])
    p.add_argument("--yes", action="store_true", help="подтвердить при живом эфире")
    p.add_argument("--minutes", type=int, default=15, help="сколько держать тестовую картинку")
    p.set_defaults(func=cmd_cocast)

    p = sub.add_parser("audio", help="звук: режим, сторож, проверки")
    p.add_argument("action", choices=["enable", "disable", "watch", "check", "route", "phones",
                                      "rescue", "watch-mic", "dota-fix", "split", "fix-scenes",
                                      "scene-report", "device", "levels"])
    p.add_argument("--probe", action="store_true", help="проверить, куда попадёт поток «по умолчанию»")
    p.add_argument("--device", help="имя выхода наушников для проверки")
    p.add_argument("--name", help="задать устройство вывода (для action=device)")
    p.add_argument("--seconds", type=int, default=40, help="сколько слушать (watch-mic)")
    p.add_argument("--watch", action="store_true", help="для dota-fix: следить постоянно")
    p.set_defaults(func=cmd_audio)

    p = sub.add_parser("audio-check", help="быстрая проверка звука")
    p.add_argument("--probe", action="store_true", help="плюс проверка маршрута «по умолчанию»")
    p.set_defaults(func=cmd_audio_check)

    sub.add_parser("status", help="что поднято и где смотреть").set_defaults(func=cmd_status)
    sub.add_parser("config", help="показать разрешённые пути и порты").set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load(args.config)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        return args.func(cfg, args)
    except ObsError as exc:
        print(f"OBS: {exc}", file=sys.stderr)
        return 3
    except layout.LayoutError as exc:
        print(f"раскладка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
