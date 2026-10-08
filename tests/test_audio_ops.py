"""Шаги включения/выключения звука и выбор устройства — на подставном провайдере.

Живого pactl/PipeWire/systemctl/OBS здесь нет: FakeAudioOps держит состояние в
памяти, поэтому проверяются ровно шаги, идемпотентность, отказ 409 и конверт.
"""
from __future__ import annotations

import asyncio

import pytest

from server.audio import NORMAL, STREAM
from server.audio_ops import AudioController, CommandError, RefusedError
from tests.fakes import FakeAudioOps

pytestmark = pytest.mark.unit

TIMEOUT = 1.0

PHONES = "alsa_output.usb-Logitech_A20_X_0000000000000000-00.iec958-stereo"
SPEAKERS = "alsa_output.pci-0000_00_1f.3.analog-stereo"


def run(coro):
    """Ждём корутину с таймаутом: тест не должен виснуть."""
    return asyncio.wait_for(coro, timeout=TIMEOUT)


def sinks(*names):
    return [{"name": n, "state": "IDLE", "description": f"Описание {n}"} for n in names]


class StubPanel:
    """Панель для чтения obs/stream/record — провайдера звука не касается."""

    def __init__(self, *, obs: bool = True, streaming: bool = False, recording: bool = False):
        self.state = {"obs": obs,
                      "stream": {"active": streaming},
                      "record": {"active": recording}}


def step_keys(envelope):
    """Ключи шагов из human-строк steps (до «:»)."""
    return [s.split(":", 1)[0] for s in envelope["steps"]]


# ── 1. снапшот: форма и виды устройств ──

async def test_snapshot_shape_and_device_kinds():
    ops = FakeAudioOps(
        sinks=sinks(SPEAKERS, PHONES, "rz_mix", "game_mix", "discord_mix", "easyeffects_sink"),
        default_sink="rz_mix", device=PHONES, enabled=True, active=True,
        loops=[{"source": "rz_mix.monitor", "targets": [PHONES]}])
    snap = AudioController(ops).snapshot()

    assert snap["mode"] == STREAM
    assert snap["service"] == {"enabled": True, "active": True}
    assert snap["default_sink"] == "rz_mix"
    assert snap["chosen"] == PHONES
    assert {"mode", "service", "default_sink", "chosen", "devices", "mixes",
            "obs", "issues", "updated_at"} <= set(snap)
    assert snap["mixes"] == {"present": True,
                             "loops": [{"source": "rz_mix.monitor", "targets": [PHONES]}]}

    by_name = {d["name"]: d for d in snap["devices"]}
    assert by_name[PHONES]["kind"] == "physical"
    assert by_name["rz_mix"]["kind"] == "virtual"
    assert by_name[PHONES]["is_chosen"] is True
    assert by_name["rz_mix"]["is_default"] is True
    assert by_name[PHONES]["label"] == f"Описание {PHONES}"


async def test_snapshot_issues_report_broken_setup():
    ops = FakeAudioOps(
        sinks=sinks(PHONES, SPEAKERS, "rz_mix"),
        default_sink=PHONES, device=PHONES, enabled=True, active=False,
        loops=[{"source": "rz_mix.monitor", "targets": [SPEAKERS, PHONES]}])
    snap = AudioController(ops).snapshot()

    assert snap["mode"] == STREAM
    assert any("два устройства" in i for i in snap["issues"])
    assert any("сторож не работает" in i for i in snap["issues"])


# ── 2. включение стрима ──

async def test_enable_stream_runs_steps_in_order_with_side_effects():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS), default_sink=SPEAKERS, device="")
    env = await run(AudioController(ops).set_mode("stream"))

    assert env["ok"] is True and env["error"] is None
    assert env["name"] == "audio.mode"
    assert step_keys(env) == ["device", "sinks", "loops", "default", "obs", "watchdog", "state"]
    assert env["state"]["mode"] == STREAM

    # выходы созданы, выход по умолчанию — rz_mix, сторож включён
    assert {"rz_mix", "game_mix", "discord_mix"} <= {s["name"] for s in ops.sinks}
    assert ops.default == "rz_mix"
    assert ops.enabled and ops.active
    # петли идут в выбранное устройство (A20 нет — первый физический = колонки)
    assert {l["source"] for l in ops.loops} == {
        "rz_mix.monitor", "game_mix.monitor", "discord_mix.monitor"}
    assert all(l["targets"] == [SPEAKERS] for l in ops.loops)
    # захваты OBS наведены, состояние записано
    assert ops.inputs.get("Звук системы") == "game_mix.monitor"
    assert ops.inputs.get("Дискорд") == "discord_mix.monitor"
    assert ops.state and ops.state["headphones"] == SPEAKERS


