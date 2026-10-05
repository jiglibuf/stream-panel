#!/usr/bin/env python3
"""Аудит звука по сценам: какие звуковые источники лежат в каждой сцене и не заглушены ли.

Печатает таблицу: сцена → источники звука (с пометкой [ТИХО] для заглушённых).
Нужен, когда «в обычных сценах звук есть, а в какой-то конкретной нет».

Параметры — из окружения: REDZONE_OBS_WS_CONFIG, REDZONE_WATCH_SOURCES (через запятую).
"""
import base64
import hashlib
import json
import os
import uuid

from websockets.sync.client import connect

CFG = os.environ.get("REDZONE_OBS_WS_CONFIG",
                     os.path.expanduser("~/.config/obs-studio/plugin_config/obs-websocket/config.json"))
_WATCH = os.environ.get("REDZONE_WATCH_SOURCES",
                        "Звук системы,Микрофон,RZ К2 хаб,RZ К3 хаб,Алерты (фоллоу)")
WATCH = tuple(s for s in _WATCH.split(",") if s)


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
                return m["d"].get("responseData", {})

    print(f"текущая сцена: {req('GetCurrentProgramScene').get('currentProgramSceneName')}\n")
    for sc in req("GetSceneList").get("scenes", []):
        name = sc["sceneName"]
        items = req("GetSceneItemList", sceneName=name).get("sceneItems", [])
        found = []
        for it in items:
            src = it["sourceName"]
            if src not in WATCH:
                continue
            muted = req("GetInputMute", inputName=src).get("inputMuted")
            found.append(f"{src}{' [ТИХО]' if muted else ''}")
        mark = "!!" if ("RZ" in name or "аналит" in name) and not found else "  "
        print(f"{mark} {name:32s} {'; '.join(found) if found else '— звуковых источников нет'}")
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
