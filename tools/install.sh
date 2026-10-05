#!/usr/bin/env bash
# Установка пульта стрима: окружение Python, фронт, каталог конфига, юниты.
#
#   tools/install.sh                  окружение + юниты; фронт — только если не собран
#   tools/install.sh --with-frontend  пересобрать фронт заново
#   tools/install.sh --skip-frontend  не трогать фронт
#   tools/install.sh --enable         включить юниты в автозапуск (по умолчанию — нет)
#   tools/install.sh --help
#
# Идемпотентен: повторный запуск не ломает установленное и ничего не дублирует.
# sudo не требует — это пользовательские юниты в ~/.config/systemd/user, правами
# системы установщик не распоряжается. Чужие службы не трогаются.
#
# Про автозапуск: по умолчанию юниты только КОПИРУЮТСЯ, но не включаются.
# Стрим-стек должен подниматься тогда, когда владелец стримит, а не при каждом
# входе в систему. Включение — осознанное, ключом --enable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/stream-panel"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

FRONTEND="auto"     # auto | always | skip
ENABLE=0

say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf 'предупреждение: %s\n' "$*" >&2; }

usage() {
    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --with-frontend) FRONTEND="always" ;;
        --skip-frontend) FRONTEND="skip" ;;
        --enable)        ENABLE=1 ;;
        -h|--help)       usage; exit 0 ;;
        *) warn "неизвестный ключ: $1"; usage >&2; exit 2 ;;
    esac
    shift
done

[ -f "$ROOT/pyproject.toml" ] || { warn "не похоже на репозиторий: нет $ROOT/pyproject.toml"; exit 1; }

say "Пульт стрима: установка"
say "репозиторий: $ROOT"
say

# --- 1. окружение Python ------------------------------------------------------
say "1/5 окружение Python"
if ! command -v python3 >/dev/null 2>&1; then
    warn "нет python3 — поставьте Python 3.12 или новее и повторите"
    exit 1
fi
ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
note "python3 $ver"
if [ ! -x "$PY" ]; then
    note "создаю $VENV"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV" >/dev/null
    else
        python3 -m venv "$VENV"
    fi
else
    note ".venv уже есть — не трогаю"
fi

# Ставим только то, что реально нужно сервису; наличие проверяем импортом,
# чтобы повторный запуск ничего не переустанавливал.
if "$PY" -c 'import aiohttp, websockets' >/dev/null 2>&1; then
    note "зависимости (aiohttp, websockets) уже стоят"
else
    note "ставлю зависимости (aiohttp, websockets)"
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$PY" aiohttp websockets >/dev/null
    else
        "$PY" -m pip install -q --upgrade pip
        "$PY" -m pip install -q aiohttp websockets
    fi
fi
say

# --- 2. фронт -----------------------------------------------------------------
say "2/5 веб-интерфейс"
need_frontend=0
case "$FRONTEND" in
    always) need_frontend=1 ;;
    auto)   [ -f "$ROOT/frontend/dist/index.html" ] || need_frontend=1 ;;
    skip)   note "пропускаю по ключу --skip-frontend" ;;
esac
if [ "$need_frontend" = 1 ]; then
    if command -v npm >/dev/null 2>&1; then
        note "собираю фронт (зависимости ставятся только для сборки)"
        if [ -f "$ROOT/frontend/package-lock.json" ]; then
            npm --prefix "$ROOT/frontend" ci  >/dev/null
        else
            npm --prefix "$ROOT/frontend" install >/dev/null
        fi
        npm --prefix "$ROOT/frontend" run build
    else
        warn "npm не найден — фронт не собран; сервис отдаст страницу-заглушку"
        note "поставьте Node 20+ и повторите: tools/install.sh --with-frontend"
    fi
else
    note "сборка уже на месте: frontend/dist/index.html"
fi
say

# --- 3. каталог конфига -------------------------------------------------------
say "3/5 каталог конфига"
mkdir -p "$CONFIG_DIR"
note "$CONFIG_DIR"
# Шаблон окружения для юнитов (CHAT_CHANNEL, CHAT_PORT, BRACKET_URL и прочее).
# Секретов в нём нет — только пояснения и закомментированные примеры.
ENV_EXAMPLE="$CONFIG_DIR/env.example"
if [ ! -e "$ENV_EXAMPLE" ]; then
    cat >"$ENV_EXAMPLE" <<'ENVEOF'
# Окружение для юнитов пульта (читается как EnvironmentFile).
# Рабочий файл — ~/.config/stream-panel/env; создайте его копией этого шаблона
# и раскомментируйте нужное. Секреты сюда не кладите.
#
# Канал Twitch для чат-релея и порт страницы чата:
#CHAT_CHANNEL=имя-канала
#CHAT_PORT=8097
#
# Адрес турнирной страницы, которую снимает обновлятор сетки:
#BRACKET_URL=https://<хост>/<турнир>
ENVEOF
    chmod 644 "$ENV_EXAMPLE"
    note "создан шаблон env.example"
else
    note "env.example уже есть — не трогаю"
fi

# Рабочие файлы из шаблонов *.example.json (config.json и подобные). Существующие
# рабочие файлы НЕ перезаписываются: в них могут быть настоящие токены.
found=0
copied=0
while IFS= read -r src; do
    [ -n "$src" ] || continue
    found=$((found + 1))
    dst="${src%.example.json}.json"
    if [ -e "$dst" ]; then
        note "шаблон найден, рабочий файл уже есть — не трогаю: ${dst#"$ROOT"/}"
    else
        cp "$src" "$dst"
        chmod 600 "$dst"
        note "создан из шаблона: ${dst#"$ROOT"/} (права 600)"
        copied=$((copied + 1))
    fi
done < <(find "$ROOT" -name '*.example.json' \
              -not -path '*/.git/*' -not -path '*/node_modules/*' \
              -not -path '*/.venv/*' 2>/dev/null)
if [ "$found" = 0 ]; then
    note "шаблонов *.example.json не найдено — пропускаю"
elif [ "$copied" = 0 ]; then
    note "все рабочие файлы уже на месте"
fi
say

# --- 4. юниты systemd ---------------------------------------------------------
say "4/5 юниты systemd (пользовательские)"
mkdir -p "$UNIT_DIR"
count=0
for f in "$ROOT"/deploy/systemd/*.service "$ROOT"/deploy/systemd/*.timer; do
    [ -f "$f" ] || continue
    cp "$f" "$UNIT_DIR/$(basename "$f")"
    count=$((count + 1))
done
note "скопировано юнитов: $count -> $UNIT_DIR"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
    note "systemd перечитал конфигурацию (daemon-reload)"
fi
if [ "$ENABLE" = 1 ]; then
    for f in "$ROOT"/deploy/systemd/*.service "$ROOT"/deploy/systemd/*.timer; do
        [ -f "$f" ] || continue
        name="$(basename "$f")"
        systemctl --user enable "$name" >/dev/null 2>&1 \
            && note "включён в автозапуск: $name" \
            || note "не удалось включить: $name"
    done
    note "включены только что; запуск — tools/panel.sh up"
else
    note "юниты НЕ включены: стрим-стек поднимается вручную (tools/panel.sh up)"
fi
say

# --- 5. итог ------------------------------------------------------------------
say "5/5 готово"
note "поднять пульт:            tools/panel.sh up"
note "состояние:                tools/panel.sh status"
note "погасить всё перед игрой: tools/game-mode.sh on"
note "вернуть как было:         tools/game-mode.sh off"
note "автозапуск (по желанию):  tools/install.sh --enable"
say "Готово. Каталог конфига: $CONFIG_DIR"
