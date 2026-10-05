"""Турнирная сетка в эфире: снимает её браузером и кладёт картинкой в OBS.

Почему не браузерным источником: встроенный браузер OBS на этой машине не грузит
внешние сайты (локальные страницы рисует, внешние — нет), поэтому источник
показывал пустоту. Внешний браузер во внешнюю сеть ходит нормально, поэтому
страницу снимаем им и показываем PNG — так же надёжно и без встроенного браузера.

Идемпотентно: создаёт источник-картинку, раскладывает его по сценам колонки,
гасит старый браузерный источник и перезагружает картинку в OBS. Запуск по
таймеру обновляет снимок.

Страница турнира часто отдаёт 403 (Cloudflare), а модуль встраивания — 200 и
тёмную тему; адрес брать с `/module?theme=2`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .layout import LayoutError, box_tf
from .obsclient import Obs


def placeholder(cfg) -> Path:
    """Путь картинки по умолчанию; при отсутствии кладёт заготовку из образа модуля."""
    out = cfg.bracket_output
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        seed = cfg.overlay_dir / "bracket.png"
        if seed.exists():
            shutil.copy2(seed, out)
    return out


def capture(cfg) -> bool:
    """Снимает страницу сетки в PNG. True — файл обновлён."""
    out = cfg.bracket_output
    out.parent.mkdir(parents=True, exist_ok=True)
    before = out.stat().st_mtime if out.exists() else 0
    tmp = out.with_suffix(".tmp.png")
    try:
        result = subprocess.run(
            [cfg.bracket_chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", f"--window-size={cfg.bracket_width},{cfg.bracket_height}",
             "--virtual-time-budget=9000", f"--screenshot={tmp}", cfg.bracket_url],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"!! не удалось снять сетку: {exc}")
        return False
    if not tmp.exists() or tmp.stat().st_size < 3000:
        tail = (result.stderr or result.stdout or "").strip()[-200:]
        print(f"!! не удалось снять сетку: {tail}")
        return False
    tmp.replace(out)
    changed = out.stat().st_mtime != before
    print(f"сетка снята: {out.stat().st_size} байт" + ("" if changed else " (без изменений)"))
    return True


def place(cfg, *, reposition: bool = False) -> None:
    """Раскладывает картинку сетки по сценам колонки.

    Если источник в сцене уже есть, его положение не трогаем — чтобы выставленное
    вручную место не съезжало на каждом обновлении. Первую раскладку делает
    `reposition=True` (флаг `--place`).
    """
    with Obs.from_config(cfg) as obs:
        if obs.busy():
            raise LayoutError("идёт эфир или запись — сцены не трогаю")
        path = placeholder(cfg)
        name = cfg.src("bracket")
        if name in obs.inputs():
            # переустановка того же пути заставляет OBS перечитать файл с диска
            obs.try_get("SetInputSettings", inputName=name, overlay=True,
                        inputSettings={"file": str(path)})
        else:
            obs.get("CreateInput", inputName=name, inputKind="image_source",
                    sceneName=(cfg.column_scenes or obs.scenes())[0],
                    sceneItemEnabled=True, inputSettings={"file": str(path)})
        for scene in cfg.column_scenes:
            item = obs.item(scene, name)
            created = item is None
            if created:
                data = obs.get("CreateSceneItem", sceneName=scene, sourceName=name,
                               sceneItemEnabled=True)
                item_id = data.get("sceneItemId")
            else:
                item_id = item["sceneItemId"]
            if item_id is None:
                continue
            if created or reposition:
                obs.try_get("SetSceneItemTransform", sceneName=scene, sceneItemId=item_id,
                            sceneItemTransform=box_tf((cfg.column_x, cfg.column_bracket_y),
                                                      (cfg.column_width, cfg.column_bracket_height)))
            # старый браузерный источник гасим — он всё равно пустой
            legacy = obs.item(scene, cfg.src("grid"))
            if legacy and legacy.get("sceneItemEnabled"):
                obs.try_get("SetSceneItemEnabled", sceneName=scene,
                            sceneItemId=legacy["sceneItemId"], sceneItemEnabled=False)
        print("источник сетки разложен по сценам: " + ", ".join(cfg.column_scenes))


def run(cfg, *, place_it: bool = False) -> int:
    """Снимает сетку и раскладывает её. Если адрес не задан — только раскладывает."""
    if cfg.bracket_url:
        if not capture(cfg):
            return 1
    else:
        print("адрес сетки не задан (bracket.url) — обновляю раскладку без снимка")
    place(cfg, reposition=place_it)
    return 0
