#!/usr/bin/env python3
"""Следит за состоянием мьюта микрофона и событиями OBS, чтобы найти, кто его включает.

Подписывается на события и раз в 3 секунды сверяет мьют и текущую сцену.
Запуск: watch_mic_mute.py [секунд]
Параметры из окружения: REDZONE_OBS_WS_CONFIG, REDZONE_AUDIO_MIC_SOURCE.
"""
import base64
import hashlib
import json
import os
import sys
import time
import uuid

from websockets.sync.client import connect

CFG = os.environ.get("REDZONE_OBS_WS_CONFIG",
                     os.path.expanduser("~/.config/obs-studio/plugin_config/obs-websocket/config.json"))
MIC = os.environ.get("REDZONE_AUDIO_MIC_SOURCE", "Микрофон")


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    cfg = json.load(open(CFG))
    pw, port = cfg.get("server_password", ""), cfg.get("server_port", 4455)

    def auth(salt, challenge):
        sec = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
        return base64.b64encode(hashlib.sha256((sec + challenge).encode()).digest()).decode()

    ws = connect(f"ws://127.0.0.1:{port}", subprotocols=["obswebsocket.json"], max_size=None,
                 open_timeout=10)
    d = json.loads(ws.recv())["d"]
    ident = {"rpcVersion": 1, "eventSubscriptions": 0x7FF}   # все события
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
                return m["d"].get("responseData", {})

    req("SetInputMute", inputName=MIC, inputMuted=False)
    print(f"мьют снят, слушаю {seconds} с…")
    t0 = time.time()
    last = None
    while time.time() - t0 < seconds:
        try:
            m = json.loads(ws.recv(timeout=3))
        except TimeoutError:
            m = None
        except Exception:
            break
        if m and m.get("op") == 5:
            ev = m["d"].get("eventType", "")
            data = m["d"].get("eventData", {})
            if ev == "InputMuteStateChanged":
                print(f"  [{time.time()-t0:5.1f}s] СОБЫТИЕ InputMuteStateChanged: "
                      f"{data.get('inputName')} muted={data.get('inputMuted')}")
            elif ev in ("CurrentProgramSceneChanged", "SceneItemEnableStateChanged"):
                print(f"  [{time.time()-t0:5.1f}s] {ev}: {json.dumps(data, ensure_ascii=False)[:90]}")
        cur = req("GetInputMute", inputName=MIC).get("inputMuted")
        sc = req("GetCurrentProgramScene").get("currentProgramSceneName")
        if (cur, sc) != last:
            print(f"  [{time.time()-t0:5.1f}s] состояние: мьют={cur}, сцена='{sc}'")
            last = (cur, sc)
    print("готово")
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
