"""Журнал действий пульта (ТЗ §3 F8).

Одна строка — одно событие, JSON в UTF-8 (не ASCII), с переводом строки в конце.
Файл дополняется в режиме O_APPEND и переживает рестарт: ничего не перезаписывается.
Каталог под файл создаётся при необходимости, права — 0600 (в журнале адреса
устройств). Запись защищена блокировкой: журнал может быть общим для нескольких
задач, а дописывание — одной операцией из заранее собранного буфера, поэтому сбой
посреди записи не оставляет обрывок строки.

Ротация по размеру: если дописывание превысило бы `max_bytes`, текущий файл
переименовывается в `.1`, старые сдвигаются (`.2`, `.3`, ...), лишние сверх `keep`
удаляются. Ошибки записи наружу пробрасываются — их перехватывает вызывающий слой
(HTTP-слой пульта), чтобы недоступная ФС не роняла сервис.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import time
from datetime import datetime, timezone

_FILE_MODE = 0o600


class Journal:
    """Дописываемый журнал JSONL с ротацией по размеру."""

    def __init__(self, path, *, max_bytes: int = 1_000_000, keep: int = 3,
                 clock=time.time):
        self.path = pathlib.Path(path)
        self.max_bytes = int(max_bytes)
        self.keep = int(keep)
        self.clock = clock
        self._lock = asyncio.Lock()

    def entry(self, **fields) -> dict:
        """Собрать запись: `time` (ISO-8601 с таймзоной из `clock`) плюс поля как переданы."""
        record = {"time": datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat()}
        record.update(fields)
        return record

    async def write(self, entry: dict) -> None:
        """Дописать одну строку и сбросить на диск. Блокировка — на весь цикл."""
        data = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._would_overflow(len(data)):
                self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
            try:
                written = os.write(fd, data)
                if written != len(data):
                    raise OSError("журнал: записана только часть строки")
            finally:
                os.close(fd)
            # umask мог ослабить права при создании — гарантируем 0600.
            with contextlib.suppress(OSError):
                os.chmod(self.path, _FILE_MODE)

    # ── ротация ──
    def _would_overflow(self, incoming: int) -> bool:
        try:
            current = self.path.stat().st_size
        except OSError:
            return False
        return current > 0 and current + incoming > self.max_bytes

    def _rotate(self) -> None:
        """Сдвинуть base -> .1, .1 -> .2, ... ; лишнее сверх `keep` удалить."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(self._suffixed(self.keep))
        for index in range(self.keep - 1, 0, -1):
            src = self._suffixed(index)
            if os.path.exists(src):
                os.replace(src, self._suffixed(index + 1))
        if self.path.exists():
            os.replace(self.path, self._suffixed(1))

    def _suffixed(self, index: int) -> pathlib.Path:
        return self.path.with_name(f"{self.path.name}.{index}")
