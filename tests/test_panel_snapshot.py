"""Снапшот состояния Panel: сцены, транспорт, звук, stale — только через FakeObs.

Живого OBS и сети здесь нет по определению: obs — стаб FakeObs с async req().
"""
from __future__ import annotations

import pathlib

import pytest

from server.panel import MIC, SYS, Panel
from tests.fakes import FakeObs

pytestmark = pytest.mark.unit


class Clock:
    """Внедряемые часы: t двигается вручную, чтобы проверять stale."""

    def __init__(self, t: float = 1000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def panel_for(fake: FakeObs, **kwargs) -> Panel:
    return Panel(fake, clock=Clock(), **kwargs)


def scene_list(current: str | None, *names: str) -> dict:
    return {"currentProgramSceneName": current,
            "scenes": [{"sceneName": n} for n in names]}


# ── 1. сортировка сцен и активная сцена ──

async def test_scenes_sorted_by_numpad_and_unmapped_last():
    fake = FakeObs(responses={"GetSceneList": scene_list(
        "WoW", "Чёрный экран", "Dota 2", "Неизвестная сцена", "WoW")})
    snap = await panel_for(fake).refresh()

    assert [s["name"] for s in snap["scenes"]] == [
        "Dota 2", "WoW", "Чёрный экран", "Неизвестная сцена"]
    assert [s["num"] for s in snap["scenes"]] == [1, 3, 9, None]
    assert snap["scene"] == "WoW"
    assert snap["obs"] is True


async def test_active_scene_missing_from_list_is_allowed():
    fake = FakeObs(responses={"GetSceneList": scene_list("Сцена из другой коллекции", "Dota 2")})
    snap = await panel_for(fake).refresh()

    assert snap["scene"] == "Сцена из другой коллекции"
    assert [s["name"] for s in snap["scenes"]] == ["Dota 2"]
    assert snap["obs"] is True


# ── 2. таймкоды и громкость ──

async def test_timecodes_truncated_and_volume_as_percent():
    fake = FakeObs(responses={
        "GetSceneList": scene_list("Dota 2", "Dota 2"),
        "GetStreamStatus": {"outputActive": True, "outputTimecode": "01:02:03.456",
                            "outputReconnecting": True},
        "GetRecordStatus": {"outputActive": True, "outputTimecode": "00:00:07.890"},
        "GetInputMute": lambda inputName: {"inputMuted": False},
        "GetInputVolume": lambda inputName: {"inputVolumeMul": 0.5 if inputName == MIC else 0.25},
    })
    snap = await panel_for(fake).refresh()

    assert snap["stream"]["time"] == "01:02:03.45"
    assert len(snap["stream"]["time"]) == 11
    assert snap["stream"]["reconnect"] is True
    assert snap["record"]["time"] == "00:00:07.89"
    assert snap["audio"]["mic"]["vol"] == 50
    assert snap["audio"]["sys"]["vol"] == 25


# ── 3. отказ буфера повтора не роняет снапшот ──

async def test_replay_refusal_keeps_rest_of_snapshot():
    fake = FakeObs(
        responses={"GetSceneList": scene_list("Dota 2", "Dota 2"),
                   "GetStreamStatus": {"outputActive": True, "outputTimecode": "00:00:01.000"},
                   "GetInputMute": lambda inputName: {"inputMuted": False},
                   "GetInputVolume": lambda inputName: {"inputVolumeMul": 0.5}},
        failures={"GetReplayBufferStatus"})
    snap = await panel_for(fake).refresh()

    assert snap["replay"] is False
    assert snap["obs"] is True
    assert [s["name"] for s in snap["scenes"]] == ["Dota 2"]
    assert snap["stream"]["active"] is True
    assert snap["audio"]["mic"]["vol"] == 50


# ── 3б. отказ транспорта не выдумывает «эфира нет» ──

async def test_transport_refusal_keeps_last_known_state():
    """Кнопка «Стоп эфира» не должна блокироваться из-за одного отказа запроса."""
    fake = FakeObs(responses={
        "GetSceneList": scene_list("Dota 2", "Dota 2"),
        "GetStreamStatus": {"outputActive": True, "outputTimecode": "00:10:00.000",
                            "outputReconnecting": True},
        "GetRecordStatus": {"outputActive": True, "outputTimecode": "00:01:00.000"},
    })
    panel = panel_for(fake)
    fresh = await panel.refresh()
    assert fresh["stream"]["active"] is True
    assert fresh["record"]["active"] is True

    fake.failures.add("GetStreamStatus")
    fake.failures.add("GetRecordStatus")
    snap = await panel.refresh()

    assert snap["obs"] is True
    assert snap["stream"]["active"] is True
    assert snap["stream"]["time"] == "00:10:00.00"
    assert snap["stream"]["reconnect"] is True
    assert snap["record"]["active"] is True
    assert snap["record"]["time"] == "00:01:00.00"


async def test_transport_refusal_on_first_refresh_is_empty_state():
    """Первый же опрос отказал — показываем «не идёт», а не выдуманное время."""
    fake = FakeObs(responses={"GetSceneList": scene_list("Dota 2", "Dota 2")},
                   failures={"GetStreamStatus", "GetRecordStatus"})
    snap = await panel_for(fake).refresh()

    assert snap["obs"] is True
    assert snap["stream"] == {"active": False, "time": "", "reconnect": False}
    assert snap["record"] == {"active": False, "time": ""}


# ── 4. отказ одного аудио-источника ──

async def test_audio_source_refusal_nulls_only_that_channel():
    fake = FakeObs(
        responses={"GetSceneList": scene_list("Dota 2", "Dota 2"),
                   "GetInputMute": lambda inputName: {"inputMuted": inputName == SYS},
                   "GetInputVolume": lambda inputName: {"inputVolumeMul": 0.5}},
        failures={("GetInputVolume", MIC)})
    snap = await panel_for(fake).refresh()

    assert snap["obs"] is True
    assert snap["audio"]["mic"]["mute"] is None
    assert snap["audio"]["mic"]["vol"] is None
    assert snap["audio"]["sys"]["mute"] is True
    assert snap["audio"]["sys"]["vol"] == 50


# ── 5. отказ списка сцен = офлайн с текстом ошибки ──

async def test_scene_list_refusal_marks_offline_with_error():
    fake = FakeObs(failures={"GetSceneList"})
    snap = await panel_for(fake).refresh()

    assert snap["obs"] is False
    assert snap["error"] and "GetSceneList" in snap["error"]


# ── 6. офлайн сохраняет последние сцены ──

async def test_offline_keeps_last_known_scenes_and_marks_stale():
    fake = FakeObs(responses={"GetSceneList": scene_list("WoW", "Dota 2", "WoW")})
    panel = panel_for(fake)
    await panel.refresh()
    assert [s["name"] for s in panel.state["scenes"]] == ["Dota 2", "WoW"]

    fake.connected = False
    snap = await panel.refresh()

    assert snap["obs"] is False
    assert snap["stale"] is True
    assert [s["name"] for s in snap["scenes"]] == ["Dota 2", "WoW"]
    assert snap["scene"] == "WoW"


# ── 7. stale пересчитывается на чтение, а не замораживается ──

async def test_stale_recalculated_on_read():
    clock = Clock(100.0)
    fake = FakeObs(responses={"GetSceneList": scene_list("Dota 2", "Dota 2")})
    panel = Panel(fake, clock=clock, stale_after=5.0)
    await panel.refresh()

    assert panel.state["stale"] is False
    assert panel.is_stale() is False

    clock.t += 5.1
    assert panel.state["stale"] is True
    assert panel.is_stale() is True


# ── 8. уровни звука ──

async def test_poll_meters_sets_levels_and_offline_is_noop():
    fake = FakeObs(responses={"GetInputVolumeMeters": {"inputs": [
        {"inputName": MIC, "inputLevelsMul": [[0.1, 0.5], [0.2, 0.8]]},
        {"inputName": SYS, "inputLevelsMul": [[0.3, 0.9]]}]}})
    panel = panel_for(fake)

    assert panel.state["audio"]["mic"]["level"] is None
    await panel.poll_meters()
    snap = panel.state
    assert snap["audio"]["mic"]["level"] == 0.8
    assert snap["audio"]["sys"]["level"] == 0.9

    offline = FakeObs(connected=False)
    panel2 = panel_for(offline)
    await panel2.poll_meters()
    assert offline.calls == []
    assert panel2.state["audio"]["mic"]["level"] is None


async def test_poll_meters_clipping_over_one_and_missing_source():
    fake = FakeObs(responses={"GetInputVolumeMeters": {"inputs": [
        {"inputName": MIC, "inputLevelsMul": [[1.2]]}]}})
    panel = panel_for(fake)
    await panel.poll_meters()

    assert panel.state["audio"]["mic"]["level"] == 1.2
    assert panel.state["audio"]["sys"]["level"] is None


# ── state отдаёт копию, а не внутренний снапшот ──

async def test_state_returns_copy():
    fake = FakeObs(responses={"GetSceneList": scene_list("Dota 2", "Dota 2")})
    panel = panel_for(fake)
    await panel.refresh()

    snap = panel.state
    snap["scenes"].append({"name": "Хак", "num": None})
    snap["obs"] = False

    again = panel.state
    assert [s["name"] for s in again["scenes"]] == ["Dota 2"]
    assert again["obs"] is True


# ── 10. отсутствие сети в самом модуле ──

def test_panel_module_has_no_network_imports():
    import server.panel as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    for banned in ("import websockets", "from websockets", "import aiohttp",
                   "from aiohttp", "import socket", "from socket"):
        assert banned not in src
