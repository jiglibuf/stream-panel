#!/usr/bin/env python3
"""Возвращает звук игры в сцены, где его пропустил сборщик, и проверяет фокус.

1) добавляет источник «Звук системы» в сцены аналитики, если его там нет
   (его пропускал сборщик, и звука на этих сценах не было);
2) ненадолго переключается на сцену с фокусом на своей карте и смотрит, снимает ли
   Lua-скрипт фокуса мьют со «Звук системы», затем возвращает прежнюю сцену.

Параметры — из окружения: REDZONE_OBS_WS_CONFIG, REDZONE_AUDIO_SYSTEM_SOURCE,
REDZONE_ANALYTICS_SCENES, REDZONE_FOCUS_SCENES (списки через запятую).
"""
import base64
import hashlib
import json
import os
import time
import uuid

from websockets.sync.client import connect

CFG = os.environ.get("REDZONE_OBS_WS_CONFIG",
                     os.path.expanduser("~/.config/obs-studio/plugin_config/obs-websocket/config.json"))
SYSTEM = os.environ.get("REDZONE_AUDIO_SYSTEM_SOURCE", "Звук системы")
ANALYTICS = tuple(s for s in os.environ.get("REDZONE_ANALYTICS_SCENES", "").split(",") if s)
TEST_SCENES = tuple(s for s in os.environ.get("REDZONE_FOCUS_SCENES", "").split(",") if s)


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
                    print(f"    !! {rt}: {st.get('comment')}")
                return m["d"].get("responseData", {})

    start_scene = req("GetCurrentProgramScene").get("currentProgramSceneName")
    print(f"текущая сцена: {start_scene}")

    if ANALYTICS:
        print("\n1) добавляю источник звука в сцены аналитики")
        for sc in ANALYTICS:
            items = [i["sourceName"] for i in req("GetSceneItemList", sceneName=sc).get("sceneItems", [])]
            if SYSTEM in items:
                print(f"    уже есть: {sc}")
            elif req("CreateSceneItem", sceneName=sc, sourceName=SYSTEM, sceneItemEnabled=True):
                print(f"    добавлен: {sc}")
    else:
        print("\n1) сцены аналитики не заданы — пропускаю")

    if TEST_SCENES:
        print("\n2) проверяю, что фокус на своей карте включает звук (Lua-скрипт фокуса)")
        for sc in TEST_SCENES:
            req("SetCurrentProgramScene", sceneName=sc)
            time.sleep(5)                      # сторож скрипта срабатывает раз в 3 с
            muted = req("GetInputMute", inputName=SYSTEM).get("inputMuted")
            print(f"    {sc}: «{SYSTEM}» {'ЗАГЛУШЕН — это ошибка' if muted else 'звучит ✓'}")
    else:
        print("\n2) сцены фокуса не заданы — проверку пропускаю")

    req("SetCurrentProgramScene", sceneName=start_scene)
    print(f"\nвернул сцену: {start_scene}")
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
