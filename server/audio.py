"""Чистая логика звука: классификация выходов, выбор устройства, режим, issues.

Модуль ничего не знает ни про pactl, ни про OBS, ни про HTTP: только обычные
функции над списками и словарями. Поэтому вся логика «что правильно» проверяется
без живой системы, а системный слой (`audio_ops`) и HTTP лишь собирают данные
по этим правилам.

Источник правды о режиме — состояние службы-сторожа, а не наш файл: сторож могли
поднять руками, и тогда режим уже «стрим», хотя в автозапуск его не вернули.
"""
from __future__ import annotations

# Виды выходов в терминах контракта GET /api/audio.
PHYSICAL = "physical"
VIRTUAL = "virtual"
MONITOR = "monitor"

# Два режима звука.
STREAM = "stream"
NORMAL = "normal"

# Виртуальные выходы: три наших и выход EasyEffects. Петля в них звука не даёт,
# поэтому в выборе устройства и в качестве цели монитора они не участвуют.
MIX_SINKS = ("rz_mix", "game_mix", "discord_mix")
VIRTUAL_SINKS = frozenset(MIX_SINKS) | {"easyeffects_sink"}

# Образец имени гарнитуры: одна и та же гарнитура зовётся по-разному в
# беспроводном режиме и в режиме зарядки по USB — зашивать точное имя нельзя.
DEVICE_HINT = "Logitech_A20"

_MONITOR_SUFFIX = ".monitor"


def classify(name: str) -> str:
    """Вид выхода: `monitor` | `virtual` | `physical`.

    Монитор важнее виртуальности: `rz_mix.monitor` — это монитор, а не выход
    (по контракту мониторы определяются суффиксом). Всё незнакомое — физический
    выход: список виртуальных закрытый.
    """
    if not isinstance(name, str) or not name:
        return PHYSICAL
    if name.endswith(_MONITOR_SUFFIX):
        return MONITOR
    if name in VIRTUAL_SINKS or name.startswith("easyeffects"):
        return VIRTUAL
    return PHYSICAL


def choose_device(names, chosen=None, hint: str = DEVICE_HINT) -> str:
    """Устройство мониторинга (см. docs/AUDIO.md): выбранное → образец гарнитуры → первый физический.

    Выбранное устройство принимается только если оно физическое и ещё существует:
    иначе (файл пуст, устройство пропало, в файле виртуальный выход) падаем на
    поиск по образцу `Logitech_A20`, затем на первый физический выход. Пустая
    строка, если физических выходов нет вовсе.
    """
    physical = [n for n in (names or []) if classify(n) == PHYSICAL]
    if chosen and chosen in physical:
        return chosen
    for name in physical:
        if hint in name:
            return name
    return physical[0] if physical else ""


def compute_mode(enabled, active) -> str:
    """Режим из состояния службы: сторож включён ИЛИ работает → режим стрима.

    Сторож мог быть поднят вручную (`enabled` false, `active` true) — это всё ещё
    стрим. Обратный случай (`enabled` true, `active` false) — тоже стрим: режим
    задан, но сторож не работает (это попадёт в `issues`).
    """
    return STREAM if (bool(enabled) or bool(active)) else NORMAL


def confirm_required(streaming, recording) -> bool:
    """Нужно ли подтверждение: переключение рвёт звук в живом эфире или записи."""
    return bool(streaming) or bool(recording)


def loop_issues(loops, chosen) -> list[str]:
    """Проблемы связей петель: играет не в выбранное устройство или сразу в два."""
    issues: list[str] = []
    for loop in loops or []:
        source = (loop or {}).get("source") or "?"
        targets = [t for t in ((loop or {}).get("targets") or []) if t]
        if len(targets) > 1:
            issues.append(f"петля {source} играет в два устройства: {', '.join(targets)}")
        elif len(targets) == 1 and targets[0] != chosen:
            issues.append(f"петля {source} играет в {targets[0]}, "
                          f"а не в выбранное устройство {chosen or '—'}")
    return issues


def mode_issues(mode, service, virtual_present: bool) -> list[str]:
    """Проблемы режима: сторож молчит при стриме или остались виртуальные выходы."""
    issues: list[str] = []
    if mode == STREAM and not (service or {}).get("active"):
        issues.append("режим стрима, но сторож не работает")
    if mode == NORMAL and virtual_present:
        issues.append("в обычном режиме остались виртуальные выходы")
    return issues


def collect_issues(mode, service, loops, chosen, names) -> list[str]:
    """Полный список человеческих проблем для владельца (пусто, если всё хорошо)."""
    # В обычном режиме «лишними» считаются только НАШИ миксы: easyeffects_sink живёт
    # в системе всегда и к разделению звука для эфира отношения не имеет.
    virtual_present = any(name in MIX_SINKS for name in (names or []))
    return loop_issues(loops, chosen) + mode_issues(mode, service, virtual_present)
