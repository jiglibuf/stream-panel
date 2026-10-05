#!/usr/bin/env bash
# Проверка «звук реально доходит до наушников».
#
# Отвечает на вопрос: тишина в ушах — это порванная петля или просто нечего играть?
# Играет тихий тон в микс-сборщик, пишет монитор наушников и меряет уровень записи.
# Тон короткий и тихий (2 с, 12%) — слышно, но не пугает.
#
# Запуск: modules/redzone/scripts/check_phones.sh [имя-выхода-наушников]
set -euo pipefail

STATE_DIR="${REDZONE_STATE_DIR:-$HOME/.local/state/stream-panel/redzone}"
DEVICE_FILE="${REDZONE_AUDIO_DEVICE_FILE:-$STATE_DIR/device}"
SINK="${REDZONE_AUDIO_ALL_SINK:-rz_mix}"
HINT="${REDZONE_AUDIO_HEADPHONES_HINT:-}"
WORK="${TMPDIR:-/tmp}/rz-phones"
mkdir -p "$WORK"
PY=/usr/bin/python3

pick() {
    local chosen=""
    [ -f "$DEVICE_FILE" ] && chosen="$(head -n1 "$DEVICE_FILE")"
    LC_ALL=C pactl list short sinks | awk -F'\t' -v want="$chosen" -v hint="$HINT" '
        $2 ~ /^(rz_mix|game_mix|discord_mix|easyeffects_sink)$/ || $2 ~ /monitor$/ {next}
        { if (want != "" && $2 == want) {print $2; exit}
          if (hint != "" && index($2, hint) && best == "") best = $2
          if (first == "") first = $2 }
        END { print (best != "" ? best : first) }'
}

PHONES="${1:-$(pick)}"
if [ -z "$PHONES" ] || ! pactl list short sinks | grep -qF "$PHONES"; then
  echo "выход не найден: ${PHONES:-<нет физического выхода>}" >&2
  echo "подключённые выходы:" >&2
  LC_ALL=C pactl list short sinks | cut -f2 | sed 's/^/  /' >&2
  exit 1
fi

ffmpeg -hide_banner -loglevel error -y -f lavfi -i "sine=frequency=440:duration=2" \
  -af volume=0.12 -ar 48000 -ac 2 "$WORK/tone.wav"

timeout 5 parecord --device="$PHONES.monitor" --file-format=wav --channels=2 --rate=48000 \
  "$WORK/rec.wav" & REC=$!
sleep 0.8
if command -v pw-play >/dev/null; then
  pw-play --target="$SINK" "$WORK/tone.wav" >/dev/null 2>&1 || true
else
  paplay --device="$SINK" "$WORK/tone.wav" >/dev/null 2>&1 || true
fi
wait "$REC" 2>/dev/null || true

out=$(ffmpeg -hide_banner -i "$WORK/rec.wav" -af volumedetect -f null - 2>&1 \
      | grep -E "mean_volume|max_volume" || true)
echo "  записал монитор выхода $PHONES:"
printf '%s\n' "$out" | sed 's/^/    /'

max=$(printf '%s\n' "$out" | grep max_volume | grep -oE '\-?[0-9.]+' | head -1)
if [ -z "$max" ]; then
  echo "  ВЕРДИКТ: запись не удалась — проверь parecord и устройство" >&2
  exit 1
fi
"$PY" - "$max" <<'PY'
import sys
max_db = float(sys.argv[1])
if max_db > -60:
    print("  ВЕРДИКТ: звук доходит до наушников ✓")
else:
    print(f"  ВЕРДИКТ: тишина ({max_db:.1f} дБ) — петля не подключена ✗")
    sys.exit(1)
PY
rm -f "$WORK/tone.wav" "$WORK/rec.wav"
