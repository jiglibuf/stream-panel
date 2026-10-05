#!/usr/bin/env bash
# Самопроверка схемы совместного каста — целиком и не выходя в эфир.
#
#   1) публикует в хаб тестовую картинку с часами (H.264 + AAC, как это делает OBS);
#   2) читает её так же, как будут читать друзья: с внешнего адреса хаба и ключом зрителя;
#   3) снимает кадр для замера задержки (сравнить часы в кадре со временем съёма);
#   4) прогоняет релей (копия потока без перекодирования) в тестовый путь хаба.
#
# Зачем: отличить «сломано у нас» от «сломано у друга» за полминуты, не поднимая эфир и не
# рискуя показать зрителям тестовую картинку. Ничего в Twitch не уходит.
#
# Параметры — из окружения: REDZONE_STREAM_HOST (адрес хаба, по которому придут друзья),
# REDZONE_HUB_API, REDZONE_HUB_RTMP, REDZONE_HUB_CREDENTIALS, REDZONE_TESTPATTERN_FILTER,
# REDZONE_COCAST_TARGET, REDZONE_HUB_VIEWER_USER.
set -euo pipefail

API="${REDZONE_HUB_API:-http://127.0.0.1:9997}"
HUB_RTMP="${REDZONE_HUB_RTMP:-rtmp://127.0.0.1:1935}"
HOST="${REDZONE_STREAM_HOST:?задай REDZONE_STREAM_HOST — адрес хаба, по которому придут друзья}"
CRED="${REDZONE_HUB_CREDENTIALS:-$HOME/.local/state/stream-panel/redzone/credentials.txt}"
FILTER="${REDZONE_TESTPATTERN_FILTER:-$(dirname "$0")/testpattern_vf.filter}"
VIEWER="${REDZONE_HUB_VIEWER_USER:-rzview}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=/usr/bin/python3
WORK="${TMPDIR:-/tmp}/cocast-selftest"
mkdir -p "$WORK"

pass=$(awk -v u="$VIEWER:" '$1==u{print $2}' "$CRED" | head -1)
[ -n "$pass" ] || { echo "в $CRED нет строки «$VIEWER:» — добавь ключ зрителя" >&2; exit 1; }

field() {   # $1 — путь, $2 — ready | tracks | readers | source
  curl -s -m 8 "$API/v3/paths/list" | $PY -c '
import json, sys
name, what = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    print("нет ответа от хаба"); sys.exit(0)
for item in data.get("items", []):
    if item.get("name") == name:
        if what == "tracks":
            print(",".join(item.get("tracks") or []) or "нет")
        elif what == "readers":
            print(len(item.get("readers") or []))
        elif what == "source":
            print(item.get("source") or "")
        else:
            print(item.get("ready"))
        break
else:
    print("нет пути")
' "$1" "$2"
}

PUB=""; RELAY=""; CARD=""
cleanup() {
  [ -n "$PUB" ] && kill "$PUB" 2>/dev/null || true
  [ -n "$RELAY" ] && kill "$RELAY" 2>/dev/null || true
  [ -n "$CARD" ] && kill "$CARD" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

if [ "$(field program source)" != "" ]; then
  echo "путь program уже занят ($(field program source)) — сначала останови публикатора" >&2
  exit 1
fi

echo "1/4 публикую тестовую картинку с часами в program (H.264 + AAC, как у OBS)"
ffmpeg -hide_banner -loglevel warning -nostdin -re \
  -f lavfi -i "testsrc2=size=1280x720:rate=30" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -vf "$(cat "$FILTER")" \
  -c:v h264_nvenc -preset p4 -b:v 4000k -g 60 \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -f flv "$HUB_RTMP/program" & PUB=$!
for _ in $(seq 1 15); do [ "$(field program ready)" = "True" ] && break; sleep 1; done
echo "     хаб увидел: ready=$(field program ready), треки: $(field program tracks)"

echo "2/4 читаю так же, как будут читать друзья: с $HOST, ключом $VIEWER (пароль не печатаю)"
probe=$(ffprobe -v error -rtsp_transport tcp \
  -show_entries stream=codec_name,width,height,sample_rate -of csv=p=0 \
  "rtsp://$VIEWER:$pass@$HOST:8554/program" 2>&1 | head -4 || true)
if [ -z "$probe" ]; then
  echo "     ЧТЕНИЕ НЕ УДАЛОСЬ — проверь ключ $VIEWER и доступность $HOST"
else
  printf '%s\n' "$probe" | sed 's/^/     /'
fi

echo "3/4 замер задержки: снимаю кадр из того, что видит друг"
now=$(date +%T)
ffmpeg -hide_banner -loglevel error -rtsp_transport tcp -y \
  -i "rtsp://$VIEWER:$pass@$HOST:8554/program" -frames:v 1 "$WORK/frame.png" 2>/dev/null || true
echo "     кадр снят в $now; файл: $WORK/frame.png (часы в кадре = момент отдачи, разница = задержка)"

echo "4/5 релей: копирую поток из хаба в тестовый путь (в Twitch НЕ идём)"
COCAST_OUTPUT="$HUB_RTMP/programcopy" timeout 14 "$HERE/cocast_relay.sh" & RELAY=$!
for _ in $(seq 1 12); do [ "$(field programcopy ready)" = "True" ] && break; sleep 1; done
echo "     programcopy: ready=$(field programcopy ready), треки: $(field programcopy tracks)"
echo "     поток читает: $(field program readers) подписчиков"

echo "5/5 доступ: ключом зрителя читается общий эфир, но НЕ карты друзей"
ffmpeg -hide_banner -loglevel error -nostdin -re \
  -f lavfi -i "testsrc2=size=640x360:rate=30" -c:v h264_nvenc -f flv "$HUB_RTMP/card2" & CARD=$!
for _ in $(seq 1 10); do [ "$(field card2 ready)" = "True" ] && break; sleep 1; done
for path in program card2; do
  out=$(timeout 8 ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name -of csv=p=0 \
        "rtsp://$VIEWER:$pass@$HOST:8554/$path" 2>&1 | head -2 | tr '\n' ' ' \
        | sed -E "s#$VIEWER:[^@]*@#$VIEWER:***@#g" || true)
  if [ -z "$out" ]; then out="отказ в доступе (для card2 так и должно быть)"; fi
  printf '     %-8s → %s\n' "$path" "$out"
done
kill "$CARD" 2>/dev/null || true; wait "$CARD" 2>/dev/null || true; CARD=""

echo
echo "гашу тестовый поток и релей"
cleanup
trap - EXIT
sleep 2
src=$(field program source)
echo "     program: ${src:-свободен}"
