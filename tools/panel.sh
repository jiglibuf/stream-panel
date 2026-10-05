#!/usr/bin/env bash
# Пульт стрима: поднять, погасить, посмотреть состояние — для пульта и
# подключённых модулей.
#
#   tools/panel.sh up                 поднять пульт
#   tools/panel.sh down               погасить пульт
#   tools/panel.sh restart            перезапустить пульт
#   tools/panel.sh status             что сейчас работает
#   tools/panel.sh up all             пульт + все установленные модули + хаб
#   tools/panel.sh up chat mix        только выбранные модули
#
# Компоненты: panel (по умолчанию), chat, mix, analytics, bracket, cocast, hub, all.
#
# Пульт поднимается юнитом systemd пользователя (stream-panel.service), если он
# установлен; иначе — scripts/run.sh. Оба запускают одно и то же на одном порту,
# поэтому одновременно их держать нельзя. Подъём/останов идемпотентны: повторный
# запуск при живом процессе ничего не ломает. Чужие службы не трогаются.
#
# Помните: юниты по умолчанию не включены в автозапуск — стек поднимается вручную
# тогда, когда владелец стримит.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HUB_CONTAINER="${STREAM_HUB_CONTAINER:-mediamtx}"
MEDIAMTX_DIR="${MEDIAMTX_DIR:-$HOME/mediamtx}"

say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf 'предупреждение: %s\n' "$*" >&2; }

have_unit() { [ -f "$UNIT_DIR/$1" ]; }
is_active() { systemctl --user is-active --quiet "$1" 2>/dev/null; }

# Имя юнита для модуля (пусто, если такого компонента нет).
module_unit() {
    case "$1" in
        chat)      echo "rz-chat.service" ;;
        mix)       echo "rz-mix.service" ;;
        analytics) echo "dota-analytics.service" ;;
        bracket)   echo "rz-bracket.timer" ;;
        cocast)    echo "rz-cocast-relay.service" ;;
        *)         return 1 ;;
    esac
}

# Развернуть `all` в список компонентов; остальное — как есть.
expand_component() {
    if [ "$1" = "all" ]; then
        printf '%s\n' panel chat mix analytics bracket cocast hub
    else
        printf '%s\n' "$1"
    fi
}

# --- пульт ----------------------------------------------------------------
panel_running_via_script() {
    local pid_file="$ROOT/run/panel.pid"
    [ -f "$pid_file" ] || return 1
    local pid; pid="$(cat "$pid_file" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

panel_up() {
    if have_unit stream-panel.service; then
        systemctl --user start stream-panel.service
        if is_active stream-panel.service; then
            say "пульт поднят (stream-panel.service)"
        else
            warn "юнит запущен, но не активен — смотри: journalctl --user -u stream-panel"
            return 1
        fi
    else
        # Юнита нет — запускаем тем же способом, что и раньше: scripts/run.sh.
        "$ROOT/scripts/run.sh"
    fi
}

panel_down() {
    if have_unit stream-panel.service; then
        # Юнит установлен, но пульт могли поднять и скриптом — тогда гасим скриптом.
        if ! is_active stream-panel.service && panel_running_via_script; then
            "$ROOT/scripts/stop.sh"
            return
        fi
        systemctl --user stop stream-panel.service
        say "пульт остановлен (stream-panel.service)"
    else
        "$ROOT/scripts/stop.sh"
    fi
}

panel_state() {
    if have_unit stream-panel.service; then
        if is_active stream-panel.service; then
            echo "работает (stream-panel.service)"
        else
            echo "выключен (stream-panel.service)"
        fi
    elif panel_running_via_script; then
        echo "работает (scripts/run.sh, pid $(cat "$ROOT/run/panel.pid" 2>/dev/null))"
    else
        echo "выключен"
    fi
}

# --- хаб MediaMTX ---------------------------------------------------------
hub_running() {
    command -v docker >/dev/null 2>&1 || return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$HUB_CONTAINER" 2>/dev/null || echo false)" = "true" ]
}

