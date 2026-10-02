"""Журнал действий (ТЗ §3 F8): запись, время, ротация, устойчивость (unit).

Живого OBS здесь нет: проверяется только файловый слой — построение записи,
дописывание строк, ротация по размеру, права и переживание рестарта.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime

import pytest

from server.journal import Journal

pytestmark = pytest.mark.unit


def _lines(path) -> list[str]:
    """Непустые строки файла журнала."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


async def test_write_one_entry_is_valid_json_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)

    await journal.write(journal.entry(action="scene.set", result="ok"))

    assert path.exists()
    lines = _lines(path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["action"] == "scene.set"
    assert record["result"] == "ok"
    assert "time" in record


def test_entry_time_is_iso_with_timezone_from_clock(tmp_path):
    fixed = 1_700_000_000.0  # 2023-11-14T22:13:20+00:00
    journal = Journal(tmp_path / "journal.jsonl", clock=lambda: fixed)

    record = journal.entry(action="panic")

    parsed = datetime.fromisoformat(record["time"])
    assert parsed.tzinfo is not None
    assert record["action"] == "panic"


async def test_rotation_shifts_and_drops_beyond_keep(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path, max_bytes=100, keep=2)

    for i in range(12):
        await journal.write(journal.entry(n=i))

    assert path.exists()
    assert (tmp_path / "journal.jsonl.1").exists()
    assert (tmp_path / "journal.jsonl.2").exists()
    # сверх keep не хранится
    assert not (tmp_path / "journal.jsonl.3").exists()

    # самый свежий файл — базовый, с последними записями; размер ограничен
    newest = path.read_text(encoding="utf-8")
    assert '"n": 11' in newest
    assert path.stat().st_size <= 100


async def test_survives_restart_appends_without_loss(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = Journal(path)
    await first.write(first.entry(n=1))

    # «рестарт»: новый объект на том же пути, обычный режим добавления
    second = Journal(path)
    await second.write(second.entry(n=2))

    records = [json.loads(line) for line in _lines(path)]
    assert [r["n"] for r in records] == [1, 2]


async def test_parallel_writes_are_not_glued(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)

    await asyncio.gather(*(journal.write(journal.entry(n=i)) for i in range(20)))

    lines = _lines(path)
    assert len(lines) == 20
    records = [json.loads(line) for line in lines]
    assert sorted(r["n"] for r in records) == list(range(20))


async def test_file_mode_is_0600(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)

    await journal.write(journal.entry(action="auth"))

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


async def test_missing_parent_dir_is_created(tmp_path):
    path = tmp_path / "вложенный" / "ещё" / "journal.jsonl"
    journal = Journal(path)

    await journal.write(journal.entry(action="x"))

    assert path.exists()
    assert len(_lines(path)) == 1