def test_loops_read_from_module_args_not_from_processing_hops():
    """Петля объявляет себя аргументами модуля (source/sink) — это и есть её цель.

    Связи портов дополнительно показывают промежуточные узлы (easyeffects_sink),
    и раньше сервис принимал такую обработку за «звук ушёл в другое устройство»:
    в живом пульте это давало три ложные тревоги. Фикстуры сняты с живой системы (pw-dump + pactl list modules).
    """
    import json

    from server.audio_ops import SystemAudio

    a20 = "alsa_output.usb-Logitech_A20_X_-_USB_wired_mode_0000000000000000-00.analog-stereo"
    speakers = "alsa_output.pci-0000_00_1f.3.analog-stereo"
    modules = (
        "Module #30\n\tName: module-loopback\n"
        f"\tArgument: source=game_mix.monitor sink={a20} latency_msec=30"
        " source_dont_move=true sink_dont_move=true\n\tUsage counter: n/a\n"
        "Module #31\n\tName: module-loopback\n"
        f"\tArgument: source=discord_mix.monitor sink={a20} latency_msec=30"
        " source_dont_move=true sink_dont_move=true\n\tUsage counter: n/a\n"
    )

    def dump(extra=None):
        nodes = [("game_mix", 10), ("discord_mix", 11), ("output.loopback-1", 20),
                 ("output.loopback-2", 21), ("easyeffects_sink", 30), (a20, 40)]
        pairs = [(10, 20), (20, 40), (20, 30), (11, 21), (21, 40), (21, 30)]
        if extra:
            nodes.append((extra, 41))
            pairs.append((20, 41))
        data = [{"id": i, "type": "PipeWire:Interface:Node",
                 "info": {"props": {"node.name": n}}} for n, i in nodes]
        data += [{"id": 100 + k, "type": "PipeWire:Interface:Link",
                  "info": {"output-node-id": o, "input-node-id": i}}
                 for k, (o, i) in enumerate(pairs)]
        return data

    def provider(extra=None):
        def run(args, check=False):
            if args[0] == "pactl" and args[1] == "list" and args[2] == "modules":
                return modules
            if args[0] == "pw-dump":
                return json.dumps(dump(extra))
            return ""

        return SystemAudio(run=run)

    # обработка (easyeffects_sink) — не второе устройство: цель одна
    assert provider().list_loops() == [
        {"source": "game_mix.monitor", "targets": [a20]},
        {"source": "discord_mix.monitor", "targets": [a20]},
    ]

    # а «прилипшее» физическое устройство — настоящая проблема (звук в двух местах)
    from server.audio import collect_issues

    loops = provider(speakers).list_loops()
    assert loops[0]["targets"] == [a20, speakers]
    assert collect_issues("stream", {"enabled": True, "active": True}, loops, a20,
                          ["rz_mix", "game_mix", "discord_mix", a20, speakers])


def test_env_forces_c_locale_for_pactl():
    """pactl печатает поля на языке локали («Имя:», «Состояние:»), а разбор ждёт
    английские: без LC_ALL=C список устройств молча получается пустым — в живом
    сервисе вкладка «Звук» показывала «устройства не найдены», хотя pactl из
    терминала работал. Проверяем, что провайдер задаёт локаль сам.
    """
    from server.audio_ops import SystemAudio

    env = SystemAudio()._env()
    assert env.get("LC_ALL") == "C"
    assert env.get("LANG") == "C"
    assert env.get("XDG_RUNTIME_DIR")


def test_normal_mode_does_not_blame_easyeffects():
    """EasyEffects живёт в системе всегда и к миксам эфира не относится: его
    присутствие не должно выглядеть проблемой в обычном режиме (в живом снапшоте
    это давало ложное «остались виртуальные выходы»)."""
    from server.audio import NORMAL, collect_issues

    service = {"enabled": False, "active": False}
    assert collect_issues(NORMAL, service, [], "alsa_output.a",
                          ["easyeffects_sink", "alsa_output.a"]) == []
    # а наши собственные выходы в обычном режиме — настоящая проблема
    assert collect_issues(NORMAL, service, [], "alsa_output.a",
                          ["rz_mix", "alsa_output.a"])