hub_up() {
    if ! command -v docker >/dev/null 2>&1; then
        warn "docker не найден — хаб пропущен"
        return 1
    fi
    if docker inspect "$HUB_CONTAINER" >/dev/null 2>&1; then
        docker start "$HUB_CONTAINER" >/dev/null && say "хаб поднят ($HUB_CONTAINER)"
    elif [ -f "$MEDIAMTX_DIR/compose.yaml" ]; then
        ( cd "$MEDIAMTX_DIR" && docker compose up -d )
        say "хаб поднят через compose ($MEDIAMTX_DIR)"
    else
        warn "контейнер $HUB_CONTAINER не создан и нет $MEDIAMTX_DIR/compose.yaml — хаб пропущен"
        return 1
    fi
}

hub_down() {
    command -v docker >/dev/null 2>&1 || { note "docker не найден — хаб пропущен"; return 0; }
    if hub_running; then
        # Только stop: контейнер сохраняется, включение — тем же hub_up.
        docker stop "$HUB_CONTAINER" >/dev/null && say "хаб остановлен ($HUB_CONTAINER)"
    else
        say "хаб уже остановлен"
    fi
}

# --- диспетчер ------------------------------------------------------------
start_one() {
    case "$1" in
        panel) panel_up ;;
        hub)   hub_up ;;
        *)
            local unit; unit="$(module_unit "$1")"
            if have_unit "$unit"; then
                systemctl --user start "$unit"
                say "поднят: $1 ($unit)"
            else
                note "модуль не установлен — пропускаю: $1"
            fi
            ;;
    esac
}

stop_one() {
    case "$1" in
        panel) panel_down ;;
        hub)   hub_down ;;
        *)
            local unit; unit="$(module_unit "$1")"
            if have_unit "$unit"; then
                if is_active "$unit"; then
                    systemctl --user stop "$unit"
                    say "погашен: $1 ($unit)"
                else
                    say "уже выключен: $1 ($unit)"
                fi
            else
                note "модуль не установлен — пропускаю: $1"
            fi
            ;;
    esac
}

status_one() {
    local name="$1" unit state label
    case "$name" in
        panel)
            say "пульт (stream-panel.service): $(panel_state)"
            return ;;
        hub)
            if hub_running; then state="работает (контейнер $HUB_CONTAINER)"
            elif command -v docker >/dev/null 2>&1; then state="выключен"
            else state="docker не найден"; fi
            say "хаб MediaMTX ($HUB_CONTAINER): $state"
            return ;;
        chat)      label="чат-релей";        unit="$(module_unit chat)" ;;
        mix)       label="сторож звука";     unit="$(module_unit mix)" ;;
        analytics) label="аналитика";        unit="$(module_unit analytics)" ;;
        bracket)   label="турнирная сетка";  unit="$(module_unit bracket)" ;;
        cocast)    label="релей каста";      unit="$(module_unit cocast)" ;;
        *) note "неизвестный компонент: $name"; return ;;
    esac
    if ! have_unit "$unit"; then state="не установлен"
    elif is_active "$unit"; then state="работает"
    else state="выключен"; fi
    say "$label ($unit): $state"
}

usage() {
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

cmd="${1:-status}"
[ $# -gt 0 ] && shift

# Для status без аргументов показываем всё; для up/down — только пульт.
components=()
if [ $# -gt 0 ]; then
    for arg in "$@"; do
        while IFS= read -r c; do components+=("$c"); done < <(expand_component "$arg")
    done
elif [ "$cmd" = "status" ] || [ "$cmd" = "st" ]; then
    while IFS= read -r c; do components+=("$c"); done < <(expand_component all)
else
    components=(panel)
fi

case "$cmd" in
    up|start)
        for c in "${components[@]}"; do start_one "$c"; done
        ;;
    down|stop)
        for c in "${components[@]}"; do stop_one "$c"; done
        ;;
    restart)
        for c in "${components[@]}"; do stop_one "$c"; done
        for c in "${components[@]}"; do start_one "$c"; done
        ;;
    status|st)
        say "Состояние стрим-стека (по умолчанию всё выключено):"
        for c in "${components[@]}"; do status_one "$c"; done
        ;;
    -h|--help)
        usage
        ;;
    *)
        warn "неизвестная команда: $cmd"
        usage >&2
        exit 2
        ;;
esac
