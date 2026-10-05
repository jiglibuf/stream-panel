#!/usr/bin/env bash
# Звук в эфир — тумблер: ВЫКЛЮЧИТЬ и вернуть всё как было.
#
# Гасит сторож, возвращает прежний выход по умолчанию, уводит ВСЕ потоки из виртуальных
# выходов обратно в наушники, выгружает петли и сами выходы, возвращает «Звук системы»
# на монитор наушников и убирает источник «Дискорд».
set -euo pipefail

STATE_DIR="${REDZONE_STATE_DIR:-$HOME/.local/state/stream-panel/redzone}"
STATE="$STATE_DIR/mix.state"
DEVICE_FILE="${REDZONE_AUDIO_DEVICE_FILE:-$STATE_DIR/device}"
OBS_WS_CONFIG="${REDZONE_OBS_WS_CONFIG:-$HOME/.config/obs-studio/plugin_config/obs-websocket/config.json}"
UNIT="${REDZONE_AUDIO_UNIT:-rz-mix.service}"
HINT="${REDZONE_AUDIO_HEADPHONES_HINT:-}"
SINK="${REDZONE_AUDIO_ALL_SINK:-rz_mix}"
GAME_SINK="${REDZONE_AUDIO_GAME_SINK:-game_mix}"
DISCORD_SINK="${REDZONE_AUDIO_DISCORD_SINK:-discord_mix}"
SINKS=("$SINK" "$GAME_SINK" "$DISCORD_SINK")
SYSTEM_SRC="${REDZONE_AUDIO_SYSTEM_SOURCE:-Звук системы}"
DISCORD_SRC="${REDZONE_AUDIO_DISCORD_SOURCE:-Дискорд}"

[ -f "$STATE" ] || { echo "Разделение звука не включено (нет $STATE) — нечего выключать."; exit 0; }
prev_default="$(sed -n 's/^prev_default=//p' "$STATE")"
obs_device="$(sed -n 's/^obs_device=//p' "$STATE")"
headphones="$(sed -n 's/^headphones=//p' "$STATE")"
[ -n "$prev_default" ] || prev_default="$headphones"
[ -n "$obs_device" ] || obs_device="$headphones.monitor"

echo "1/4 останавливаю сторож…"
systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true

echo "2/4 возвращаю выход по умолчанию: $prev_default…"
# Наушников может не быть (зарядка, отвалился Bluetooth). Тогда pactl возвращает ошибку,
# а с set -e на этом шаге падал весь скрипт — миксы оставались включёнными.
if ! pactl set-default-sink "$prev_default" 2>/dev/null; then
    chosen="$(head -n1 "$DEVICE_FILE" 2>/dev/null || true)"
    fallback="$(LC_ALL=C pactl list short sinks | awk -F'\t' -v want="$chosen" -v hint="$HINT" '
        $2 ~ /^(rz_mix|game_mix|discord_mix|easyeffects_sink)$/ || $2 ~ /monitor$/ {next}
        { if (want != "" && $2 == want) {print $2; exit}
          if (hint != "" && index($2, hint) && best == "") best = $2
          if (first == "") first = $2 }
        END { print (best != "" ? best : first) }')"
    if [ -n "$fallback" ]; then
        echo "    $prev_default недоступен — беру $fallback"
        pactl set-default-sink "$fallback"
        prev_default="$fallback"
    else
        echo "    !! ни наушников, ни другого выхода нет — оставляю как есть"
    fi
fi

echo "3/4 увожу потоки из виртуальных выходов в $prev_default…"
python3 - "$prev_default" "$SINK" "$GAME_SINK" "$DISCORD_SINK" <<'PY'
import os, re, subprocess, sys

target = sys.argv[1]
names = tuple(sys.argv[2:5])
env = {**os.environ, "LANG": "C"}       # окружение наследуем, иначе pactl не видит PipeWire
short = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, env=env).stdout
ids = {l.split("\t")[0]: l.split("\t")[1] for l in short.splitlines() if "\t" in l and l.split("\t")[1] in names}
if not ids:
    print("    виртуальных выходов уже нет — переносить нечего")
    sys.exit(0)
out = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, env=env).stdout
moved = 0
for blk in out.split("Sink Input #")[1:]:
    sid = blk.split("\n", 1)[0].strip()
    sink = re.search(r"^\tSink: (\d+)", blk, re.M)
    if sink and ids.get(sink.group(1)):
        app = re.search(r'application\.name = "(.*?)"', blk)
        subprocess.run(["pactl", "move-sink-input", sid, target], env=env)
        print("    перенёс:", app.group(1) if app else sid)
        moved += 1
print("    перенесено потоков:", moved)
PY

echo "4/4 выгружаю петли и виртуальные выходы…"
python3 - "$SINK" "$GAME_SINK" "$DISCORD_SINK" <<'PY'
import os, subprocess, sys

names = tuple(sys.argv[1:4])
env = {**os.environ, "LANG": "C"}
mods = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True, env=env).stdout
for line in mods.splitlines():
    if not any(n in line for n in names):
        continue
    if "module-loopback" in line:
        subprocess.run(["pactl", "unload-module", line.split("\t")[0]], env=env)
        print("    выгружена петля:", line.split("source=")[-1].split()[0] if "source=" in line else "?")
    elif "module-null-sink" in line:
        subprocess.run(["pactl", "unload-module", line.split("\t")[0]], env=env)
        print("    выгружен выход:", line.split("sink_name=")[-1].split()[0] if "sink_name=" in line else "?")
PY

# Монитора наушников может не быть (они на зарядке) — тогда берём монитор текущего выхода.
obs_monitor="$obs_device"
if ! LC_ALL=C pactl list short sources | awk -F'\t' '{print $2}' | grep -qx "$obs_monitor"; then
    obs_monitor="$prev_default.monitor"
    echo "    монитор наушников недоступен — «Звук системы» смотрит на $obs_monitor"
fi

python3 - "$obs_monitor" "$OBS_WS_CONFIG" "$SYSTEM_SRC" "$DISCORD_SRC" <<'PY'
import base64, hashlib, json, sys, uuid
from websockets.sync.client import connect

device, cfg_path, system_src, discord_src = sys.argv[1:5]
cfg = json.load(open(cfg_path))
pw, port = cfg.get("server_password", ""), cfg.get("server_port", 4455)


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
            st = m["d"]["requestStatus"]
            print(("    ok  " if st["result"] else "    !!  ") + rt +
                  ("" if st["result"] else " -> " + str(st.get("comment"))))
            return m["d"].get("responseData", {})


req("SetInputSettings", inputName=system_src, inputSettings={"device_id": device}, overlay=True)
if (req("GetInputSettings", inputName=discord_src) or {}).get("inputSettings") is not None:
    req("RemoveInput", inputName=discord_src)     # убирается из всех сцен сразу
ws.close()
PY

rm -f "$STATE"
echo "готово — как было до разделения (захват монитора наушников снова заблокирован OBS)."
