"""Снапшот состояния OBS для фронта: сцены, транспорт, звук, свежесть данных.

Panel не знает ни про HTTP, ни про транспорт к OBS: он получает объект с
`connected` и `async req(...)`, опрашивает OBS и собирает один обычный dict —
контракт для фронта. Здесь нет сетевых зависимостей, поэтому модуль легко
тестировать стабом.

Главная идея снапшота — «не мигать пустотой»: отказ отдельного запроса не
обнуляет уже показанные данные (кроме явно оговорённых случаев), а при обрыве
связи последние известные сцены сохраняются и помечаются stale.
"""
from __future__ import annotations

import asyncio
import copy
import math
import time

MIC = "Микрофон"
SYS = "Звук системы"

# Реальные имена сцен владельца -> номер numpad (коллекция «Стрим»).
SCENE_NUMS: dict[str, int] = {
    "Dota 2": 1,
    "Hearthstone": 2,
    "WoW": 3,
    "Just Chatting": 4,
    "Рабочий стол": 5,
    "Перерыв": 6,
    "Начало стрима": 7,
    "Конец стрима": 8,
    "Чёрный экран": 9,
}

_TIMECODE_LEN = 11  # «ЧЧ:ММ:СС.мс» без последней доли
_OFFLINE = "OBS офлайн"
_EMPTY_STREAM = {"active": False, "time": "", "reconnect": False}
_EMPTY_RECORD = {"active": False, "time": ""}

# Интервалы по умолчанию (см. docs/ARCHITECTURE.md): полный ресинк каждые 2 с, порог устаревания —
# 10 с. Равные значения заставляли бы stale мигать ровно на границе опроса.
DEFAULT_REFRESH_INTERVAL = 2.0
DEFAULT_STALE_AFTER = 10.0
# Сколько секунд уровень считается «живым» после последнего события метроб.
DEFAULT_METER_STALE_AFTER = 1.0


def _timecode(value) -> str:
    return str(value or "")[:_TIMECODE_LEN]


def _percent(multiplier) -> int | None:
    """inputVolumeMul (0..1) -> проценты 0..100, с округлением и зажимом."""
    if multiplier is None:
        return None
    try:
        percent = int(round(float(multiplier) * 100))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, percent))


def _magnitude(channels) -> float:
    """Уровень входа из inputLevelsMul: магнитуда (индекс 0) самого громкого канала.

    У OBS на канал идут три числа: магнитуда, пик, inputPeak. Пиковые поля бывают
    мусорными (наблюдали 3.048e+32 при нулевой магнитуде), поэтому читаем только
    магнитуду и зажимаем её снизу нулём. Нет пригодных данных — 0.0.
    """
    values: list[float] = []
    for channel in channels or []:
        if not channel:
            continue
        try:
            value = float(channel[0])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(value):
            values.append(max(0.0, value))
    return max(values) if values else 0.0


