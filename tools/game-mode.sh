#!/usr/bin/env bash
# «Режим игры»: погасить стрим-стек и освободить VRAM, чтобы игра шла ровно.
#
#   tools/game-mode.sh on       выключить стрим-стек (обратимо)
#   tools/game-mode.sh off      вернуть ровно то, что было выключено
#   tools/game-mode.sh status   что сейчас выключено этим режимом
#
# Что гасится: модули (аналитика, совместный каст, нормализатор звука — если
# установлены), чат-релей, сторож звука, таймер турнирной сетки, хаб MediaMTX
# (контейнер) и загруженная локальная модель Ollama.
#
# Гарантии:
#   * ничего не удаляется — только останавливается (docker stop, а не rm);
#   * обратимо: `off` возвращает ровно те службы, что были активны до `on`,
#     — их список хранится в файле состояния;
#   * OBS не трогается: гасить эфир из скрипта опасно, это делает владелец;
#   * пульт остаётся работать — иначе режим нечем вернуть из интерфейса.
#
# Свои службы можно добавить через переменную GAME_MODE_UNITS (через двоеточие):
# они остановятся и вернутся так же.
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/stream-panel"
STATE="$STATE_DIR/game-mode.state"
HUB_CONTAINER="${STREAM_HUB_CONTAINER:-mediamtx}"

# Службы и таймеры, которые относятся к стрим-стеку. Отсутствующие и уже
# выключенные просто пропускаются.
UNITS=(
    rz-cocast-relay.service
    rz-chat.service
    rz-mix.service
    dota-analytics.service
    rz-bracket.timer
    rz-audio-sync.service
)
if [ -n "${GAME_MODE_UNITS:-}" ]; then
    IFS=':' read -r -a extra <<<"$GAME_MODE_UNITS"
    UNITS+=("${extra[@]}")
fi

say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf 'предупреждение: %s\n' "$*" >&2; }

unit_active() { systemctl --user is-active --quiet "$1" 2>/dev/null; }

hub_running() {
    command -v docker >/dev/null 2>&1 || return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$HUB_CONTAINER" 2>/dev/null || echo false)" = "true" ]
}

# Занятая VRAM в МиБ (пусто, если nvidia-smi нет).
vram_used() {
    command -v nvidia-smi >/dev/null 2>&1 || return 0
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -d ' '
}

report_vram() {
    local before="$1" after="$2"
    [ -n "$before" ] || return 0
    if [ -n "$after" ]; then
        say "VRAM: было ${before} МиБ, стало ${after} МиБ (освободилось $(( before - after )) МиБ)"
    else
        say "VRAM до операции: ${before} МиБ"
    fi
}

# --- включить режим игры --------------------------------------------------
turn_on() {
    mkdir -p "$STATE_DIR"
    if [ -f "$STATE" ]; then
        say "режим игры уже включён (состояние: $STATE)"
        say "вернуть всё: tools/game-mode.sh off"
        return 0
    fi
    : >"$STATE"
    chmod 600 "$STATE"

    local before after; before="$(vram_used)"
    say "режим игры: гашу стрим-стек (ничего не удаляется, только останавливается)"

    local unit
    for unit in "${UNITS[@]}"; do
        if unit_active "$unit"; then
            echo "unit $unit" >>"$STATE"
            if systemctl --user stop "$unit"; then
                note "остановлен: $unit"
            else
                warn "не удалось остановить: $unit"
            fi
        fi
    done

    if hub_running; then
        echo "container $HUB_CONTAINER" >>"$STATE"
        if docker stop "$HUB_CONTAINER" >/dev/null 2>&1; then
            note "остановлен хаб MediaMTX ($HUB_CONTAINER)"
        else
            warn "не удалось остановить хаб ($HUB_CONTAINER)"
        fi
    fi

    if command -v ollama >/dev/null 2>&1; then
        local model
        while IFS= read -r model; do
            [ -n "$model" ] || continue
            echo "ollama $model" >>"$STATE"
            if ollama stop "$model" >/dev/null 2>&1; then
                note "выгружена локальная модель: $model"
            else
                warn "не удалось выгрузить модель: $model"
            fi
        done < <(ollama ps 2>/dev/null | awk 'NR > 1 {print $1}')
    fi

    after="$(vram_used)"
    say
    if [ -s "$STATE" ]; then
        note "остановлено позиций: $(wc -l <"$STATE")"
    else
        note "стрим-стек уже был выключен — останавливать было нечего"
    fi
    report_vram "$before" "$after"
    say "Пульт не тронут. Вернуть всё: tools/game-mode.sh off"
}

# --- выключить режим игры (вернуть как было) ------------------------------
turn_off() {
    if [ ! -f "$STATE" ]; then
        say "режим игры не включён — возвращать нечего"
        return 0
    fi

    local before after; before="$(vram_used)"
    say "режим игры: возвращаю то, что было остановлено"

    local kind name
    while read -r kind name; do
        [ -n "${kind:-}" ] || continue
        case "$kind" in
            unit)
                if systemctl --user start "$name"; then
                    note "возвращён: $name"
                else
                    warn "не удалось вернуть: $name"
                fi
                ;;
            container)
                if docker start "$name" >/dev/null 2>&1; then
                    note "возвращён хаб MediaMTX ($name)"
                else
                    warn "не удалось вернуть контейнер: $name"
                fi
                ;;
            ollama)
                # Модель останавливается (выгружается из VRAM), а не удаляется;
                # Ollama поднимет её сама при первом же запросе.
                note "локальная модель $name загрузится сама при первом запросе"
                ;;
        esac
    done <"$STATE"

    rm -f "$STATE"
    after="$(vram_used)"
    say
    report_vram "$before" "$after"
    say "Режим игры выключен."
}

# --- состояние ------------------------------------------------------------
show_status() {
    say "Режим игры: $( [ -f "$STATE" ] && echo включён || echo выключен )"
    if [ -f "$STATE" ]; then
        if [ -s "$STATE" ]; then
            say "остановлено этим режимом:"
            sed 's/^/  /' "$STATE"
        else
            say "  (ничего не было запущено)"
        fi
    fi
    local cur; cur="$(vram_used)"
    [ -n "$cur" ] && say "занято VRAM сейчас: ${cur} МиБ"
}

case "${1:-}" in
    on|enable)   turn_on ;;
    off|disable) turn_off ;;
    status|st)   show_status ;;
    -h|--help|"")
        sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        [ -n "${1:-}" ] || exit 2
        ;;
    *)
        warn "неизвестная команда: $1"
        exit 2
        ;;
esac