async def test_repeat_stream_repairs_dead_watchdog():
    """Повторный тап «Стрим» лечит полуживой режим: сторож включён, но не работает.

    Идемпотентность — про ЗДОРОВЫЙ режим. Владелец жмёт ту же кнопку именно тогда,
    когда режим сломан, и молчаливый no-op выглядел бы как «кнопка не работает».
    """
    ops = FakeAudioOps(sinks=sinks(SPEAKERS, "rz_mix", "game_mix", "discord_mix"),
                       default_sink="rz_mix", device=SPEAKERS, enabled=True, active=False)
    env = await run(AudioController(ops).set_mode("stream"))

    assert env["ok"] is True
    assert step_keys(env) == ["device", "sinks", "loops", "default", "obs", "watchdog", "state"]
    assert ops.active is True            # сторож поднят
    assert "enable_service" in ops.mutations()


async def test_repeat_stream_repairs_loops_playing_into_two_devices():
    """Петли играют не туда (наш живой баг) — повторный «Стрим» это исправляет."""
    picked = "alsa_output.usb-Фирма_Монитор_1-00.analog-stereo"
    ops = FakeAudioOps(sinks=sinks(SPEAKERS, picked, "rz_mix", "game_mix", "discord_mix"),
                       default_sink="rz_mix", device=picked, enabled=True, active=True)
    ops.loops = [{"source": "game_mix.monitor", "targets": [SPEAKERS, picked]}]
    env = await run(AudioController(ops).set_mode("stream"))

    assert env["ok"] is True
    assert step_keys(env) != []           # не no-op: состояние чинили
    # петли переподключены на выбранное устройство (в фейке это create_loop/remove_loops/drop_loop_links)
    assert ops.mutations() & {"create_loop", "remove_loops", "drop_loop_links"}


async def test_enable_stream_is_idempotent_when_already_streaming():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS, "rz_mix"), default_sink="rz_mix",
                       enabled=True, active=True)
    env = await run(AudioController(ops).set_mode("stream"))

    assert env["ok"] is True
    assert env["changed"] == [] and env["steps"] == []
    assert ops.mutations() == set()  # к системе не ходили


async def test_enable_stream_offline_obs_does_not_fail_system_steps():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS), default_sink=SPEAKERS, obs_online=False)
    env = await run(AudioController(ops, StubPanel(obs=False)).set_mode("stream"))

    assert env["ok"] is True
    assert "obs" in step_keys(env)
    assert ops.enabled and ops.default == "rz_mix"  # системные шаги всё равно сделаны
    assert "set_input_device" not in ops.mutations()


# ── 3. выключение стрима ──

async def test_disable_stream_runs_steps_in_order_and_restores():
    ops = FakeAudioOps(
        sinks=sinks(SPEAKERS, "rz_mix", "game_mix", "discord_mix"),
        loops=[{"source": "rz_mix.monitor", "targets": [SPEAKERS]},
               {"source": "game_mix.monitor", "targets": [SPEAKERS]},
               {"source": "discord_mix.monitor", "targets": [SPEAKERS]}],
        streams=[{"id": "7", "sink": "rz_mix", "app": "dota"}],
        default_sink="rz_mix", device=SPEAKERS, enabled=True, active=True,
        state={"prev_default": SPEAKERS}, inputs={"Дискорд": "discord_mix.monitor"})
    env = await run(AudioController(ops).set_mode("normal"))

    assert env["ok"] is True and env["name"] == "audio.mode"
    assert step_keys(env) == ["watchdog", "default", "streams", "loops", "sinks", "obs", "state"]
    assert env["state"]["mode"] == NORMAL

    assert not ops.enabled and not ops.active            # сторож снят
    assert ops.default == SPEAKERS                       # выход по умолчанию — выбранное
    assert ops.streams[0]["sink"] == SPEAKERS            # поток уведён из rz_mix
    assert ops.loops == []                               # петли выгружены
    assert not ({"rz_mix", "game_mix", "discord_mix"} & {s["name"] for s in ops.sinks})
    assert ops.inputs.get("Звук системы") == f"{SPEAKERS}.monitor"
    assert "Дискорд" not in ops.inputs                   # источник удалён
    assert ops.state is None                             # состояние стёрто


