#!/usr/bin/env bash
# Звук в эфир — тумблер: ВКЛЮЧИТЬ.
#
# Зачем вообще: OBS отказывается читать монитор устройства, на которое сам выводит
# мониторинг («Prevented feedback-loop»). Обходим виртуальными выходами: они становятся
# выходом по умолчанию, а звук из них петлёй возвращается в наушники.
#
# Что получается (подробности — в rz_mix_watch.py):
#   игра    → game_mix    → источник «Звук системы»  (глушится фокусом на чужой карте)
#   Discord → discord_mix → источник «Дискорд»       (не глушится никогда)
#   прочее  → rz_mix      → в эфир не идёт
#
# Держит это служба-сторож; состояние пишется в каталог состояния, чтобы выключение
# вернуло всё как было. Параметры берутся из окружения (их ставит модуль RedZone).

set -euo pipefail

STATE_DIR="${REDZONE_STATE_DIR:-$HOME/.local/state/stream-panel/redzone}"
STATE="$STATE_DIR/mix.state"
DEVICE_FILE="${REDZONE_AUDIO_DEVICE_FILE:-$STATE_DIR/device}"
SINK="${REDZONE_AUDIO_ALL_SINK:-rz_mix}"
UNIT="${REDZONE_AUDIO_UNIT:-rz-mix.service}"
HINT="${REDZONE_AUDIO_HEADPHONES_HINT:-}"
mkdir -p "$STATE_DIR"

# Устройство для петель, по убыванию доверия: выбор владельца (файл device), затем
# гарнитура по образцу, затем первый физический выход. Виртуальные выходы не берём.
pick_device() {
    local chosen=""
    [ -f "$DEVICE_FILE" ] && chosen="$(head -n1 "$DEVICE_FILE")"
    LC_ALL=C pactl list short sinks | awk -F'\t' -v want="$chosen" -v hint="$HINT" '
        $2 ~ /^(rz_mix|game_mix|discord_mix|easyeffects_sink)$/ || $2 ~ /monitor$/ {next}
        { if (want != "" && $2 == want) {print $2; exit}
          if (hint != "" && index($2, hint) && best == "") best = $2
          if (first == "") first = $2 }
        END { print (best != "" ? best : first) }'
}

HEADPHONES="$(pick_device)"
[ -n "$HEADPHONES" ] || { echo "не нашёл ни одного физического выхода — звук не поднять" >&2; exit 1; }
OBS_DEVICE="$HEADPHONES.monitor"          # к этому устройству вернём «Звук системы» при выключении

prev_default=""
[ -f "$STATE" ] && prev_default="$(sed -n 's/^prev_default=//p' "$STATE")"
if [ "$prev_default" = "$SINK" ] || [ "$prev_default" = "${REDZONE_AUDIO_GAME_SINK:-game_mix}" ] \
   || [ "$prev_default" = "${REDZONE_AUDIO_DISCORD_SINK:-discord_mix}" ]; then
    prev_default="$HEADPHONES"            # уже стояло наше значение — значит «как было» это наушники
fi

echo "1/2 поднимаю выходы, петли и выход по умолчанию (служба $UNIT)…"
systemctl --user enable --now "$UNIT"
sleep 8
echo "    выход по умолчанию: $(pactl get-default-sink)"
echo "    выходы: $(pactl list short sinks | awk '{printf "%s ", $2}')"

printf 'prev_default=%s\nobs_device=%s\nheadphones=%s\n' \
    "$prev_default" "$OBS_DEVICE" "$HEADPHONES" > "$STATE"
echo "2/2 состояние сохранено: $STATE"
