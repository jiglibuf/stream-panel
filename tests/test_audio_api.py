"""Ручки звука HTTP-слоя: авторизация, конверт, коды 400/409/500 (integration).

Живого OBS и системы нет: Panel крутится на FakeObs, системный провайдер —
FakeAudioOps. Проверяются ровно маршруты, доступ и форма ответов.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from server.app import make_guard
from server.audio_auto import AutoSwitch, read_auto_flag
from server.audio_ops import AudioController
from server.commands import Commands
from server.panel import Panel
from tests.fakes import FakeAudioOps, FakeObs
from tests.http_support import RESPONSES, TOKEN, auth, make_stack, running

pytestmark = pytest.mark.integration

SPEAKERS = "alsa_output.pci-0000_00_1f.3.analog-stereo"
PHONES = "alsa_output.usb-Logitech_A20_X_0000000000000000-00.iec958-stereo"

AUDIO_KEYS = {"mode", "service", "default_sink", "chosen", "devices", "mixes",
              "obs", "issues", "updated_at"}
ENVELOPE_KEYS = {"ok", "name", "changed", "state", "steps", "error"}


def sinks(*names):
    return [{"name": n, "state": "IDLE", "description": n} for n in names]


async def make_audio_stack(*, ops_kw=None, streaming: bool = False, recording: bool = False,
                           auto_flag=None):
    """Стек пульта с фейковым системным провайдером звука."""
    responses = dict(RESPONSES)
    responses["GetStreamStatus"] = {"outputActive": streaming,
                                    "outputTimecode": "00:00:00.000",
                                    "outputReconnecting": False}
    responses["GetRecordStatus"] = {"outputActive": recording,
                                    "outputTimecode": "00:00:00.000"}
    obs = FakeObs(responses=responses)
    panel = Panel(obs, refresh_interval=5.0, meter_stale_after=5.0)
    commands = Commands(obs, panel)
    kwargs = dict(sinks=sinks(SPEAKERS), default_sink=SPEAKERS, device=SPEAKERS)
    kwargs.update(ops_kw or {})
    ops = FakeAudioOps(**kwargs)
    audio = AudioController(ops, panel)
    from server.app import create_app

    app = create_app(obs=obs, panel=panel, commands=commands, token=TOKEN,
                     push_interval=0.05, audio=audio,
                     audio_auto=AutoSwitch(flag_path=auto_flag) if auto_flag else None)
    await panel.refresh()
    return app, obs, panel, ops, audio


# ── 1. доступ ──

async def test_audio_get_requires_token():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        assert (await cl.get("/api/audio")).status == 401
        assert (await cl.get("/api/audio", headers=auth("не-тот"))).status == 401


async def test_audio_mode_requires_token():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        assert (await cl.post("/api/audio/mode", json={"mode": "stream"})).status == 401


async def test_audio_outside_allowlist_403_even_with_token():
    guard = make_guard(TOKEN)
    req = make_mocked_request("GET", "/api/audio", headers=auth()).clone(remote="8.8.8.8")

    async def handler(request):
        return web.Response(text="ok")

    assert (await guard(req, handler)).status == 403


# ── 2. GET /api/audio ──

async def test_get_audio_returns_snapshot():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        resp = await cl.get("/api/audio", headers=auth())
        assert resp.status == 200
        snap = await resp.json()
        assert AUDIO_KEYS <= set(snap)
        assert snap["mode"] == "normal"
        assert snap["chosen"] == SPEAKERS


# ── 3. POST /api/audio/mode ──

async def test_mode_switch_returns_envelope():
    app, obs, panel, ops, audio = await make_audio_stack()
    async with running(app) as cl:
        resp = await cl.post("/api/audio/mode", json={"mode": "stream"}, headers=auth())
        assert resp.status == 200
        env = await resp.json()
        assert ENVELOPE_KEYS <= set(env)
        assert env["ok"] is True and env["name"] == "audio.mode"
        assert env["error"] is None
        assert env["state"]["mode"] == "stream"
        assert ops.enabled


async def test_mode_switch_idempotent_is_noop_ok():
    app, *_ = await make_audio_stack(ops_kw={"enabled": True, "active": True})
    async with running(app) as cl:
        resp = await cl.post("/api/audio/mode", json={"mode": "stream"}, headers=auth())
        assert resp.status == 200
        env = await resp.json()
        assert env["ok"] is True and env["changed"] == [] and env["steps"] == []


async def test_mode_unknown_is_400():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        resp = await cl.post("/api/audio/mode", json={"mode": "нет-такого"}, headers=auth())
        assert resp.status == 400
        assert (await resp.json())["ok"] is False


async def test_mode_live_without_confirm_is_409():
    app, *_ = await make_audio_stack(streaming=True)
    async with running(app) as cl:
        resp = await cl.post("/api/audio/mode", json={"mode": "stream"}, headers=auth())
        assert resp.status == 409
        env = await resp.json()
        assert env["ok"] is False and "эфир" in env["error"]


async def test_mode_live_with_confirm_succeeds():
    app, obs, panel, ops, audio = await make_audio_stack(streaming=True)
    async with running(app) as cl:
        resp = await cl.post("/api/audio/mode",
                             json={"mode": "stream", "confirm": True}, headers=auth())
        assert resp.status == 200
        assert (await resp.json())["ok"] is True


async def test_mode_step_failure_is_500_with_steps():
    app, *_ = await make_audio_stack(ops_kw={"failures": {"create_sink"}})
    async with running(app) as cl:
        resp = await cl.post("/api/audio/mode", json={"mode": "stream"}, headers=auth())
        assert resp.status == 500
        env = await resp.json()
        assert env["ok"] is False and env["error"]
        assert isinstance(env["steps"], list)


async def test_mode_garbage_bodies_400_not_500():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        assert (await cl.post("/api/audio/mode", json={}, headers=auth())).status == 400
        assert (await cl.post("/api/audio/mode", json={"mode": 123},
                              headers=auth())).status == 400
        assert (await cl.post("/api/audio/mode",
                              json={"mode": "stream", "confirm": "да"},
                              headers=auth())).status == 400
        resp = await cl.post("/api/audio/mode", data="это не json",
                             headers={**auth(), "Content-Type": "application/json"})
        assert resp.status == 400


# ── 4. POST /api/audio/device ──

async def test_set_device_saves_choice_and_returns_envelope():
    app, obs, panel, ops, audio = await make_audio_stack(
        ops_kw={"sinks": sinks(SPEAKERS, PHONES)})
    async with running(app) as cl:
        resp = await cl.post("/api/audio/device", json={"name": PHONES}, headers=auth())
        assert resp.status == 200
        env = await resp.json()
        assert ENVELOPE_KEYS <= set(env)
        assert env["ok"] is True and env["name"] == "audio.device"
        assert ops.device == PHONES


async def test_set_device_unknown_is_400():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        resp = await cl.post("/api/audio/device", json={"name": "alsa_output.нет"},
                             headers=auth())
        assert resp.status == 400
        assert (await resp.json())["ok"] is False


async def test_set_device_virtual_is_400():
    app, *_ = await make_audio_stack(ops_kw={"sinks": sinks(SPEAKERS, "rz_mix")})
    async with running(app) as cl:
        resp = await cl.post("/api/audio/device", json={"name": "rz_mix"}, headers=auth())
        assert resp.status == 400


async def test_set_device_garbage_body_400():
    app, *_ = await make_audio_stack()
    async with running(app) as cl:
        assert (await cl.post("/api/audio/device", json={}, headers=auth())).status == 400
        assert (await cl.post("/api/audio/device", json={"name": 5},
                              headers=auth())).status == 400


# ── 4. POST /api/audio/auto ──

async def test_auto_toggle_switches_and_is_remembered_in_file(tmp_path):
    """Тумблер автоматики: переключается и запоминается в файле рядом с настройками."""
    flag = tmp_path / "rz_mix.auto"
    app, *_ = await make_audio_stack(auto_flag=flag)
    async with running(app) as cl:
        snap = await (await cl.get("/api/audio", headers=auth())).json()
        assert snap["auto"] == {"enabled": True}            # по умолчанию включена

        resp = await cl.post("/api/audio/auto", headers=auth(), json={"enabled": False})
        assert resp.status == 200
        envelope = await resp.json()
        assert envelope["ok"] is True and envelope["changed"] == ["auto"]
        assert envelope["state"]["auto"] == {"enabled": False}
        assert flag.read_text(encoding="utf-8").strip() == "off"

        # Решение переживает перезапуск пульта: при старте читается тот же файл.
        assert read_auto_flag(flag) is False


async def test_auto_toggle_requires_token_and_boolean(tmp_path):
    app, *_ = await make_audio_stack(auto_flag=tmp_path / "rz_mix.auto")
    async with running(app) as cl:
        assert (await cl.post("/api/audio/auto", json={"enabled": True})).status == 401
        assert (await cl.post("/api/audio/auto", headers=auth(),
                              json={"enabled": "да"})).status == 400


# ── 5. без звукового контроллера ручек нет ──

async def test_audio_routes_absent_without_controller():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        assert (await cl.get("/api/audio", headers=auth())).status == 404
