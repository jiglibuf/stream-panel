#!/usr/bin/env bash
# Проверка разделения звука: захват не заблокирован, игра слышна и в наушниках, и в эфире.
set -uo pipefail

STATE_DIR="${REDZONE_STATE_DIR:-$HOME/.local/state/stream-panel/redzone}"
DEVICE_FILE="${REDZONE_AUDIO_DEVICE_FILE:-$STATE_DIR/device}"
SINK="${REDZONE_AUDIO_ALL_SINK:-rz_mix}"
HINT="${REDZONE_AUDIO_HEADPHONES_HINT:-}"
OBS_WS_CONFIG="${REDZONE_OBS_WS_CONFIG:-$HOME/.config/obs-studio/plugin_config/obs-websocket/config.json}"
SYSTEM_SRC="${REDZONE_AUDIO_SYSTEM_SOURCE:-Звук системы}"
HERE="$(cd "$(dirname "$0")" && pwd)"

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

echo "=== виртуальные выходы ==="
LC_ALL=C pactl list short sinks | grep -E "rz_mix|game_mix|discord_mix" || \
  echo "  миксов нет (режим разделения не включён)"

echo
echo "=== захват «$SYSTEM_SRC» в OBS ==="
OBS_WS_CONFIG="$OBS_WS_CONFIG" SYSTEM_SRC="$SYSTEM_SRC" python3 - <<'PY'
import os, re, subprocess

env = {**os.environ, "LANG": "C"}
srcs = subprocess.run(["pactl", "list", "short", "sources"],
                      capture_output=True, text=True, env=env).stdout
names = {p[0]: p[1] for p in (l.split("\t", 2) for l in srcs.splitlines()) if len(p) >= 2}
out = subprocess.run(["pactl", "list", "source-outputs"],
                     capture_output=True, text=True, env=env).stdout
need = os.environ["SYSTEM_SRC"]
found = False
for blk in out.split("Source Output #")[1:]:
    if f'media.name = "{need}"' not in blk:
        continue
    found = True
    m = re.search(r"^\tSource: (\d+)", blk, re.M)
    if not m or m.group(1) == "4294967295":
        print("  захват НЕ подключён — OBS заблокировал его (feedback-loop)")
    else:
        print(f"  захват читает: {names.get(m.group(1), m.group(1))}")
if not found:
    print(f"  потока захвата нет — источник «{need}» не активен")
PY

echo
echo "=== громкости ключевых источников (тишина бывает и от сдвинутого фейдера) ==="
OBS_WS_CONFIG="$OBS_WS_CONFIG" SYSTEM_SRC="$SYSTEM_SRC" python3 - <<'PY'
import base64, hashlib, json, os, uuid
from websockets.sync.client import connect

cfg = json.load(open(os.environ["OBS_WS_CONFIG"]))
pw, port = cfg.get("server_password", ""), cfg.get("server_port", 4455)
system = os.environ["SYSTEM_SRC"]


def auth(salt, challenge):
    sec = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((sec + challenge).encode()).digest()).decode()


ws = connect(f"ws://127.0.0.1:{port}", subprotocols=["obswebsocket.json"], max_size=None)
d = json.loads(ws.recv())["d"]
ident = {"rpcVersion": 1, "eventSubscriptions": 0}
if d.get("authentication"):
    ident["authentication"] = auth(d["authentication"]["salt"], d["authentication"]["challenge"])
ws.send(json.dumps({"op": 1, "d": ident}))
json.loads(ws.recv())


def req(rt, **dd):
    rid = str(uuid.uuid4())
    ws.send(json.dumps({"op": 6, "d": {"requestType": rt, "requestId": rid, "requestData": dd}}))
    while True:
        m = json.loads(ws.recv())
        if m["op"] == 7 and m["d"]["requestId"] == rid:
            return m["d"].get("responseData", {})


for name in (system, "Микрофон"):
    try:
        db = req("GetInputVolume", inputName=name).get("inputVolumeDb", 0.0)
        muted = req("GetInputMute", inputName=name).get("inputMuted")
    except Exception:
        print(f"  {name}: источника нет")
        continue
    flag = "  ← ЗАГЛУШЕН" if muted else ("  ← фейдер почти в тишине!" if db is not None and db < -40 else "")
    print(f"  {name:16s} {db:8.2f} дБ{flag}")
ws.close()
PY

echo
echo "=== что реально отдаётся ==="
for src in "$SINK.monitor" "$(pick).monitor"; do
    [ -z "${src%.monitor}" ] && continue
    lvl="$(timeout 20 ffmpeg -hide_banner -f pulse -i "$src" -t 4 -af volumedetect -f null - 2>&1 \
           | grep -oP 'max_volume: \K.*' | head -1)"
    printf '  %-70s %s\n' "$src" "${lvl:-нет данных}"
done

echo
echo "=== эфир (запись и замер) ==="
timeout 180 python3 "$HERE/mic_rescue.py" 2>&1 | tail -3
