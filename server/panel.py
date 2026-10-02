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


class Panel:
    """Держит последний снапшот состояния OBS и умеет его обновлять.

    obs — любой объект с `connected: bool` и `async req(request_type, **data)`;
    clock — внедряемые монотонные часы (для проверки stale в тестах).
    """

    def __init__(self, obs, *, scene_nums=None, mic: str = MIC, sys: str = SYS,
                 stale_after: float = 5.0, meter_interval: float = 0.2,
                 refresh_interval: float = 5.0, clock=time.monotonic):
        self.obs = obs
        self.scene_nums = dict(scene_nums) if scene_nums is not None else dict(SCENE_NUMS)
        self.mic = mic
        self.sys = sys
        self.stale_after = float(stale_after)
        self.meter_interval = float(meter_interval)
        self.refresh_interval = float(refresh_interval)
        self.clock = clock
        self._snap = self._empty()
        self._updated_at = self.clock()

    # ── чтение состояния ──
    @property
    def state(self) -> dict:
        """Копия последнего снапшота; stale пересчитывается на момент чтения."""
        snap = copy.deepcopy(self._snap)
        snap["stale"] = self.is_stale()
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

    async def poll_meters(self) -> None:
        """Один опрос уровней звука; при офлайне тихо выходит, не трогая OBS."""
        if not self.obs.connected:
            return
        try:
            data = await self.obs.req("GetInputVolumeMeters")
        except Exception:
            return
        levels = self._parse_meters(data)
        audio = dict(self._snap.get("audio") or {})
        for key, name in (("mic", self.mic), ("sys", self.sys)):
            channel = dict(audio.get(key) or {"mute": None, "vol": None, "level": None})
            channel["level"] = levels.get(name)
            audio[key] = channel
        self._snap = {**self._snap, "audio": audio}

    async def run(self) -> None:
        """Два независимых цикла: метры и полный рефреш; отменяются чисто."""
        tasks = [asyncio.create_task(self._tick(self.meter_interval, self.poll_meters)),
                 asyncio.create_task(self._tick(self.refresh_interval, self.refresh))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
            return {"active": False, "time": "", "reconnect": False}
        return {"active": bool(data.get("outputActive")),
                "time": _timecode(data.get("outputTimecode")),
                "reconnect": bool(data.get("outputReconnecting"))}

    async def _record(self) -> dict:
        try:
            data = await self.obs.req("GetRecordStatus")
        except Exception:
            return {"active": False, "time": ""}
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
        try:
            muted = await self.obs.req("GetInputMute", inputName=name)
            volume = await self.obs.req("GetInputVolume", inputName=name)
        except Exception:
            return {"mute": None, "vol": None, "level": prev_level}
        return {"mute": bool(muted.get("inputMuted")),
                "vol": _percent(volume.get("inputVolumeMul")),
                "level": prev_level}

    @staticmethod
    def _parse_meters(data: dict) -> dict:
        """GetInputVolumeMeters -> {inputName: уровень}. Берём последний сэмпл
        каждого канала и максимум по каналам (может превышать 1 при клиппинге)."""
        levels: dict[str, float | None] = {}
        for item in data.get("inputs") or []:
            samples = [channel[-1]
                       for channel in (item.get("inputLevelsMul") or []) if channel]
            levels[item.get("inputName")] = max(samples) if samples else None
        return levels

    def _sort_scenes(self, names: list[str]) -> list[dict]:
        """Сцены по номеру numpad; сцены без маппинга — в конце с num=None."""

        def key(name: str):
            num = self.scene_nums.get(name)
            return (num is None, num if num is not None else 0, name)

        return [{"name": name, "num": self.scene_nums.get(name)}
                for name in sorted(names, key=key)]

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
            "stream": {"active": False, "time": "", "reconnect": False},
            "record": {"active": False, "time": ""},
            "replay": False,
            "audio": {"mic": {"mute": None, "vol": None, "level": None},
                      "sys": {"mute": None, "vol": None, "level": None}},
            "stale": True,
            "updated_at": 0.0,
        }
