"""Раскладка RedZone: «одна большая карта + полоса миниатюр + правая колонка».

Сборщик идемпотентный: источники и сцены создаются только если их нет, а
трансформы и видимость выставляются каждый прогон — повторный запуск ничего не
дублирует и не ломает.

Геометрия — константами (сверена со снимком рабочих сцен): канва 2560×1440,
крупная плитка 1984×1116 в (24, 24), миниатюры 512×288 в правой колонке с шагом
304 px, чат занимает остаток колонки, вебка — в левом нижнем углу.

Карты 2..N берут картинку из приёмного хаба (медиа-источник по RTSP) и стоят
поверх плитки-заглушки: пока потока нет, медиа-источник прозрачен и видна
заглушка «КАРТА N». Карта 1 по умолчанию показывает свою игру (захват).

Порядок работы с источниками важен: OBS выбрасывает источники, на которые не
ссылается ни одна сцена, при первом же сохранении коллекции. Поэтому сборщик
сначала создаёт источники, потом сцены, и только затем убирает лишнее.
"""
from __future__ import annotations

from .obsclient import Obs, ObsError

# ── геометрия (канва 2560×1440) ──────────────────────────────────────────────
CANVAS = (2560, 1440)

BIG = {
    "frame": ((20.0, 20.0), (1992.0, 1124.0)),
    "video": ((24.0, 24.0), (1984.0, 1116.0)),
    "label": ((38.0, 1128.0), None),
}
SLOT_STEP, COL_X = 304.0, 2024.0
THUMB = {
    "frame": ((2020.0, 20.0), (520.0, 296.0)),
    "video": ((COL_X, 24.0), (512.0, 288.0)),
    "label": ((2038.0, 300.0), None),
}
CHAT = ((2024.0, 632.0), (512.0, 784.0))
SOLO = ((0.0, 0.0), (2560.0, 1440.0))
SOLO_LABEL = (38.0, 1400.0)

# нижняя полоса мозаичной сцены: вебка | алерты | плашка RED ZONE
WEBCAM = {"frame": (16, 1148, 478, 276), "video": (24, 1156, 462, 260)}
ALERTS = (502.0, 1156.0, 347.0, 260.0)
PANEL = (865.0, 1156.0, 1143.0, 260.0)
TITLE_AT = (PANEL[0] + PANEL[2] / 2, PANEL[1] + PANEL[3] / 2 - 30)
SUBTITLE_AT = (PANEL[0] + PANEL[2] / 2, PANEL[1] + PANEL[3] / 2 + 78)

C_BG = 0x0E0E12
C_FRAME = 0x2A2A34
C_FOCUS = 0xE53935
C_PANEL = 0x200A0C
C_TITLE = 0xFFFFFF
C_SUBTITLE = 0xFF8A80

ACCENTS = {1: "#e53935", 2: "#29b6f6", 3: "#66bb6a", 4: "#ffa726"}

