#!/usr/bin/env bash
# Релей общего эфира: берёт готовый микс из хаба MediaMTX и отдаёт его в Twitch.
#
# Зачем: соведущим нужна та же картинка, что в эфире, с задержкой около секунды. С самого
# Twitch это невозможно (там буфер в несколько секунд), поэтому картинку раздаёт хаб, а в
# Twitch поток уходит отсюда.
#
# Кодирование НЕ трогается (-c copy): битрейт, качество и задержка те же, что у OBS.
# Читаю именно RTSP: SRT-чтение из этого хаба не работает (проверено на живых гостях).
# Адрес и ключ Twitch лежат в файле с правами 600 (его кладёт `cocast on`) — в этом файле
# их нет, чтобы ключ не попал ни в репозиторий, ни в логи.
#
# Параметры — из окружения: REDZONE_COCAST_TARGET, REDZONE_HUB_API, REDZONE_HUB_RTSP.
set -euo pipefail

PY=/usr/bin/python3                       # тут нужен только json из стандартной библиотеки
TARGET="${REDZONE_COCAST_TARGET:-$HOME/.local/state/stream-panel/redzone/twitch-target.json}"
API="${REDZONE_HUB_API:-http://127.0.0.1:9997}"
INPUT="${COCAST_INPUT:-${REDZONE_HUB_RTSP:-rtsp://127.0.0.1:8554}/program}"

hub_ready() {   # yes, когда микс реально отдаётся в хабе
  curl -s -m 5 "$API/v3/paths/list" | $PY -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("no"); sys.exit(0)
print("yes" if any(p.get("name") == "program" and p.get("ready") for p in data.get("items", [])) else "no")'
}

# Службу можно держать включённой постоянно: она молчит, пока нет ни адреса Twitch, ни
# микса в хабе. А если машина перезагрузилась в режиме совместного каста — вернёт эфир
# сама, как только OBS снова начнёт отдавать.
while :; do
  if [ -f "$TARGET" ] || [ -n "${COCAST_OUTPUT:-}" ]; then
    [ "$(hub_ready)" = "yes" ] && break
  fi
  sleep 3
done

# Куда отдавать: боевой адрес берём из файла с ключом, а COCAST_OUTPUT (только для локальных
# проверок) позволяет прогнать цепочку в тестовый путь хаба, не выходя в эфир.
if [ -n "${COCAST_OUTPUT:-}" ]; then
  OUTPUT="$COCAST_OUTPUT"
  echo "релей (проверка): читаю $INPUT и отдаю в $OUTPUT"
else
  [ -f "$TARGET" ] || { echo "нет $TARGET — сначала: cocast on" >&2; exit 1; }
  mapfile -t target < <("$PY" -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["server"]);print(d["key"])' "$TARGET")
  SERVER="${target[0]:?в файле нет адреса Twitch}"
  KEY="${target[1]:?в файле нет ключа Twitch}"
  OUTPUT="${SERVER}/${KEY}"
  echo "релей: читаю $INPUT и отдаю в ${SERVER} (поток копируется, без перекодирования)"
fi

# -rtsp_transport tcp — меньше потерь на локальном сокете;
# no_duration_filesize — чтобы FLV жил как непрерывный поток, без «длительности».
exec ffmpeg -hide_banner -loglevel warning -nostdin \
  -rtsp_transport tcp \
  -i "$INPUT" \
  -c copy -f flv -flvflags no_duration_filesize \
  "$OUTPUT"
