#!/usr/bin/env python3
"""«Пропал звук в эфире» — быстрая проверка и лечение.

Что делает:
  1. смотрит состояние микрофона (мьют, громкость) в OBS;
  2. перещёлкивает мьют — лечит залипший флаг (API и коллекция расходятся);
  3. пишет несколько секунд программы и измеряет уровень звука в миксе.

Уровень −91 дБ в миксе при живом микрофоне = звук не доходит до выхода.

Параметры — из окружения: REDZONE_OBS_WS_CONFIG, REDZONE_AUDIO_MIC_SOURCE
(имя источника микрофона), REDZONE_RECORD_DIR (где искать запись, если OBS не отдал путь).
"""
import base64
import glob
import hashlib
import json
import os
import subprocess
import time
import uuid

from websockets.sync.client import connect

CFG = os.environ.get("REDZONE_OBS_WS_CONFIG",
                     os.path.expanduser("~/.config/obs-studio/plugin_config/obs-websocket/config.json"))
RECORD_DIR = os.environ.get("REDZONE_RECORD_DIR", os.path.expanduser("~/Videos"))
MIC = os.environ.get("REDZONE_AUDIO_MIC_SOURCE", "Микрофон")


def main():
    cfg = json.load(open(CFG))
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
    assert json.loads(ws.recv())["op"] == 2

    def req(rt, **dd):
        rid = str(uuid.uuid4())
        ws.send(json.dumps({"op": 6, "d": {"requestType": rt, "requestId": rid, "requestData": dd}}))
        while True:
            m = json.loads(ws.recv())
            if m["op"] == 7 and m["d"]["requestId"] == rid:
                st = m["d"]["requestStatus"]
                if not st["result"]:
                    print(f"  ! {rt}: {st.get('comment')}")
                    return None
                return m["d"].get("responseData", {})

    if (req("GetRecordStatus") or {}).get("outputActive"):
        print("запись уже идёт — измерять не буду")
        ws.close()
        return 0

    dev = (req("GetInputSettings", inputName=MIC) or {}).get("inputSettings", {}).get("device_id")
    muted = req("GetInputMute", inputName=MIC) or {}
    vol = req("GetInputVolume", inputName=MIC) or {}
    print(f"микрофон OBS: {dev}")
    print(f"  мьют={muted.get('inputMuted')} громкость={vol.get('inputVolumeDb')} дБ")

    req("SetInputMute", inputName=MIC, inputMuted=True)
    time.sleep(0.3)
    req("SetInputMute", inputName=MIC, inputMuted=False)
    print("  мьют перещёлкнут (лечение залипания)")

    before = set(glob.glob(os.path.join(RECORD_DIR, "*"))) if os.path.isdir(RECORD_DIR) else set()
    print("пишу 5 секунд программы…")
    req("StartRecord")
    time.sleep(5)
    stop = req("StopRecord") or {}
    time.sleep(1)
    ws.close()

    new = sorted(set(glob.glob(os.path.join(RECORD_DIR, "*"))) - before,
                 key=os.path.getmtime, reverse=True) if os.path.isdir(RECORD_DIR) else []
    path = stop.get("outputPath") or (new[0] if new else None)
    if not path or not os.path.exists(path):
        print("  запись не удалась — смотри лог OBS")
        return 1
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=180)
    lv = {}
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            lv["mean"] = line.split(":", 1)[1].strip()
        if "max_volume" in line:
            lv["max"] = line.split(":", 1)[1].strip()
    print(f"  микс программы: mean {lv.get('mean')}, max {lv.get('max')}")
    try:
        worst = float(lv.get("max", "-99").split()[0])
    except ValueError:
        worst = -99.0
    if worst < -55.0:
        print(f"  → В МИКСЕ ТИШИНА ({worst} дБ). Проверь: устройство микрофона, добавлен ли "
              f"«Звук системы» в сцену, не замьючен ли источник, играет ли звук на рабочем столе")
        return 2
    print("  → звук в миксе есть ✓")
    os.remove(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
