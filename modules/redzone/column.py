"""Правая колонка сцены: турнирная сетка сверху, чат снизу.

Два элемента колонки ведут разные подсистемы: картинку сетки кладёт `bracket.py`,
чат — этот модуль. Здесь же — копия чата под высоту колонки и перевод источника
чата на локальный чат-релей.

Почему отдельный источник чата: «Чат (заготовка)» — общий, в чужих сценах он
растянут на всю высоту. Менять размеры самого источника нельзя (там всё поедет),
поэтому для колонки делается копия нужного размера, а старый источник в этих
сценах убирается.
"""
from __future__ import annotations

from .layout import LayoutError, box_tf
from .obsclient import Obs

CHAT_CSS = "body { background-color: rgba(0,0,0,0); margin: 0; overflow: hidden; }"


def _refresh(obs: Obs, name: str) -> None:
    """Перечитать виджет: у браузерного источника кэш иначе отдаёт старую страницу."""
    obs.try_get("PressInputPropertiesButton", inputName=name, propertyName="refreshnocache")


def _place_column(obs: Obs, cfg, source: str, y: int, height: int) -> None:
    """Кладёт источник в правую колонку каждой сцены колонки."""
    for scene in cfg.column_scenes:
        item = obs.item(scene, source)
        if item is None:
            data = obs.get("CreateSceneItem", sceneName=scene, sourceName=source,
                           sceneItemEnabled=True)
            item_id = data.get("sceneItemId")
            if item_id is None:
                continue
        else:
            item_id = item["sceneItemId"]
        obs.try_get("SetSceneItemTransform", sceneName=scene, sceneItemId=item_id,
                    sceneItemTransform=box_tf((cfg.column_x, y),
                                              (cfg.column_width, height)))


def ensure_chat_source(obs: Obs, cfg) -> str:
    """Создаёт копию чата под колонку, если её нет. Возвращает имя источника."""
    name = cfg.src("chat_rz")
    if name in obs.inputs():
        obs.try_get("SetInputSettings", inputName=name, overlay=True,
                    inputSettings={"width": cfg.column_width,
                                   "height": cfg.column_chat_height})
        return name
    origin = obs.input_settings(cfg.src("chat")) if cfg.src("chat") in obs.inputs() else {}
    base = {k: v for k, v in origin.items() if k in ("url", "css")}
    if not base.get("css"):
        base["css"] = CHAT_CSS
    obs.get("CreateInput", inputName=name, inputKind="browser_source",
            sceneName=(cfg.column_scenes or obs.scenes() or ["Scene"])[0],
            sceneItemEnabled=True,
            inputSettings={**base, "width": cfg.column_width,
                           "height": cfg.column_chat_height})
    return name


def column(cfg) -> int:
    """Собирает правую колонку: чат своей высоты, старый высокий чат убирает."""
    with Obs.from_config(cfg) as obs:
        chat_rz = ensure_chat_source(obs, cfg)
        _place_column(obs, cfg, chat_rz, cfg.column_chat_y, cfg.column_chat_height)
        for scene in cfg.column_scenes:
            old = obs.item(scene, cfg.src("chat"))
            if old is not None:
                obs.try_get("RemoveSceneItem", sceneName=scene,
                            sceneItemId=old["sceneItemId"])
        _refresh(obs, chat_rz)
        print(f"колонка собрана: {chat_rz} {cfg.column_width}×{cfg.column_chat_height} "
              f"в ({cfg.column_x},{cfg.column_chat_y})")
        for scene in cfg.column_scenes:
            item = obs.item(scene, chat_rz)
            if item:
                t = item["sceneItemTransform"]
                print(f"  [{scene}] чат {t['width']:.0f}×{t['height']:.0f} "
                      f"в ({t['positionX']:.0f},{t['positionY']:.0f}) "
                      f"вкл={item['sceneItemEnabled']}")
        return 0


def connect_chat(cfg) -> int:
    """Переводит источник чата на локальный релей и проверяет, что он отрисовался."""
    url = f"http://{cfg.chat_host}:{cfg.chat_port}/"
    with Obs.from_config(cfg) as obs:
        name = ensure_chat_source(obs, cfg)
        if cfg.src("chat_rz") not in obs.inputs() and name not in obs.inputs():
            raise LayoutError("нет источника чата для колонки")
        obs.get("SetInputSettings", inputName=name, overlay=True,
                inputSettings={"url": url, "width": cfg.column_width,
                               "height": cfg.column_chat_height, "css": CHAT_CSS})
        _refresh(obs, name)
        import base64
        import time
        time.sleep(6)
        shot = obs.try_get("GetSourceScreenshot", sourceName=name, imageFormat="png",
                           imageWidth=cfg.column_width, imageHeight=cfg.column_chat_height)
        raw = base64.b64decode(shot["imageData"].split(",", 1)[1]) if shot.get("imageData") else b""
        if raw:
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            (cfg.state_dir / "chat_rz.png").write_bytes(raw)
        verdict = "РИСУЕТСЯ" if len(raw) > 5000 else "ПУСТО"
        print(f"источник «{name}» → {url}, кадр {len(raw)} байт ({verdict})")
        return 0 if raw else 1
