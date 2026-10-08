"""Автоматический режим звука по состоянию эфира — проверки без системы.

Правило, которое проверяем: реагируем на СМЕНУ «идёт эфир / эфир кончился», и только
по свежему состоянию OBS. Ручную смену режима посреди эфира не откатываем.
"""
from __future__ import annotations

import asyncio

import pytest

from server.audio import NORMAL, STREAM
from server.audio_auto import (AutoSwitch, read_auto_flag, run, step,
                               write_auto_flag)

pytestmark = pytest.mark.unit


def test_start_of_stream_turns_mode_on():
    """Пошёл эфир — включаем «Стрим», но не мгновенно, а когда состояние устоялось."""
    sw = AutoSwitch(hold_up=1.5, hold_down=6.0)

    assert sw.observe(online=True, streaming=False, mode=NORMAL, now=0.0) is None
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=10.0) is None    # кандидат
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=12.0) == STREAM  # устоялось
    # дальше молчим: режим уже включён, а состояние эфира не менялось
    assert sw.observe(online=True, streaming=True, mode=STREAM, now=14.0) is None
    assert sw.observe(online=True, streaming=True, mode=STREAM, now=90.0) is None


def test_end_of_stream_returns_normal():
    sw = AutoSwitch(hold_up=1.5, hold_down=6.0)
    sw.observe(online=True, streaming=False, mode=NORMAL, now=0.0)
    sw.observe(online=True, streaming=True, mode=NORMAL, now=10.0)
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=12.0) == STREAM

    assert sw.observe(online=True, streaming=False, mode=STREAM, now=30.0) is None
    assert sw.observe(online=True, streaming=False, mode=STREAM, now=37.0) == NORMAL
    assert sw.observe(online=True, streaming=False, mode=NORMAL, now=99.0) is None


def test_short_hiccup_of_stream_state_does_not_switch():
    """OBS на миг сказал «не в эфире» — это не повод гасить миксы."""
    sw = AutoSwitch(hold_up=2.0, hold_down=6.0)
    sw.observe(online=True, streaming=True, mode=STREAM, now=0.0)

    assert sw.observe(online=True, streaming=False, mode=STREAM, now=1.0) is None   # мигнуло
    assert sw.observe(online=True, streaming=True, mode=STREAM, now=2.0) is None    # вернулось
    assert sw.observe(online=True, streaming=True, mode=STREAM, now=120.0) is None


def test_obs_going_offline_ends_stream_only_after_grace():
    """OBS закрыли — считаем, что эфир кончился, но не сразу: связь могла мигнуть.

    Сначала ждём `offline_grace`, и только потом отсчитываем `hold_down` — итого
    миксы гаснут заметно позже закрытия OBS. Так и задумано: моргнувшая связь не
    должна гасить звук посреди трансляции.
    """
    sw = AutoSwitch(hold_down=5.0, offline_grace=20.0)
    sw.observe(online=True, streaming=True, mode=STREAM, now=0.0)

    assert sw.observe(online=False, streaming=False, mode=STREAM, now=5.0) is None    # мёртвая зона
    assert sw.observe(online=False, streaming=False, mode=STREAM, now=23.0) is None   # ещё ждём связь
    assert sw.observe(online=False, streaming=False, mode=STREAM, now=26.0) is None   # грейс вышел, отсчёт заново
    assert sw.observe(online=False, streaming=False, mode=STREAM, now=32.0) == NORMAL
    assert sw.observe(online=False, streaming=False, mode=NORMAL, now=90.0) is None


def test_obs_returning_within_grace_keeps_mode():
    sw = AutoSwitch(hold_down=5.0, offline_grace=20.0)
    sw.observe(online=True, streaming=True, mode=STREAM, now=0.0)

    assert sw.observe(online=False, streaming=False, mode=STREAM, now=5.0) is None
    assert sw.observe(online=True, streaming=True, mode=STREAM, now=10.0) is None   # вернулся в эфире
    assert sw.observe(online=True, streaming=True, mode=STREAM, now=600.0) is None


def test_manual_change_mid_stream_is_not_reverted():
    """Владелец сам вернул «Обычный» посреди эфира — не спорим с ним."""
    sw = AutoSwitch(hold_up=2.0, hold_down=6.0)
    sw.observe(online=True, streaming=False, mode=NORMAL, now=0.0)
    sw.observe(online=True, streaming=True, mode=NORMAL, now=10.0)
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=13.0) == STREAM

    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=20.0) is None
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=300.0) is None


