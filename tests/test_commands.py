"""Слой команд пульта: валидация, отказы 400/409, выполнение и ресинк.

Живого OBS и сети здесь нет: obs — стаб FakeObs, поверх него работает
настоящий Panel. Каждое ожидание обёрнуто в asyncio.wait_for, чтобы тест не
мог зависнуть.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from server.commands import CommandError, Commands, RefusedError
from server.panel import MIC, SYS, Panel
from tests.fakes import FakeObs

pytestmark = pytest.mark.unit

TIMEOUT = 1.0


class Clock:
    """Внедряемые часы: время двигается вручную, чтобы проверять stale."""

    def __init__(self, t: float = 1000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


async def run(coro):
    """Ждём корутину с таймаутом: тест не должен виснуть."""
    return await asyncio.wait_for(coro, timeout=TIMEOUT)


def full_responses(*, scene: str = "Dota 2", scenes=("Dota 2", "WoW"),
                   stream: bool = False, record: bool = False, replay: bool = False,
                   muted: bool = False, volume: float = 0.5) -> dict:
    """Правдоподобные ответы OBS на полный опрос Panel."""
    return {
        "GetSceneList": {"currentProgramSceneName": scene,
                         "scenes": [{"sceneName": n} for n in scenes]},
        "GetStreamStatus": {"outputActive": stream, "outputTimecode": "00:00:00.000",
                            "outputReconnecting": False},
        "GetRecordStatus": {"outputActive": record, "outputTimecode": "00:00:00.000"},
        "GetReplayBufferStatus": {"outputActive": replay},
        "GetInputMute": lambda inputName: {"inputMuted": muted},
        "GetInputVolume": lambda inputName: {"inputVolumeMul": volume},
    }


async def rig(*, scene: str = "Dota 2", scenes=("Dota 2", "WoW"), stream: bool = False,
              record: bool = False, replay: bool = False, muted: bool = False,
              volume: float = 0.5, connected: bool = True, failures=None, clock=None,
              refresh: bool = True, **panel_kwargs):
    """FakeObs + настоящий Panel + Commands; сценарий задаётся ответами OBS."""
    responses = full_responses(scene=scene, scenes=scenes, stream=stream, record=record,
                               replay=replay, muted=muted, volume=volume)
    fake = FakeObs(responses=responses, connected=connected, failures=failures)
    panel = Panel(fake, clock=clock or Clock(), **panel_kwargs)
    cmds = Commands(fake, panel)
    if refresh:
        await run(panel.refresh())
    return fake, panel, cmds


# ── 1. неизвестная команда ──

async def test_01_unknown_command_is_400_without_obs_calls():
    fake, _, cmds = await rig(refresh=False)
    with pytest.raises(CommandError) as ei:
        await run(cmds.execute("такой.команды.нет"))
    assert ei.value.code == 400
    assert fake.calls == []


# ── 2. подтверждение транспорта ──

async def test_02_stream_start_without_confirm_is_409_without_obs_calls():
    fake, _, cmds = await rig()
    with pytest.raises(RefusedError) as ei:
        await run(cmds.execute("stream.start"))
    assert ei.value.code == 409
    assert isinstance(ei.value, CommandError)
    assert "StartStream" not in [r for r, _ in fake.calls]


# ── 3. старт эфира ──

async def test_03_stream_start_sends_startstream_once_and_refreshes():
    fake, _, cmds = await rig(stream=False)
    res = await run(cmds.execute("stream.start", {"confirm": True}))

    assert res["ok"] is True
    assert res["changed"] == ["stream"]
    assert res["error"] is None
    assert fake.called("StartStream") == [{}]  # у StartStream в v5 нет аргументов


# ── 4. старт при идущем эфире — не ошибка ──

async def test_04_stream_start_when_already_live_is_noop():
    fake, _, cmds = await rig(stream=True)
    before = len(fake.calls)
    res = await run(cmds.execute("stream.start", {"confirm": True}))

    assert res["ok"] is True
    assert res["changed"] == []
    assert fake.called("StartStream") == []
    assert len(fake.calls) == before  # к OBS не ходили и не рефрешили
    assert res["state"]["stream"]["active"] is True


# ── 5. стоп при остановленном эфире — не ошибка ──

async def test_05_stream_stop_when_stopped_is_noop():
    fake, _, cmds = await rig(stream=False)
    before = len(fake.calls)
    res = await run(cmds.execute("stream.stop", {"confirm": True}))

    assert res["ok"] is True
    assert res["changed"] == []
    assert fake.called("StopStream") == []
    assert len(fake.calls) == before


# ── 6. запись — одной кнопкой, сторона из состояния OBS ──

async def test_06_record_toggle_follows_obs_state():
    fake, _, cmds = await rig(record=False)
    res = await run(cmds.execute("record.toggle"))
    assert res["ok"] is True
    assert res["changed"] == ["record"]
    assert len(fake.called("StartRecord")) == 1
    assert fake.called("StopRecord") == []

    fake2, _, cmds2 = await rig(record=True)
    res2 = await run(cmds2.execute("record.toggle"))
    assert res2["ok"] is True
    assert len(fake2.called("StopRecord")) == 1
    assert fake2.called("StartRecord") == []


# ── 7. stale гасит разрушительные, но не безопасные ──

async def test_07_stale_refuses_destructive_but_allows_safe():
    clock = Clock(1000.0)
    fake, _, cmds = await rig(clock=clock)
    clock.t += cmds.panel.stale_after + 1.0  # заведомо больше порога устаревания
    fake.calls.clear()

    with pytest.raises(RefusedError):
        await run(cmds.execute("record.toggle"))
    assert fake.calls == []  # отказали до обращения к OBS

    res = await run(cmds.execute("audio.toggleMute", {"track": "mic"}))
    assert res["ok"] is True

    clock.t += cmds.panel.stale_after + 1.0  # успешная команда отрефрешила; снова устареваем
    res2 = await run(cmds.execute("scene.set", {"name": "WoW"}))
    assert res2["ok"] is True

    clock.t += cmds.panel.stale_after + 1.0
    with pytest.raises(RefusedError):
        await run(cmds.execute("stream.start", {"confirm": True}))  # stale гасит и его


# ── 8. повтор доступен только при включённом буфере ──

async def test_08_replay_save_requires_enabled_buffer():
    fake, _, cmds = await rig(replay=False)
    with pytest.raises(RefusedError):
        await run(cmds.execute("replay.save"))
    assert fake.called("SaveReplayBuffer") == []

    fake2, _, cmds2 = await rig(replay=True)
    res = await run(cmds2.execute("replay.save"))
    assert res["ok"] is True
    assert res["changed"] == ["replay"]
    assert len(fake2.called("SaveReplayBuffer")) == 1


# ── 9. mute инвертируется относительно OBS ──

async def test_09_toggle_mute_inverts_obs_state():
    fake, _, cmds = await rig(muted=False)
    res = await run(cmds.execute("audio.toggleMute", {"track": "mic"}))
    assert res["ok"] is True
    assert res["changed"] == ["audio.mic"]
    assert fake.called("SetInputMute") == [{"inputName": MIC, "inputMuted": True}]

    fake2, _, cmds2 = await rig(muted=True)
    res2 = await run(cmds2.execute("audio.toggleMute", {"track": "sys"}))
    assert res2["ok"] is True
    assert res2["changed"] == ["audio.sys"]
    assert fake2.called("SetInputMute") == [{"inputName": SYS, "inputMuted": False}]


# ── 10. валидация аргументов звука и перевод процентов ──

async def test_10_audio_argument_validation_and_volume():
    fake, _, cmds = await rig()

    with pytest.raises(CommandError):
        await run(cmds.execute("audio.toggleMute", {"track": "нет такого"}))
    with pytest.raises(CommandError):
        await run(cmds.execute("audio.setVolume", {"track": "mic", "value": 250}))
    with pytest.raises(CommandError):
        await run(cmds.execute("audio.setVolume", {"track": "mic", "value": "громко"}))
    assert fake.called("SetInputVolume") == []  # отказы не дошли до OBS

    res = await run(cmds.execute("audio.setVolume", {"track": "mic", "value": 42}))
    assert res["ok"] is True
    calls = fake.called("SetInputVolume")
    assert len(calls) == 1
    assert calls[0]["inputName"] == MIC
    assert calls[0]["inputVolumeMul"] == pytest.approx(0.42)


# ── 11. паника: все шаги, частичный успех не откатывается ──

async def test_11_panic_runs_all_steps_and_reports_partial_failure():
    fake, _, cmds = await rig()
    res = await run(cmds.execute("panic"))

    assert res["ok"] is True
    assert res["changed"] == ["panic"]
    assert res["steps"] == {"scene": True, "mic": True, "sys": True}
    assert res["state"]["obs"] is True  # свежий снапшот после ресинка
    assert len(fake.called("SetCurrentProgramScene")) == 1
    assert len(fake.called("SetInputMute")) == 2

    fake2, _, cmds2 = await rig(failures={"SetCurrentProgramScene"})
    res2 = await run(cmds2.execute("panic"))
    assert res2["ok"] is False
    assert isinstance(res2["steps"]["scene"], str) and res2["steps"]["scene"]
    assert res2["steps"]["mic"] is True
    assert res2["steps"]["sys"] is True
    assert res2["error"]
    assert len(fake2.called("SetInputMute")) == 2  # успешные шаги не откатились


# ── 12. офлайн: любая команда отвергается без запросов ──

async def test_12_offline_refuses_every_command_without_calls():
    fake = FakeObs(connected=False)
    panel = Panel(fake, clock=Clock())
    cmds = Commands(fake, panel)

    for name, args in (("scene.set", {"name": "Dota 2"}),
                       ("stream.start", {"confirm": True}),
                       ("record.toggle", None)):
        with pytest.raises(RefusedError):
            await run(cmds.execute(name, args))
    assert fake.calls == []


# ── 13. state — результат свежего panel.refresh() ──

async def test_13_state_is_fresh_refresh_result():
    obs_state = {"scene": "Dota 2", "scenes": ["Dota 2", "WoW"]}

    def get_scenes():
        return {"currentProgramSceneName": obs_state["scene"],
                "scenes": [{"sceneName": n} for n in obs_state["scenes"]]}

    def set_scene(sceneName):
        obs_state["scene"] = sceneName
        return {}

    fake = FakeObs(responses={"GetSceneList": get_scenes,
                              "SetCurrentProgramScene": set_scene})
    panel = Panel(fake, clock=Clock())
    cmds = Commands(fake, panel)
    await run(panel.refresh())
    assert panel.state["scene"] == "Dota 2"

    res = await run(cmds.execute("scene.set", {"name": "WoW"}))
    assert res["ok"] is True
    assert res["state"]["scene"] == "WoW"
    assert res["state"] == panel.state


# ── 14. в модуле нет сетевых зависимостей ──

def test_14_commands_module_has_no_network_imports():
    import server.commands as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    for banned in ("import websockets", "from websockets", "import aiohttp",
                   "from aiohttp", "import socket", "from socket"):
        assert banned not in src