async def test_disable_stream_is_idempotent_in_normal_mode():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS), default_sink=SPEAKERS, enabled=False, active=False)
    env = await run(AudioController(ops).set_mode("normal"))

    assert env["ok"] is True and env["steps"] == []
    assert ops.mutations() == set()


# ── 4. отказ 409 при эфире ──

async def test_mode_switch_refused_without_confirm_when_live():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS), default_sink=SPEAKERS)
    ctrl = AudioController(ops, StubPanel(streaming=True))
    with pytest.raises(RefusedError) as ei:
        await run(ctrl.set_mode("stream"))
    assert ei.value.code == 409
    assert "эфир" in str(ei.value)
    assert ops.mutations() == set()  # отказали до всякой системы


async def test_mode_switch_refused_when_recording():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS))
    with pytest.raises(RefusedError):
        await run(AudioController(ops, StubPanel(recording=True)).set_mode("stream"))


async def test_normal_switch_allowed_when_obs_offline_and_stream_flag_stale():
    """OBS закрыли, а в состоянии пульта остался старый «идёт эфир».

    Отказывать тут нельзя: эфира нет, прерывать нечего, а иначе владелец не вернёт
    себе звук — в живом пульте вышло ровно так: 409 «идёт эфир» при выключенном OBS,
    и миксы со сторожем продолжали работать.
    """
    ops = FakeAudioOps(sinks=sinks(SPEAKERS, "rz_mix", "game_mix", "discord_mix"),
                       default_sink="rz_mix", device=SPEAKERS, enabled=True, active=True,
                       obs_online=False)
    env = await run(AudioController(ops, StubPanel(obs=False, streaming=True)).set_mode("normal"))

    assert env["ok"] is True and env["error"] is None
    assert "watchdog" in env["changed"]
    assert ops.enabled is False and ops.active is False


async def test_mode_switch_proceeds_with_confirm_when_live():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS), default_sink=SPEAKERS)
    env = await run(AudioController(ops, StubPanel(streaming=True)).set_mode("stream", confirm=True))
    assert env["ok"] is True
    assert ops.enabled


async def test_unknown_mode_is_400():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS))
    with pytest.raises(CommandError) as ei:
        await run(AudioController(ops).set_mode("нет-такого"))
    assert ei.value.code == 400
    assert ops.mutations() == set()


# ── 5. сбой шага: 500-конверт с тем, что успело сделаться ──

async def test_failed_step_reports_error_and_steps_done():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS), default_sink=SPEAKERS,
                       failures={"create_sink"})
    env = await run(AudioController(ops).set_mode("stream"))

    assert env["ok"] is False
    assert env["error"].startswith("sinks")
    assert step_keys(env) == ["device"]  # шаг sinks сорвался, дальше не пошли
    assert env["state"]["mode"] in (STREAM, NORMAL)  # снапшот всё равно отдан


# ── 6. выбор устройства ──

async def test_set_device_validates_existence_and_kind():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS, "rz_mix"))
    ctrl = AudioController(ops)
    with pytest.raises(CommandError):
        await run(ctrl.set_device("alsa_output.нет-такого"))
    with pytest.raises(CommandError):
        await run(ctrl.set_device("rz_mix"))  # виртуальный нельзя
    assert ops.mutations() == set()


async def test_set_device_saves_choice_and_redirects_loops_in_stream():
    ops = FakeAudioOps(
        sinks=sinks(SPEAKERS, PHONES), enabled=True, active=True,
        loops=[{"source": "rz_mix.monitor", "targets": [SPEAKERS, PHONES]}])
    env = await run(AudioController(ops).set_device(PHONES))

    assert env["ok"] is True and env["name"] == "audio.device"
    assert ops.device == PHONES                 # выбор сохранён
    assert env["state"]["chosen"] == PHONES
    # связь со старым устройством снята (тот самый «звук в двух местах»)
    assert ops.loops[0]["targets"] == [PHONES]


async def test_set_device_in_normal_mode_does_not_touch_loops():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS, PHONES), enabled=False, active=False)
    env = await run(AudioController(ops).set_device(PHONES))
    assert env["ok"] is True
    assert ops.device == PHONES
    assert ops.calls.count(("create_loop", ("rz_mix.monitor", PHONES))) == 0


async def test_set_device_bad_name_is_400():
    ops = FakeAudioOps(sinks=sinks(SPEAKERS))
    with pytest.raises(CommandError):
        await run(AudioController(ops).set_device(123))
