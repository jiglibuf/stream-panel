#!/usr/bin/env bash
# Куда попадает звук: отчёт по всем потокам воспроизведения.
#
# Отвечает на вопросы «почему в OBS тишина» и «кто перехватывает звук»: печатает
# приложение → выход, а с ключом --probe ещё и проверяет, куда попадёт поток, играющий
# «по умолчанию» (как это делает голосовой чат без явного устройства).
#
# Запуск: modules/redzone/scripts/check_audio_route.sh [--probe]
set -euo pipefail

PY=/usr/bin/python3
WORK="${TMPDIR:-/tmp}/rz-route"; mkdir -p "$WORK"

report() {
  LC_ALL=C pactl list sink-inputs > "$WORK/inputs.txt" 2>/dev/null || true
  LC_ALL=C pactl list short sinks > "$WORK/sinks.txt" 2>/dev/null || true
  "$PY" - "$WORK/inputs.txt" "$WORK/sinks.txt" <<'PY'
import re, sys
inputs = open(sys.argv[1]).read()
sinks = {}
for line in open(sys.argv[2]):
    f = line.split('\t')
    if len(f) > 1:
        sinks[f[0]] = f[1]
print("  выходы:", ", ".join(f"{i}={n}" for i, n in sinks.items()))
print()
for b in re.split(r'Sink Input #', inputs)[1:]:
    idx = b.split('\n', 1)[0].strip()
    app = re.search(r'application\.name = "(.+?)"', b)
    media = re.search(r'media\.name = "(.+?)"', b)
    sink = re.search(r'^\s*Sink: (\d+)', b, re.M)
    muted = re.search(r'^\s*Mute: (\w+)', b, re.M)
    name = app.group(1) if app else (media.group(1) if media else '?')
    sid = sink.group(1) if sink else '?'
    print(f"  #{idx:<8} {name[:34]:<34} → {sinks.get(sid, sid)}"
          + ("  [ЗАГЛУШЕН]" if muted and muted.group(1) == 'yes' else ""))
PY
}

if [ "${1:-}" = "--probe" ]; then
  echo "  проверка: играю тихий тон «по умолчанию» (как это делает голосовой чат)…"
  ffmpeg -hide_banner -loglevel error -y -f lavfi -i "sine=frequency=440:duration=10" \
    -ar 48000 -ac 2 "$WORK/tone.wav"
  paplay "$WORK/tone.wav" 2>/dev/null &
  P=$!
  sleep 3
  echo
  report
  echo
  echo "  ↑ если тон оказался не в миксе-сборщике — значит поток перехватывает чужая программа"
  kill "$P" 2>/dev/null || true
  wait "$P" 2>/dev/null || true
  rm -f "$WORK/tone.wav"
else
  report
fi