TILE_TPL = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{title}</title><style>
  html, body {{ margin: 0; height: 100%; background: #0a0a0d; }}
  .t {{ box-sizing: border-box; width: 100vw; height: 100vh; border: 6px solid {accent};
        display: flex; align-items: center; justify-content: center;
        font-family: "DejaVu Sans", system-ui, sans-serif; }}
  .n {{ font-size: 120px; font-weight: 700; letter-spacing: 6px; color: {accent}; }}
</style></head><body><div class="t">
  <div class="n">{label}</div>
</div></body></html>
"""


def abgr(rgb: int) -> int:
    """RGB -> то, что ждёт color_source (0xAABBGGRR, а не ARGB)."""
    r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
    return (0xFF << 24) | (b << 16) | (g << 8) | r


def box_tf(pos, size) -> dict:
    """Трансформ плитки: левый верх, вписывание по внутреннему краю."""
    x, y = pos
    w, h = size
    return {"positionX": x, "positionY": y, "rotation": 0, "scaleX": 1, "scaleY": 1,
            "alignment": 5, "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 5,
            "boundsWidth": w, "boundsHeight": h}


def anchor_tf(x, y, alignment: int, scale: float = 1.0) -> dict:
    """Трансформ подписи: позиция — анкер, размер берётся у источника."""
    return {"positionX": x, "positionY": y, "rotation": 0, "scaleX": scale, "scaleY": scale,
            "alignment": alignment, "boundsType": "OBS_BOUNDS_NONE"}


def strip_tf(x, y, w, h) -> dict:
    """Трансформ элемента нижней полосы: растянуть по заданному прямоугольнику."""
    return {"positionX": x, "positionY": y, "rotation": 0, "scaleX": 1, "scaleY": 1,
            "alignment": 5, "boundsType": "OBS_BOUNDS_STRETCH", "boundsAlignment": 0,
            "boundsWidth": w, "boundsHeight": h}


class LayoutError(RuntimeError):
    """Сборка раскладки невозможна (обычно мешает эфир или пропал источник)."""


class Builder:
    """Сборщик раскладки поверх одного соединения с OBS."""

    def __init__(self, cfg, obs: Obs):
        self.cfg = cfg
        self.obs = obs
        self.created_inputs: list[str] = []
        self.created_scenes: list[str] = []
        self.warnings: list[str] = []

    # ── источники ──
    def ensure_input(self, name: str, kind: str, settings: dict,
                     *, scene: str, mute: bool | None = None) -> bool:
        """Создаёт источник, если его нет. Существующий не переписывает.

        obs-websocket v5 требует `sceneName` даже для «глобального» источника:
        создаём внутри служебной сцены, элемент потом раскладывается как надо.
        """
        if name in self.obs.inputs():
            return False
        self.obs.get("CreateInput", sceneName=scene, inputName=name, inputKind=kind,
                     inputSettings=settings)
        if mute is not None:
            self.obs.try_get("SetInputMute", inputName=name, inputMuted=mute)
        self.created_inputs.append(name)
        return True

    def ensure_scene(self, name: str) -> bool:
        if name in self.obs.scenes():
            return False
        self.obs.get("CreateScene", sceneName=name)
        self.created_scenes.append(name)
        return True

    def place(self, scene: str, source: str, tf: dict, index: int,
              *, visible: bool = True) -> None:
        """Кладёт элемент в сцену на нужный слой и выставляет трансформ."""
        item = self.obs.item(scene, source)
        if item is None:
            data = self.obs.get("CreateSceneItem", sceneName=scene, sourceName=source,
                                sceneItemEnabled=visible)
            item_id = data.get("sceneItemId")
        else:
            item_id = item["sceneItemId"]
        if item_id is None:
            self.warnings.append(f"{scene}: элемент {source} не создался")
            return
        self.obs.try_get("SetSceneItemTransform", sceneName=scene, sceneItemId=item_id,
                         sceneItemTransform=tf)
        self.obs.try_get("SetSceneItemEnabled", sceneName=scene, sceneItemId=item_id,
                         sceneItemEnabled=bool(visible))
        self.obs.try_get("SetSceneItemIndex", sceneName=scene, sceneItemId=item_id,
                         sceneItemIndex=index)

    # ── типовые сборки ──
    def _card_inputs(self, n: int, host: str, hub: bool) -> None:
        c = self.cfg
        self.ensure_input(c.card("stream", n), "browser_source", {
            # URL-режим, а не «Local file»: чтобы поставить трансляцию, достаточно
            # заменить текст ссылки в свойствах источника, не ища галочку.
            "is_local_file": False, "url": f"file://{c.tiles_dir / f'tile{n}.html'}",
            "width": 1920, "height": 1080, "fps": 60, "fps_custom": False,
            "shutdown": False, "restart_when_active": True, "reroute_audio": False,
            "page_permissions_enabled": False,
            "css": "body { background-color: rgba(0,0,0,0); margin: 0; overflow: hidden; }",
        }, scene=host, mute=True)
        if not hub:
            return
        path = c.hub_card.format(n=n)
        self.ensure_input(c.card("hub", n), "ffmpeg_source", {
            "is_local_file": False, "input": f"{c.hub_rtsp.rstrip('/')}/{path}",
            "input_format": "", "hw_decode": False, "reconnect_delay_sec": 3,
            "close_when_inactive": False, "clear_on_media_end": True, "looping": False,
            "restart_on_activate": False,
            # буфер гасит рывки на сетевых провалах; размен +1–2 с задержки на плавность
            # для мультитрансляции правильный
            "buffering_mb": 3,
        }, scene=host, mute=False)
        # «эфир» карты с хаба: если туда вписали ссылку хаба, вернуть локальную заглушку
        stream = c.card("stream", n)
        cur = self.obs.input_settings(stream)
        url = str(cur.get("url") or cur.get("local_file") or "")
        if c.hub_webrtc in url or c.hub_rtsp in url:
            tile = c.tiles_dir / f"tile{n}.html"
            self.obs.try_get("SetInputSettings", inputName=stream, overlay=True,
                             inputSettings=dict(cur, url=f"file://{tile}", is_local_file=True,
                                                local_file=str(tile)))

    def _global_inputs(self, host: str, styles: bool) -> None:
        c = self.cfg
        self.ensure_input(c.src("background"), "color_source_v3",
                          {"srgb": True, "color": abgr(C_BG), "width": CANVAS[0],
                           "height": CANVAS[1]}, scene=host)
        self.ensure_input(c.src("panel"), "color_source_v3",
                          {"srgb": True, "color": abgr(C_PANEL), "width": int(PANEL[2]),
                           "height": int(PANEL[3])}, scene=host)
        self.ensure_input(c.src("title"), "text_ft2_source_v2", {
            "text": "RED ZONE", "custom_width": 0, "word_wrap": False,
            "font": {"face": "DejaVu Sans", "flags": 0, "size": 110, "style": "Bold"},
            "antialiasing": True, "outline": True, "drop_shadow": True,
            "color1": abgr(C_TITLE), "color2": abgr(0x000000)}, scene=host)
        self.ensure_input(c.src("subtitle"), "text_ft2_source_v2", {
            "text": "мультитрансляция", "custom_width": 0, "word_wrap": False,
            "font": {"face": "DejaVu Sans", "flags": 0, "size": 40, "style": ""},
            "antialiasing": True, "outline": True, "drop_shadow": True,
            "color1": abgr(C_SUBTITLE), "color2": abgr(0x000000)}, scene=host)
        for n in self._cards():
            self.ensure_input(c.card("frame", n), "color_source_v3",
                              {"srgb": True, "color": abgr(C_FRAME), "width": 64, "height": 64},
                              scene=host)
            self.ensure_input(c.card("focus", n), "color_source_v3",
                              {"srgb": True, "color": abgr(C_FOCUS), "width": 64, "height": 64},
                              scene=host)
            self.ensure_input(c.card("label", n), "text_ft2_source_v2", {
                "text": f"КАРТА {n}", "custom_width": 0, "log_mode": False, "word_wrap": False,
                "font": {"face": "DejaVu Sans", "flags": 0, "size": 48, "style": "Bold"},
                "antialiasing": True, "outline": True, "drop_shadow": True,
                "color1": abgr(0xFFFFFF), "color2": abgr(0x000000)}, scene=host)

    def _cards(self) -> list[int]:
        """Номера карт раскладки (из сцен-мозаик)."""
        nums = set(self.cfg.solo_scenes.values())
        for focus, thumbs in self.cfg.mosaic_scenes.values():
            nums.add(focus)
            nums.update(thumbs)
        return sorted(nums)

    # ── сборка ──
    def build(self, *, hide: bool = False) -> None:
        c = self.cfg
        scenes = list(c.mosaic_scenes) + list(c.solo_scenes)
        if not scenes:
            raise LayoutError("в конфиге нет ни одной сцены раскладки")
        host = scenes[0]
        self.ensure_scene(host)
        write_tiles(c)
        self._global_inputs(host, styles=False)
        for n in self._cards():
            self._card_inputs(n, host, hub=(n != 1))

        missing = [c.src(k) for k in ("webcam", "webcam_frame", "chat")
                   if c.src(k) not in self.obs.inputs()]
        if missing:
            self.warnings.append("в OBS нет источников: " + ", ".join(missing)
                                 + " — раскладка собрана без них")

        for name, (focus, thumbs) in c.mosaic_scenes.items():
            self._build_mosaic(name, int(focus), [int(t) for t in thumbs], hide=hide)
        for name, n in c.solo_scenes.items():
            self._build_solo(name, int(n))
        print(f"собрано сцен: {len(scenes)} "
              f"(мозаика {len(c.mosaic_scenes)}, одиночные {len(c.solo_scenes)})")

    def _toolbar(self, scene: str, idx: int) -> int:
        c = self.cfg
        for key, tf in ((c.src("alerts"), strip_tf(*[float(v) for v in ALERTS])),
                        (c.src("panel"), strip_tf(*[float(v) for v in PANEL])),
                        (c.src("title"), anchor_tf(*TITLE_AT, 0)),
                        (c.src("subtitle"), anchor_tf(*SUBTITLE_AT, 0))):
            if key not in self.obs.inputs():
                continue
            self.place(scene, key, tf, idx)
            idx += 1
        return idx

    def _webcam(self, scene: str, idx: int) -> int:
        """Вебка — в левом нижнем углу; кладём поверх остальных элементов полосы."""
        c = self.cfg
        for key, (x, y, w, h) in (("webcam_frame", (WEBCAM["frame"][0] - 8, WEBCAM["frame"][1] - 8,
                                                    WEBCAM["frame"][2] + 16, WEBCAM["frame"][3] + 16)),
                                  ("webcam", WEBCAM["video"])):
            if c.src(key) not in self.obs.inputs():
                continue
            self.place(scene, c.src(key), strip_tf(x, y, w, h), idx)
            idx += 1
        return idx

    def _build_mosaic(self, scene: str, focus: int, thumbs: list[int], *, hide: bool) -> None:
        c = self.cfg
        self.ensure_scene(scene)
        order = [focus] + [n for n in thumbs if n != focus]
        idx = 0
        self.place(scene, c.src("background"), box_tf((0, 0), CANVAS), idx)
        idx += 1
        for slot, n in enumerate(order):
            where = "big" if slot == 0 else slot - 1
            rect = (BIG if where == "big" else THUMB)
            if where != "big":
                dy = SLOT_STEP * where
                rect = {
                    "frame": ((THUMB["frame"][0][0], THUMB["frame"][0][1] + dy),
                              THUMB["frame"][1]),
                    "video": ((THUMB["video"][0][0], THUMB["video"][0][1] + dy),
                              THUMB["video"][1]),
                    "label": ((THUMB["label"][0][0], THUMB["label"][0][1] + dy), None),
                }
            for key in ("frame", "focus", "stream", "game", "label"):
                source = c.card(key, n)
                if key == "game" and source not in self.obs.inputs():
                    continue  # локальный захват игры у каждого свой, его может не быть
                if key in ("frame", "focus"):
                    pos, size = rect["frame"]
                    self.place(scene, source, box_tf(pos, size), idx,
                               visible=(where != "big" if key == "frame" else where == "big"))
                elif key in ("stream", "game"):
                    tf = box_tf(rect["video"][0], rect["video"][1])
                    visible = (key == "stream")
                    self.place(scene, source, tf, idx, visible=visible)
                else:  # label
                    pos = rect["label"][0]
                    self.place(scene, source,
                               anchor_tf(pos[0], pos[1], 9, 1.4 if where == "big" else 1.0),
                               idx, visible=True)
                idx += 1
            # поверх заглушки — картинка карты (своя игра или поток из хаба)
            if hide:
                continue
            video = c.src("local_game") if n == 1 else c.card("hub", n)
            if video in self.obs.inputs():
                self.place(scene, video, box_tf(rect["video"][0], rect["video"][1]), idx)
                idx += 1
        idx = self._toolbar(scene, idx)
        if c.src("chat") in self.obs.inputs():
            self.place(scene, c.src("chat"), box_tf(CHAT[0], CHAT[1]), idx)
            idx += 1
        self._webcam(scene, idx)

    def _build_solo(self, scene: str, n: int) -> None:
        c = self.cfg
        self.ensure_scene(scene)
        idx = 0
        self.place(scene, c.src("background"), box_tf(SOLO[0], SOLO[1]), idx)
        idx += 1
        if c.card("stream", n) in self.obs.inputs():
            self.place(scene, c.card("stream", n), box_tf(SOLO[0], SOLO[1]), idx)
            idx += 1
        video = c.src("local_game") if n == 1 else c.card("hub", n)
        if video in self.obs.inputs():
            self.place(scene, video, box_tf(SOLO[0], SOLO[1]), idx)
            idx += 1
        if c.card("label", n) in self.obs.inputs():
            self.place(scene, c.card("label", n), anchor_tf(*SOLO_LABEL, 9), idx)
            idx += 1
        self._webcam(scene, idx)

    # ── отчёт ──
    def summary(self) -> None:
        print("\nсцены раскладки:")
        for scene in list(self.cfg.mosaic_scenes) + list(self.cfg.solo_scenes):
            items = self.obs.items(scene)
            shown = [i["sourceName"] for i in items if i.get("sceneItemEnabled")]
            print(f"  {scene:12} элементов={len(items):3} видимых={len(shown):3}")


def write_tiles(cfg, *, force: bool = False) -> None:
    """Пишет страницы-заглушки карт, если их ещё нет (или принудительно)."""
    cfg.tiles_dir.mkdir(parents=True, exist_ok=True)
    for n in sorted(set(cfg.solo_scenes.values()) | {1, 2, 3, 4}):
        path = cfg.tiles_dir / f"tile{n}.html"
        if force or not path.exists():
            path.write_text(TILE_TPL.format(title=f"Карта {n}", accent=ACCENTS.get(n, "#8a8a94"),
                                            label=f"КАРТА {n}"), encoding="utf-8")


def build(cfg, *, hide: bool = False) -> Builder:
    """Собирает/обновляет раскладку. Отказывается работать на живом эфире."""
    with Obs.from_config(cfg) as obs:
        if obs.busy():
            raise LayoutError("идёт эфир или запись — сцены не трогаю")
        b = Builder(cfg, obs)
        b.build(hide=hide)
        b.summary()
        for w in b.warnings:
            print("!! " + w)
        return b


# ── чем смотрят карты: заглушка / хаб / своя ссылка ──────────────────────────

def point_cards(cfg, *, cards=None, mode: str = "show", url: str | None = None) -> int:
    """Перенаправляет источники карт «RZ КN эфир».

    Режимы: `show` — что вписано сейчас, `hub` — читать приёмный хаб (WebRTC),
    `tiles` — вернуть локальные заглушки, `url` — произвольная ссылка.
    Меняет только URL браузерных источников; эфир/запись здесь не помеха.
    """
    if mode not in {"show", "hub", "tiles", "url"}:
        raise LayoutError(f"неизвестный режим: {mode}")
    if mode == "url" and not url:
        raise LayoutError("для режима url нужна ссылка (--url)")
    with Obs.from_config(cfg) as obs:
        if mode == "show":
            for n in sorted({1, 2, 3, 4}):
                settings = obs.input_settings(cfg.card("stream", n))
                target = settings.get("url") or settings.get("local_file") or ""
                print(f"{cfg.card('stream', n)}: {target}")
            return 0
        if obs.streaming():
            raise LayoutError("идёт эфир — ссылки источников не меняю")
        targets = [int(n) for n in (cards or [2, 3, 4])]
        for n in targets:
            if mode == "tiles":
                target = f"file://{cfg.tiles_dir / f'tile{n}.html'}"
            elif mode == "hub":
                target = f"{cfg.hub_webrtc.rstrip('/')}/{cfg.hub_card.format(n=n)}"
            else:
                target = str(url)
            obs.get("SetInputSettings", inputName=cfg.card("stream", n), overlay=True,
                    inputSettings={"url": target, "is_local_file": False,
                                   "restart_when_active": True})
            print(f"{cfg.card('stream', n)} → {target}")
    return 0