class Panel:
    """Держит последний снапшот состояния OBS и умеет его обновлять.

    obs — любой объект с `connected: bool` и `async req(request_type, **data)`;
    clock — внедряемые монотонные часы (для проверки stale в тестах).
    """

    def __init__(self, obs, *, scene_nums=None, mic: str = MIC, sys: str = SYS,
                 stale_after: float = DEFAULT_STALE_AFTER,
                 meter_stale_after: float = DEFAULT_METER_STALE_AFTER,
                 refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
                 clock=time.monotonic):
        self.obs = obs
        self.scene_nums = dict(scene_nums) if scene_nums is not None else dict(SCENE_NUMS)
        self.mic = mic
        self.sys = sys
        self.stale_after = float(stale_after)
        self.meter_stale_after = float(meter_stale_after)
        self.refresh_interval = float(refresh_interval)
        self.clock = clock
        self._snap = self._empty()
        self._updated_at = self.clock()
        # Момент последнего события метроб; -inf — уровней ещё не было.
        self._meters_at = float("-inf")

    # ── чтение состояния ──
    @property
    def state(self) -> dict:
        """Копия последнего снапшота; stale пересчитывается на момент чтения.

        Уровни звука — тоже живые данные: без связи или если события метроб
        замолчали дольше порога, показываем их неизвестными, а не застывшими.
        """
        snap = copy.deepcopy(self._snap)
        snap["stale"] = self.is_stale()
        if not self.obs.connected or not self._meters_fresh():
            snap["audio"] = self._unknown_levels(snap.get("audio"))
        return snap

    def is_stale(self) -> bool:
        """Данные устарели, если OBS офлайн или снапшот старше stale_after."""
        if not self._snap.get("obs"):
            return True
        return (self.clock() - self._updated_at) > self.stale_after

    # ── обновление ──
    async def refresh(self) -> dict:
        """Полный опрос OBS и сборка снапшота. Не бросает: отказы — в снапшот."""
        now = self.clock()
        if not self.obs.connected:
            self._store({**self._snap, "obs": False, "error": _OFFLINE}, now)
            return self.state

        try:
            scene_data = await self.obs.req("GetSceneList")
        except Exception as exc:
            # Список сцен критичен: без него состояния нет, но старое не выкидываем.
            self._store({**self._snap, "obs": False, "error": str(exc)}, now)
            return self.state

        names = [s.get("sceneName") for s in (scene_data.get("scenes") or [])
                 if s.get("sceneName") is not None]
        snap = self._empty()
        snap["obs"] = True
        snap["error"] = None
        snap["scene"] = scene_data.get("currentProgramSceneName")
        snap["scenes"] = self._sort_scenes(names)
        snap["stream"] = await self._stream()
        snap["record"] = await self._record()
        snap["replay"] = await self._replay()
        snap["audio"] = {"mic": await self._audio("mic", self.mic),
                         "sys": await self._audio("sys", self.sys)}
        self._store(snap, now)
        return self.state

    # ── события OBS ──
    def on_event(self, event) -> None:
        """Событие obs-websocket: уровни звука и смена связи.

        Метры приходят ТОЛЬКО событием InputVolumeMeters — запроса для них в
        obs-websocket v5 нет. Пока связи нет, событие не применяем: снапшот не
        должен показывать живой уровень по мёртвому соединению.
        """
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind in ("connected", "disconnected"):
            self._reset_meters()
        elif event.get("eventType") == "InputVolumeMeters" and self.obs.connected:
            self._apply_meters(event.get("eventData") or {})

    async def run(self) -> None:
        """Полный рефреш по расписанию; уровни звука приходят событием on_event."""
        await self._tick(self.refresh_interval, self.refresh)

    # ── внутреннее ──
    async def _tick(self, interval: float, action) -> None:
        while True:
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def _stream(self) -> dict:
        try:
            data = await self.obs.req("GetStreamStatus")
        except Exception:
            # Отказ запроса — не повод объявлять «эфира нет»: во время эфира это
            # заблокировало бы кнопку «Стоп эфира». Оставляем прежнее состояние.
            return dict(self._snap.get("stream") or _EMPTY_STREAM)
        return {"active": bool(data.get("outputActive")),
                "time": _timecode(data.get("outputTimecode")),
                "reconnect": bool(data.get("outputReconnecting"))}

    async def _record(self) -> dict:
        try:
            data = await self.obs.req("GetRecordStatus")
        except Exception:
            return dict(self._snap.get("record") or _EMPTY_RECORD)
        return {"active": bool(data.get("outputActive")),
                "time": _timecode(data.get("outputTimecode"))}

    async def _replay(self) -> bool:
        try:
            data = await self.obs.req("GetReplayBufferStatus")
        except Exception:
            return False
        return bool(data.get("outputActive"))

    async def _audio(self, key: str, name: str) -> dict:
        """mute/vol одного источника; отказ любого из двух запросов -> None."""
        prev_level = ((self._snap.get("audio") or {}).get(key) or {}).get("level")
        if not self._meters_fresh():
            prev_level = None
        try:
            muted = await self.obs.req("GetInputMute", inputName=name)
            volume = await self.obs.req("GetInputVolume", inputName=name)
        except Exception:
            return {"mute": None, "vol": None, "level": prev_level}
        return {"mute": bool(muted.get("inputMuted")),
                "vol": _percent(volume.get("inputVolumeMul")),
                "level": prev_level}

    def _apply_meters(self, event_data: dict) -> None:
        """InputVolumeMeters -> уровни в снапшоте (магнитуда, может быть и > 1)."""
        levels: dict = {}
        for item in event_data.get("inputs") or []:
            if not isinstance(item, dict):
                continue
            levels[item.get("inputName")] = _magnitude(item.get("inputLevelsMul"))
        audio = dict(self._snap.get("audio") or {})
        for key, name in (("mic", self.mic), ("sys", self.sys)):
            channel = dict(audio.get(key) or {"mute": None, "vol": None, "level": None})
            # Источника нет в событии — он честно неизвестен, а не застыл.
            channel["level"] = levels.get(name)
            audio[key] = channel
        self._snap = {**self._snap, "audio": audio}
        self._meters_at = self.clock()

    def _reset_meters(self) -> None:
        """Связь сменилась — прежние уровни недействительны."""
        self._meters_at = float("-inf")
        self._snap = {**self._snap,
                      "audio": self._unknown_levels(self._snap.get("audio"))}

    def _meters_fresh(self) -> bool:
        return (self.clock() - self._meters_at) <= self.meter_stale_after

    @staticmethod
    def _unknown_levels(audio) -> dict:
        """Копия audio с обнулёнными уровнями (уровень неизвестен)."""
        out = {}
        for key, channel in (audio or {}).items():
            channel = dict(channel or {})
            channel["level"] = None
            out[key] = channel
        return out

    def _sort_scenes(self, names: list[str]) -> list[dict]:
        """Сцены по номеру numpad; сцены без маппинга — в конце с num=None."""

        def key(name: str):
            num = self.scene_nums.get(name)
            return (num is None, num if num is not None else 0, name)

        # Имена уникальны в OBS, но дубликат в ответе не должен превращаться в две
        # плитки (визуальный «двойник» сцены с тем же номером).
        unique = list(dict.fromkeys(names))
        return [{"name": name, "num": self.scene_nums.get(name)}
                for name in sorted(unique, key=key)]

    def _store(self, snap: dict, now: float) -> None:
        snap["updated_at"] = now
        self._snap = snap
        self._updated_at = now
        self._snap["stale"] = self.is_stale()

    @staticmethod
    def _empty() -> dict:
        return {
            "obs": False,
            "error": None,
            "scene": None,
            "scenes": [],
            "stream": dict(_EMPTY_STREAM),
            "record": dict(_EMPTY_RECORD),
            "replay": False,
            "audio": {"mic": {"mute": None, "vol": None, "level": None},
                      "sys": {"mute": None, "vol": None, "level": None}},
            "stale": True,
            "updated_at": 0.0,
        }