def test_offline_startup_returns_leftover_mixes_to_normal():
    """Панель поднялась, OBS выключен, а миксы со сторожем остались от прошлого раза."""
    sw = AutoSwitch(hold_down=6.0)
    assert sw.observe(online=False, streaming=False, mode=STREAM, now=0.0) is None
    assert sw.observe(online=False, streaming=False, mode=STREAM, now=7.0) == NORMAL


def test_disabled_switch_never_acts():
    sw = AutoSwitch(enabled=False)
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=0.0) is None
    assert sw.observe(online=True, streaming=True, mode=NORMAL, now=99.0) is None
    assert sw.observe(online=False, streaming=False, mode=STREAM, now=200.0) is None


def test_toggle_file_defaults_to_on_and_can_be_switched_off(tmp_path):
    """Тумблер автоматики: по умолчанию включена, файл со словом off выключает."""
    flag = tmp_path / "rz_mix.auto"

    assert read_auto_flag(flag) is True          # файла нет — автоматика работает

    write_auto_flag(False, flag)
    assert flag.read_text(encoding="utf-8").strip() == "off"
    assert read_auto_flag(flag) is False

    write_auto_flag(True, flag)
    assert read_auto_flag(flag) is True


# ── связка с пультом: замер состояния и само переключение ──


class _Panel:
    """Подставная панель: автоматика читает отсюда только состояние."""

    def __init__(self, state):
        self.state = state


class _AudioTwin:
    """Подставной слой звука: помнит, что и с каким подтверждением включали."""

    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def current_mode(self):
        return self.mode

    async def set_mode(self, mode, *, confirm=False):
        self.calls.append((mode, confirm))
        self.mode = mode
        return {"ok": True, "name": "audio.mode", "changed": ["watchdog"],
                "steps": [], "state": {"mode": mode}, "error": None}


def _await(coro):
    return asyncio.wait_for(coro, timeout=1.0)


async def test_step_turns_stream_mode_on_when_stream_starts():
    """Пошёл эфир — автоматика сама включает «Стрим», подтверждение даёт тоже сама."""
    panel = _Panel({"obs": True, "stream": {"active": False}})
    audio = _AudioTwin(NORMAL)
    sw = AutoSwitch(hold_up=2.0, hold_down=6.0)
    await _await(step(panel, audio, sw, now=0.0))                  # исходное: не в эфире

    panel.state = {"obs": True, "stream": {"active": True}}
    assert await _await(step(panel, audio, sw, now=10.0)) is None  # копим подтверждение
    envelope = await _await(step(panel, audio, sw, now=12.0))      # состояние устоялось

    assert envelope is not None and envelope["ok"] is True
    assert audio.calls == [(STREAM, True)]
    assert await _await(step(panel, audio, sw, now=99.0)) is None
    assert audio.calls == [(STREAM, True)]                   # повторно не дёргаем


async def test_step_returns_normal_when_stream_ends():
    """Эфир кончился — миксы и сторож уходят, звук возвращается в наушники."""
    panel = _Panel({"obs": True, "stream": {"active": True}})
    audio = _AudioTwin(NORMAL)
    sw = AutoSwitch(hold_up=2.0, hold_down=6.0)
    await _await(step(panel, audio, sw, now=0.0))
    assert await _await(step(panel, audio, sw, now=3.0)) is not None   # эфир уже шёл → «Стрим»

    panel.state = {"obs": True, "stream": {"active": False}}
    assert await _await(step(panel, audio, sw, now=20.0)) is None
    assert await _await(step(panel, audio, sw, now=27.0)) is not None
    assert audio.calls[-1] == (NORMAL, True)


async def test_run_survives_failure_and_keeps_watching():
    """Сбой переключения не имеет права уронить пульт: ждём следующий замер."""
    import itertools

    class _Stop(Exception):
        pass

    panel = _Panel({"obs": True, "stream": {"active": True}})
    audio = _AudioTwin(NORMAL)

    async def boom(mode, *, confirm=False):
        raise RuntimeError("pactl отвалился")

    audio.set_mode = boom
    sw = AutoSwitch(hold_up=0.5, hold_down=0.5)
    ticks = []
    times = itertools.count(0.0, 5.0)

    async def sleep(_):
        ticks.append(1)
        if len(ticks) > 3:
            raise _Stop()

    with pytest.raises(_Stop):
        await _await(run(panel, audio, sw, interval=0, sleep=sleep, clock=lambda: next(times)))

    assert len(ticks) == 4        # цикл пережил сбой и продолжил замеры
