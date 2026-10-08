"""Автоматический режим звука: пока идёт эфир — «Стрим», после эфира — «Обычный».

Зачем: владелец забывает вернуть режим после трансляции, и миксы со сторожем
продолжают уводить звук игры и Discord в rz_mix. И наоборот: если начать эфир в
«Обычном», в трансляцию не попадёт звук игры.

Правила (согласованы с владельцем):

* реагируем только на СМЕНУ «идёт эфир / эфир кончился», а не на текущее значение —
  поэтому режим, переключённый руками посреди эфира, обратно не откатывается;
* новое состояние должно устояться: короткое мигание флага эфира ничего не меняет;
* пока OBS офлайн, состояние неизвестно — ждём `offline_grace` секунд, чтобы обрыв
  связи не погасил звук прямо во время трансляции;
* на первом наблюдении ничего не переключаем, но запоминаем порядок вещей, чтобы
  не дёргать систему на каждом опросе.

Тумблер — файлом `~/stream/redzone/rz_mix.auto` (`off` выключает автоматику);
рядом с `rz_mix.device` и `rz_mix.state`, чтобы его можно было снять и вернуть.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import time

from .audio import NORMAL, STREAM

AUTO_FLAG = pathlib.Path.home() / "stream" / "redzone" / "rz_mix.auto"

_log = logging.getLogger("panel.audio.auto")


def read_auto_flag(path: pathlib.Path = AUTO_FLAG) -> bool:
    """Автоматика включена по умолчанию; файл со словом `off` её выключает."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return text.strip().lower() != "off"


def write_auto_flag(enabled: bool, path: pathlib.Path = AUTO_FLAG) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("on\n" if enabled else "off\n", encoding="utf-8")


class AutoSwitch:
    """Решает, надо ли переключить режим звука вслед за состоянием эфира.

    Чистая логика без системы: вызывается на каждом опросе состояния, отдаёт
    решение. Что и как включать — забота вызывающего.
    """

    def __init__(self, *, enabled: bool = True, hold_up: float = 2.0,
                 hold_down: float = 6.0, offline_grace: float = 25.0,
                 flag_path: pathlib.Path | None = None):
        self.enabled = bool(enabled)
        self.hold_up = float(hold_up)          # сколько ждать подтверждения старта
        self.hold_down = float(hold_down)      # и остановки
        self.offline_grace = float(offline_grace)
        self.flag_path = pathlib.Path(flag_path) if flag_path is not None else AUTO_FLAG
        self._desired = None                   # чего ждём: STREAM | NORMAL | None
        self._since = None                     # когда это желание появилось
        self._acted = None                     # что автоматика уже включила
        self._offline_since = None             # с какого момента OBS не отвечает

    def set_enabled(self, enabled: bool) -> None:
        """Тумблер: и в памяти, и в файле — чтобы решение переживало перезапуск."""
        self.enabled = bool(enabled)
        write_auto_flag(self.enabled, self.flag_path)

    def observe(self, *, online: bool, streaming: bool, mode: str, now: float):
        """Один замер. Возвращает STREAM/NORMAL — что включить, либо None.

        `mode` — какой режим сейчас в системе: если он уже нужный, переключать
        нечего, но заслуга автоматики всё равно засчитывается.
        """
        if not self.enabled:
            return None
        desired = self._desired_mode(online, streaming, now)
        if desired is None:
            return None                        # состояние неизвестно — ждём
        if desired != self._desired or self._since is None:
            self._desired, self._since = desired, now
            return None                        # желание сменилось — пусть устоится
        if self._acted == desired:
            return None                        # уже сделано
        hold = self.hold_up if desired == STREAM else self.hold_down
        if now - self._since < hold:
            return None                        # ещё не устоялось
        self._acted = desired
        return None if mode == desired else desired

    def _desired_mode(self, online: bool, streaming: bool, now: float):
        if online:
            self._offline_since = None
            return STREAM if streaming else NORMAL
        # OBS офлайн. Если он пропал на глазах у идущего эфира — даём шанс вернуться:
        # моргнувшая связь не должна гасить миксы посреди трансляции.
        if STREAM in (self._acted, self._desired):
            if self._offline_since is None:
                self._offline_since = now
            if now - self._offline_since < self.offline_grace:
                return None
        return NORMAL


async def step(panel, audio, switch: AutoSwitch, *, now: float):
    """Один замер состояния и, если надо, переключение режима.

    Возвращает конверт переключения либо None, если делать нечего.
    """
    state = panel.state if isinstance(panel.state, dict) else {}
    stream = state.get("stream") or {}
    verdict = switch.observe(online=bool(state.get("obs")),
                             streaming=bool(stream.get("active")),
                             mode=audio.current_mode(), now=now)
    if not verdict:
        return None
    # Автоматику владелец включил сам, поэтому она не спрашивает подтверждения:
    # переключение на старте эфира — и есть её работа.
    return await audio.set_mode(verdict, confirm=True)


async def run(panel, audio, switch: AutoSwitch, *, interval: float = 2.0,
              sleep=asyncio.sleep, clock=time.monotonic) -> None:
    """Следить за состоянием эфира и держать режим звука в согласии с ним.

    Сбои здесь не имеют права ронять пульт: любая ошибка — в журнал, и следующий
    замер. Владелец жмёт кнопки во время эфира, поэтому молчаливое падение
    автоматики недопустимо.
    """
    while True:
        try:
            envelope = await step(panel, audio, switch, now=clock())
            if envelope is not None:
                mode = (envelope.get("state") or {}).get("mode")
                _log.info("автоматика: режим звука → %s (шаги: %s)",
                          mode, ", ".join(envelope.get("changed") or []) or "—")
        except Exception:                      # noqa: BLE001 — пульт важнее автоматики
            _log.exception("автоматика режима звука: сбой, продолжаю")
        await sleep(interval)
