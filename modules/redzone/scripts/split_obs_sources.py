#!/usr/bin/env python3
"""Разводит в OBS звук игры и голосового чата на два независимых источника.

Почему: на вкладках с чужими матчами скрипт фокуса глушит «Звук системы» целиком —
вместе с чужой игрой пропадали и комментарии из голосового чата.

  * «Звук системы» переключается с монитора наушников на монитор game_mix
    (туда сторож заворачивает игру) — её и глушит фокус, как раньше;
  * создаётся источник «Дискорд» на мониторе discord_mix и добавляется ВО ВСЕ сцены —
    его фокус не трогает, комментарии слышны везде;
  * монитор наушников не читается ни одним источником, поэтому двойного звука нет.

Запускать при живом OBS; эфир и запись не прерывает.

Параметры — из окружения: REDZONE_OBS_WS_CONFIG, REDZONE_AUDIO_GAME_SINK,
REDZONE_AUDIO_DISCORD_SINK, REDZONE_AUDIO_SYSTEM_SOURCE, REDZONE_AUDIO_DISCORD_SOURCE.
"""
import base64
import hashlib
import json
import os
import uuid

from websockets.sync.client import connect

CFG = os.environ.get("REDZONE_OBS_WS_CONFIG",
                     os.path.expanduser("~/.config/obs-studio/plugin_config/obs-websocket/config.json"))
SYSTEM = os.environ.get("REDZONE_AUDIO_SYSTEM_SOURCE", "Звук системы")   # историческое имя; теперь это звук игры
DISCORD = os.environ.get("REDZONE_AUDIO_DISCORD_SOURCE", "Дискорд")
GAME_SINK = os.environ.get("REDZONE_AUDIO_GAME_SINK", "game_mix")
DISCORD_SINK = os.environ.get("REDZONE_AUDIO_DISCORD_SINK", "discord_mix")


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

    kinds = {i["inputName"]: i.get("unversionedInputKind")
             for i in req("GetInputList").get("inputs", [])}
    system_kind = kinds.get(SYSTEM) or "pulse_output_capture"
    before = req("GetInputSettings", inputName=SYSTEM).get("inputSettings", {}).get("device_id")
    print(f"«{SYSTEM}» сейчас читает: {before}")

    if req("SetInputSettings", inputName=SYSTEM,
           inputSettings={"device_id": f"{GAME_SINK}.monitor"}, overlay=True) is not None:
        after = req("GetInputSettings", inputName=SYSTEM).get("inputSettings", {}).get("device_id")
        print(f"  → теперь читает: {after} {'✓' if after == GAME_SINK + '.monitor' else '!!'}")

    scenes = [s["sceneName"] for s in req("GetSceneList").get("scenes", [])]
    if DISCORD not in kinds:
        cur = req("GetCurrentProgramScene").get("currentProgramSceneName") or (scenes[0] if scenes else "")
        req("CreateInput", inputName=DISCORD, inputKind=system_kind, sceneName=cur,
            sceneItemEnabled=True, inputSettings={"device_id": f"{DISCORD_SINK}.monitor"})
        got = req("GetInputSettings", inputName=DISCORD)
        dev = (got or {}).get("inputSettings", {}).get("device_id")
        print(f"создан источник «{DISCORD}»: {dev} {'✓' if dev == DISCORD_SINK + '.monitor' else '!!'}")
        req("SetInputVolume", inputName=DISCORD, inputVolumeDb=0.0)
    else:
        print(f"источник «{DISCORD}» уже есть — выравниваю устройство")
        req("SetInputSettings", inputName=DISCORD,
            inputSettings={"device_id": f"{DISCORD_SINK}.monitor"}, overlay=True)

    scenes = [s["sceneName"] for s in req("GetSceneList").get("scenes", [])]
    added, already = [], []
    for sc in scenes:
        items = [i["sourceName"] for i in req("GetSceneItemList", sceneName=sc).get("sceneItems", [])]
        if DISCORD in items:
            already.append(sc)
            continue
        if req("CreateSceneItem", sceneName=sc, sourceName=DISCORD, sceneItemEnabled=True) is not None:
            added.append(sc)
    print(f"«{DISCORD}» добавлен в {len(added)} сцен(ы): {', '.join(added) if added else '—'}")
    print(f"  уже был в {len(already)} сцен(ах)")

    for name in (SYSTEM, DISCORD):
        muted = req("GetInputMute", inputName=name)
        vol = req("GetInputVolume", inputName=name)
        if muted is None or vol is None:
            print(f"  «{name}»: источник не найден")
            continue
        print(f"  «{name}»: мьют={muted.get('inputMuted')} громкость={vol.get('inputVolumeDb')} дБ")
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
