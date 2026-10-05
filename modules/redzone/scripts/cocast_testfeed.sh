#!/usr/bin/env bash
# Раздача тестовой картинки друзьям — чтобы проверить их плеер и сеть, не поднимая эфир.
#
# Публикует в хаб ту же картинку с часами, что и самопроверка, и держит её заданное время.
# В Twitch при этом не уходит ничего: релей отдаёт поток только в боевом режиме (когда есть
# файл с адресом Twitch), а без него просто ждёт появления микса.
#
# Запуск:    cocast_testfeed.sh 15          # держать 15 минут
# Остановка: systemctl --user stop rz-testfeed   # или дождаться конца
#
# Параметры — из окружения: REDZONE_HUB_API, REDZONE_HUB_RTMP, REDZONE_COCAST_TARGET,
# REDZONE_BRACKET_FILTER (фильтр, рисующий часы на картинке).
set -euo pipefail

MINUTES="${1:-15}"
API="${REDZONE_HUB_API:-http://127.0.0.1:9997}"
HUB="${REDZONE_HUB_RTMP:-rtmp://127.0.0.1:1935}/program"
TARGET="${REDZONE_COCAST_TARGET:-$HOME/.local/state/stream-panel/redzone/twitch-target.json}"
FILTER="${REDZONE_TESTPATTERN_FILTER:-$(dirname "$0")/testpattern_vf.filter}"
PY=/usr/bin/python3
FFMPEG="$(command -v ffmpeg)"
CURL="$(command -v curl)"

[ -n "$FFMPEG" ] || { echo "нет ffmpeg в PATH" >&2; exit 1; }
[ -f "$FILTER" ] || { echo "нет $FILTER (рисует часы на картинке)" >&2; exit 1; }

# 1. Боевой режим включён — тестовая картинка могла бы оказаться в раздаче как чужой
#    публикатор. Не рискуем: сначала выключить каст.
if [ -f "$TARGET" ]; then
  echo "включён боевой режим совместного каста — сначала: cocast off" >&2
  exit 1
fi

# 2. В program уже кто-то публикует (например, сам OBS) — не подменяем.
busy=$("$CURL" -s -m 8 "$API/v3/paths/list" | "$PY" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit
for item in data.get("items", []):
    if item.get("name") == "program" and item.get("source"):
        print(item["source"])
        break
') || true
if [ -n "$busy" ]; then
  echo "в program уже публикует: $busy — тестовую картинку не подменяю" >&2
  exit 1
fi

# 3. Релей обязан молчать: он ждёт боевого режима, поэтому картинка останется в хабе.
echo "  раздача тестовой картинки на $MINUTES мин"
echo "  в Twitch ничего не уходит: релей включится только в боевом режиме"

exec "$FFMPEG" -hide_banner -loglevel warning -nostdin -re \
  -f lavfi -i "testsrc2=size=1280x720:rate=30" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -vf "$(cat "$FILTER")" \
  -c:v h264_nvenc -preset p4 -b:v 4000k -g 60 \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -t "$((MINUTES * 60))" \
  -f flv "$HUB"
