"""Чистая логика звука: классификация, выбор устройства, режим, issues, подтверждение.

Живой системы здесь нет вовсе — только функции над обычными списками и словарями.
"""
from __future__ import annotations

import pytest

from server.audio import (
    MONITOR,
    NORMAL,
    PHYSICAL,
    STREAM,
    VIRTUAL,
    choose_device,
    classify,
    collect_issues,
    compute_mode,
    confirm_required,
    loop_issues,
    mode_issues,
)

pytestmark = pytest.mark.unit

# Та же гарнитура называется по-разному: беспроводной донгл (iec958) или режим
# зарядки по USB (analog-stereo). Обе — физические выходы.
PHONES = "alsa_output.usb-Logitech_A20_X_0000000000000000-00.iec958-stereo"
PHONES_USB = "alsa_output.usb-Logitech_A20_X_-_USB_wired_mode_....analog-stereo"
SPEAKERS = "alsa_output.pci-0000_00_1f.3.analog-stereo"


# ── 1. классификация выходов ──

def test_classify_physical_virtual_and_monitor():
    assert classify(PHONES) == PHYSICAL
    assert classify(SPEAKERS) == PHYSICAL
    # три наших выхода и EasyEffects — виртуальные
    assert classify("rz_mix") == VIRTUAL
    assert classify("game_mix") == VIRTUAL
    assert classify("discord_mix") == VIRTUAL
    assert classify("easyeffects_sink") == VIRTUAL
    # мониторы — по суффиксу, даже если основа виртуальная
    assert classify(PHONES + ".monitor") == MONITOR
    assert classify("rz_mix.monitor") == MONITOR


def test_classify_unknown_name_is_physical():
    """Незнакомое имя — физический выход: виртуальные перечислены явным списком."""
    assert classify("alsa_output.usb-какая-то-карта.analog-stereo") == PHYSICAL
    assert classify("") == PHYSICAL


# ── 2. выбор устройства (см. docs/AUDIO.md) ──

def test_choose_device_prefers_saved_choice():
    names = [SPEAKERS, PHONES, "rz_mix"]
    assert choose_device(names, PHONES) == PHONES


def test_choose_device_ignores_choice_that_is_virtual_or_missing():
    names = [PHONES_USB, SPEAKERS]
    # выбран виртуальный выход — не годится, падаем на образец Logitech_A20
    assert choose_device(names, "rz_mix") == PHONES_USB
    # выбранного устройства больше нет в системе — тоже образец
    assert choose_device(names, "alsa_output.пропал") == PHONES_USB


def test_choose_device_falls_back_by_phones_pattern_not_exact_name():
    """Имя гарнитуры меняется между донглом и зарядкой — ищем по образцу."""
    names = [SPEAKERS, PHONES_USB]
    assert choose_device(names, "") == PHONES_USB
    assert choose_device(names, None) == PHONES_USB


def test_choose_device_falls_back_to_first_physical():
    names = ["rz_mix", SPEAKERS, "game_mix"]
    assert choose_device(names, "") == SPEAKERS
    assert choose_device(names, "нет-такого") == SPEAKERS


def test_choose_device_never_returns_virtual_or_monitor():
    names = ["rz_mix", "game_mix", "discord_mix", "easyeffects_sink",
             SPEAKERS + ".monitor"]
    assert choose_device(names, "") == ""


def test_choose_device_empty_when_no_physical():
    assert choose_device([]) == ""


# ── 3. режим из состояния службы ──

def test_compute_mode_from_service_state():
    assert compute_mode(True, True) == STREAM
    # сторож поднят вручную: enabled false, active true — всё равно стрим
    assert compute_mode(False, True) == STREAM
    # включён в автозапуске, но сейчас не работает — режим всё равно стрим
    assert compute_mode(True, False) == STREAM
    assert compute_mode(False, False) == NORMAL


# ── 4. подтверждение при эфире ──

def test_confirm_required_only_when_live_or_recording():
    assert confirm_required(True, False) is True
    assert confirm_required(False, True) is True
    assert confirm_required(True, True) is True
    assert confirm_required(False, False) is False


# ── 5. разбор связей петель в issues ──

def test_loop_issues_reports_wrong_device():
    loops = [{"source": "rz_mix.monitor", "targets": [SPEAKERS]}]
    issues = loop_issues(loops, PHONES)
    assert len(issues) == 1
    assert "rz_mix.monitor" in issues[0]
    assert "не в выбранное устройство" in issues[0]
    assert SPEAKERS in issues[0]


def test_loop_issues_reports_two_devices():
    loops = [{"source": "rz_mix.monitor", "targets": [SPEAKERS, PHONES]}]
    issues = loop_issues(loops, PHONES)
    assert len(issues) == 1
    assert "играет в два устройства" in issues[0]
    assert SPEAKERS in issues[0] and PHONES in issues[0]


def test_loop_issues_quiet_when_everything_on_chosen():
    loops = [{"source": "rz_mix.monitor", "targets": [PHONES]},
             {"source": "game_mix.monitor", "targets": [PHONES]}]
    assert loop_issues(loops, PHONES) == []


# ── 6. issues по режиму ──

def test_mode_issues_stream_without_watchdog():
    service = {"enabled": True, "active": False}
    issues = mode_issues(STREAM, service, virtual_present=True)
    assert "режим стрима, но сторож не работает" in issues


def test_mode_issues_normal_with_leftover_virtuals():
    issues = mode_issues(NORMAL, {"enabled": False, "active": False}, virtual_present=True)
    assert "в обычном режиме остались виртуальные выходы" in issues


def test_mode_issues_quiet_when_consistent():
    assert mode_issues(STREAM, {"enabled": True, "active": True}, virtual_present=True) == []
    assert mode_issues(NORMAL, {"enabled": False, "active": False}, virtual_present=False) == []


# ── 7. сборка полного списка ──

def test_collect_issues_combines_loops_and_mode():
    loops = [{"source": "rz_mix.monitor", "targets": [SPEAKERS, PHONES]}]
    names = [PHONES, SPEAKERS, "rz_mix", "game_mix", "discord_mix"]
    issues = collect_issues(NORMAL, {"enabled": False, "active": False}, loops, PHONES, names)
    assert any("играет в два устройства" in i for i in issues)
    assert any("остались виртуальные выходы" in i for i in issues)
