"""Хоткеи и порядок сцен раскладки в файле коллекции OBS.

obs-websocket не умеет назначать хоткеи, поэтому правим файл коллекции, когда OBS
его не держит: сначала просим OBS переключиться на другую (пустую) коллекцию — он
сохраняет нужную на диск, — правим JSON, затем возвращаем коллекцию, и OBS читает
файл заново. Перезапуск OBS не нужен; эфир и запись не затрагиваются (скрипт их
проверяет).

Грабли: OBS хранит модификаторы хоткея плоско (`{"key": ..., "control": true}`),
а вложенный `"modifiers": {...}` молча теряется — биндинг превращается в обычную
клавишу. Проверено на живом OBS.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .obsclient import Obs


def _hotkeys(scene: dict, key: str) -> dict:
    hk = scene.setdefault("hotkeys", {})
    hk["OBSBasic.SelectScene"] = [{"key": key, "control": True}]
    return hk


def patch(cfg, *, dry_run: bool = False, obs: Obs | None = None) -> int:
    """Применяет хоткеи и порядок сцен к активной коллекции. 0 — успех, 1 — отказ.

    `obs` можно передать готовым соединением (используется проверками); по умолчанию
    подключаемся по конфигу.
    """
    close = False
    if obs is None:
        obs = Obs.from_config(cfg).connect()
        close = True
    try:
        return _patch(cfg, obs, dry_run=dry_run)
    finally:
        if close:
            obs.close()


def _patch(cfg, obs: Obs, *, dry_run: bool) -> int:
    if obs.busy():
        print("!! идёт эфир или запись — файл коллекции не трогаю")
        return 1
    listing = obs.get("GetSceneCollectionList")
    current = listing.get("currentSceneCollectionName")
    collections = listing.get("sceneCollections") or []
    print("активная коллекция:", current)
    if current != cfg.collection:
        print(f"!! активна не «{cfg.collection}» — сначала переключись на неё")
        return 1
    if cfg.spare_collection not in collections:
        print("доступные коллекции:", collections)
        print(f"!! нет коллекции-отстойника «{cfg.spare_collection}» — создать её в OBS")
        return 1

    backups = cfg.state_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    # уйти в другую коллекцию — заставляет OBS сохранить нужную на диск
    obs.get("SetCurrentSceneCollection", sceneCollectionName=cfg.spare_collection)
    time.sleep(1.0)
    source = cfg.scenes_dir / f"{cfg.collection}.json"
    if not source.exists():
        obs.try_get("SetCurrentSceneCollection", sceneCollectionName=cfg.collection)
        print(f"!! не нашёл файл коллекции {source}")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(source, backups / f"{cfg.collection}.json.{stamp}.pre-hotkeys.bak")
    data = json.loads(source.read_text(encoding="utf-8"))

    names = {s["name"] for s in data.get("sources", []) if s.get("id") == "scene"}
    missing = [n for n in cfg.scene_order if n not in names]
    if missing:
        obs.try_get("SetCurrentSceneCollection", sceneCollectionName=cfg.collection)
        print("!! в коллекции нет сцен:", missing)
        return 1

    for scene in data.get("sources", []):
        if scene.get("id") == "scene" and scene["name"] in cfg.hotkeys:
            _hotkeys(scene, cfg.hotkeys[scene["name"]])
    old_order = [s["name"] for s in data.get("scene_order", [])]
    new_order = ([s for s in cfg.scene_order if s in old_order]
                 + [s for s in old_order if s not in cfg.scene_order])
    data["scene_order"] = [{"name": n} for n in new_order]
    print("хоткеи (Ctrl+Numpad):", {n: k for n, k in cfg.hotkeys.items()})
    print("порядок сцен:\n  было :", old_order, "\n  стало:", new_order)

    if dry_run:
        obs.try_get("SetCurrentSceneCollection", sceneCollectionName=cfg.collection)
        print("(dry-run: файл не изменён)")
        return 0

    indent = 4 if source.read_text(encoding="utf-8").startswith("{\n    ") else 1
    source.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    obs.get("SetCurrentSceneCollection", sceneCollectionName=cfg.collection)
    time.sleep(1.2)
    scenes = obs.scenes()
    print("сцены после перезагрузки:", scenes[:8], "…")
    return 0
