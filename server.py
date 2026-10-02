#!/usr/bin/env python3
"""Пульт стримера: веб-интерфейс для планшета + мост к obs-websocket.

Отдаёт статику (static/), состояние OBS по /api/state, команды по /api/cmd и
websocket /ws. Звуковые дорожки играет на ПК через pw-play — то есть в устройство,
монитор которого OBS захватывает как «Звук системы», поэтому зрители их слышат.
"""
from __future__ import annotations

import asyncio, base64, hashlib, json, os, signal, struct, subprocess, time, zlib
from pathlib import Path

from aiohttp import WSMsgType, web

PANEL = Path(__file__).resolve().parent
STATIC = PANEL / "static"
SOUNDS = PANEL / "sounds"
CONFIG_FILE = PANEL / "config.json"
OBS_WS_CONFIG = Path.home() / ".config/obs-studio/plugin_config/obs-websocket/config.json"

PORT = 8090
SOUND_EXT = {".mp3", ".wav", ".ogg", ".opus", ".flac", ".m4a", ".aac", ".webm"}
ALLOW_PREFIX = ("127.", "192.168.", "10.", "100.", "::1", "localhost", "fd7a:")
MIC, SYS = "Микрофон", "Звук системы"
SCENE_NUMS = {"Dota 2": "1", "Hearthstone": "2", "WoW": "3", "Just Chatting": "4",
              "Рабочий стол": "5", "Перерыв": "6", "Начало стрима": "7",
              "Конец стрима": "8", "Чёрный экран": "9"}


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def load_cfg() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────────────────────── obs-websocket ──────────────────────────────
class Obs:
    """Минимальный клиент obs-websocket v5 с авто-переподключением."""

    def __init__(self, on_event=None):
        self.ws = None
        self.on_event = on_event
        self.connected = False
        self._id = 0
        self._waiters: dict[str, asyncio.Future] = {}
        try:
            cfg = json.loads(OBS_WS_CONFIG.read_text(encoding="utf-8"))
            self.host, self.port = "127.0.0.1", int(cfg.get("server_port", 4455))
            self.password = cfg.get("server_password") or ""
        except Exception:
            self.host, self.port, self.password = "127.0.0.1", 4455, ""

    async def run(self):
        while True:
            try:
                await self._connect()
            except Exception as e:
                log("OBS недоступен:", e.__class__.__name__, e)
            self.connected = False
            await asyncio.sleep(3)

    async def _connect(self):
        import websockets
        url = f"ws://{self.host}:{self.port}"
        async with websockets.connect(url, subprotocols=["obswebsocket.json"],
                                      max_size=32 * 1024 * 1024, open_timeout=6) as ws:
            hello = json.loads(await ws.recv())
            d = hello["d"]
            auth = None
            if d.get("authentication"):
                secret = base64.b64encode(
                    hashlib.sha256((self.password + d["authentication"]["salt"]).encode()).digest()).decode()
                auth = base64.b64encode(
                    hashlib.sha256((secret + d["authentication"]["challenge"]).encode()).digest()).decode()
            await ws.send(json.dumps({"op": 1, "d": {"rpcVersion": 1, "authentication": auth,
                                                     "eventSubscriptions": 1023}}))
            ident = json.loads(await ws.recv())
            if ident["op"] != 2:
                raise RuntimeError(f"рукопожатие не удалось: {ident}")
            self.ws, self.connected = ws, True
            log("подключились к OBS", ident["d"].get("obsWebSocketVersion"))
            if self.on_event:
                await self.on_event({"type": "connected"})
            async for raw in ws:
                msg = json.loads(raw)
                if msg["op"] == 7:                      # ответ на запрос
                    fut = self._waiters.pop(msg["d"]["requestId"], None)
                    if fut and not fut.done():
                        fut.set_result(msg["d"])
                elif msg["op"] == 5 and self.on_event:  # событие
                    await self.on_event(msg["d"])

    async def req(self, requestType: str, **data):
        if not self.connected or self.ws is None:
            raise RuntimeError("OBS офлайн")
        self._id += 1
        rid = f"r{self._id}"
        fut = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        await self.ws.send(json.dumps({"op": 6, "d": {"requestType": requestType,
                                                      "requestId": rid, "requestData": data}}))
        res = await asyncio.wait_for(fut, timeout=8)
        if not res["requestStatus"]["result"]:
            raise RuntimeError(res["requestStatus"].get("comment", requestType + " failed"))
        return res.get("responseData", {})


# ───────────────────────────── звуки ──────────────────────────────
class Player:
    """Проигрывание файлов на ПК: pw-play → звук системы → эфир."""

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        cfg = load_cfg()
        self.volume = float(cfg.get("sound_volume", 0.9))

    def list_sounds(self):
        out = []
        if not SOUNDS.exists():
            return out
        for p in sorted(SOUNDS.rglob("*")):
            if p.is_file() and p.suffix.lower() in SOUND_EXT and not p.name.startswith("."):
                rel = p.relative_to(SOUNDS).as_posix()
                grp = p.parent.relative_to(SOUNDS).as_posix() if p.parent != SOUNDS else ""
                out.append({"file": rel, "name": p.stem, "grp": "" if grp == "." else grp,
                            "playing": rel in self.procs})
        return out

    def _safe(self, rel: str) -> Path:
        p = (SOUNDS / rel).resolve()
        if not str(p).startswith(str(SOUNDS.resolve()) + os.sep) or not p.is_file():
            raise ValueError("недопустимый файл")
        return p

    def play(self, rel: str):
        p = self._safe(rel)
        self.stop(rel)
        try:
            self.procs[rel] = subprocess.Popen(
                ["pw-play", f"--volume={self.volume:.2f}", str(p)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            log(f"звук: {rel} (volume {self.volume:.2f})")
        except FileNotFoundError:
            raise RuntimeError("pw-play не найден")

    def stop(self, rel: str | None = None):
        targets = [rel] if rel else list(self.procs)
        for key in targets:
            proc = self.procs.pop(key, None)
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    proc.terminate()

    def reap(self) -> bool:
        """Убирает завершившиеся процессы; True — что-то изменилось."""
        done = [k for k, p in self.procs.items() if p.poll() is not None]
        for k in done:
            del self.procs[k]
        return bool(done)

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.5, v))
        cfg = load_cfg()
        cfg["sound_volume"] = round(self.volume, 3)
        save_cfg(cfg)


# ───────────────────────────── пульт ──────────────────────────────
player = Player()


class Panel:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.obs = Obs(on_event=self._on_obs_event)
        self.state: dict = {}

    # --- события от OBS: обновляем состояние и рассылаем ---
    async def _on_obs_event(self, ev):
        if ev.get("type") == "connected":
            await self.refresh()
            await self.broadcast()
            return
        et = ev.get("eventType", "")
        if et in ("CurrentProgramSceneChanged", "StreamStateChanged", "RecordStateChanged",
                  "ReplayBufferStateChanged", "InputMuteStateChanged", "InputVolumeChanged",
                  "InputCreated", "InputRemoved", "SceneListChanged", "InputNameChanged"):
            await self.refresh()
            await self.broadcast()

    async def refresh(self):
        st = self.state or {}
        if not self.obs.connected:
            self.state = {"obs": False, "sounds": player.list_sounds(),
                          "sndvol": round(player.volume * 100)}
            return
        try:
            scenes = self.obs_cache_scenes = await self.obs.req("GetSceneList")
            cur = scenes["currentProgramSceneName"]
            stream = await self.obs.req("GetStreamStatus")
            record = await self.obs.req("GetRecordStatus")
            try:
                replay = (await self.obs.req("GetReplayBufferStatus"))["outputActive"]
            except Exception:
                replay = False
            audio = {}
            for key, name in (("mic", MIC), ("sys", SYS)):
                try:
                    audio[key] = {"mute": (await self.obs.req("GetInputMute", inputName=name))["inputMuted"],
                                  "vol": round((await self.obs.req("GetInputVolume", inputName=name))["inputVolumeMul"] * 100)}
                except Exception:
                    audio[key] = {"mute": None, "vol": None}
            self.state = {
                "obs": True,
                "scene": cur,
                "scenes": [{"name": s["sceneName"], "num": SCENE_NUMS.get(s["sceneName"], "")}
                           for s in reversed(scenes["scenes"])],
                "stream": {"active": stream["outputActive"], "time": stream["outputTimecode"][:11],
                           "reconnect": stream.get("outputReconnecting", False)},
                "record": {"active": record["outputActive"], "time": record.get("outputTimecode", "")[:11]},
                "replay": replay,
                "audio": audio,
                "sounds": player.list_sounds(),
                "sndvol": round(player.volume * 100),
            }
        except Exception as e:
            st.update({"obs": False, "error": str(e)})
            self.state = st

    async def broadcast(self):
        if not self.clients:
            return
        data = json.dumps(self.state, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                self.clients.discard(ws)

    async def tick(self):
        while True:
            await asyncio.sleep(1)
            changed = player.reap()
            if self.clients:
                if self.state.get("stream", {}).get("active") or self.state.get("record", {}).get("active"):
                    await self.refresh()
                elif changed:
                    await self.refresh()
                await self.broadcast()

    async def command(self, msg: dict):
        c = msg.get("c")
        o = self.obs
        if c == "scene":
            await o.req("SetCurrentProgramScene", sceneName=msg["name"])
        elif c == "stream":
            act = msg.get("a")
            if act == "start":
                await o.req("StartStream")
            elif act == "stop":
                await o.req("StopStream")
            else:
                await (o.req("StopStream") if self.state.get("stream", {}).get("active") else o.req("StartStream"))
        elif c == "record":
            act = msg.get("a")
            if act == "start":
                await o.req("StartRecord")
            elif act == "stop":
                await o.req("StopRecord")
            else:
                await (o.req("StopRecord") if self.state.get("record", {}).get("active") else o.req("StartRecord"))
        elif c == "replay":
            await o.req("SaveReplayBuffer")
        elif c == "mute":
            name = MIC if msg.get("src") == "mic" else SYS
            cur = (await o.req("GetInputMute", inputName=name))["inputMuted"]
            await o.req("SetInputMute", inputName=name, inputMuted=not cur)
        elif c == "vol":
            name = MIC if msg.get("src") == "mic" else SYS
            await o.req("SetInputVolume", inputName=name, inputVolumeMul=max(0.0, min(1.0, float(msg.get("v", 50)) / 100)))
        elif c == "sound":
            player.play(msg["file"])
        elif c == "stopsounds":
            player.stop()
        elif c == "sndvol":
            player.set_volume(float(msg.get("v", 90)) / 100)
        else:
            raise ValueError(f"неизвестная команда: {c}")
        await self.refresh()
        await self.broadcast()


panel = Panel()


# ───────────────────────────── HTTP ──────────────────────────────
def allowed(remote: str | None) -> bool:
    if not remote:
        return False
    return any(remote.startswith(p) for p in ALLOW_PREFIX)


@web.middleware
async def guard(request, handler):
    if not allowed(request.remote):
        return web.json_response({"error": "нет доступа"}, status=403)
    return await handler(request)


async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def api_state(request):
    await panel.refresh()
    return web.json_response(panel.state)


async def api_cmd(request):
    try:
        msg = await request.json()
        await panel.command(msg)
        return web.json_response({"ok": True, "state": panel.state})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    panel.clients.add(ws)
    await panel.refresh()
    await ws.send_str(json.dumps(panel.state, ensure_ascii=False))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await panel.command(json.loads(msg.data))
                except Exception as e:
                    await ws.send_str(json.dumps({"error": str(e)}, ensure_ascii=False))
    finally:
        panel.clients.discard(ws)
    return ws


async def upload(request):
    reader = await request.multipart()
    saved = []
    while (part := await reader.next()) is not None:
        if part.name != "file" or not part.filename:
            continue
        target = SOUNDS / "загружено"
        target.mkdir(parents=True, exist_ok=True)
        name = Path(part.filename).name
        if Path(name).suffix.lower() not in SOUND_EXT:
            raise web.HTTPBadRequest(text="поддерживаются только аудиофайлы")
        path = target / name
        with open(path, "wb") as fh:
            while chunk := await part.read_chunk(1 << 20):
                fh.write(chunk)
        saved.append(name)
    await panel.refresh()
    await panel.broadcast()
    return web.json_response({"ok": True, "saved": saved})


# ───────────────────────────── иконки ──────────────────────────────
def make_icon(path: Path, size: int, bg=(20, 21, 26), fg=(233, 25, 22)):
    """PNG с закруглённым квадратом и красным кругом — без внешних зависимостей."""
    ss = 3
    n = size * ss
    r_out, r_in = n * 0.22, n * 0.26
    c = (n - 1) / 2
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for dy in range(ss):
                for dx in range(ss):
                    px, py = x * ss + dx + 0.5, y * ss + dy + 0.5
                    # закруглённый квадрат
                    dxo = max(r_out - px, px - (n - r_out), 0)
                    dyo = max(r_out - py, py - (n - r_out), 0)
                    if dxo * dxo + dyo * dyo > r_out * r_out:
                        continue
                    inside = ((px - c) ** 2 + (py - c) ** 2) <= r_in * r_in
                    col = fg if inside else bg
                    for i in range(3):
                        acc[i] += col[i]
                    acc[3] += 255
            a = acc[3] / (ss * ss)
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                k = acc[3] / 255
                row += bytes((round(acc[0] / k), round(acc[1] / k), round(acc[2] / k), round(a)))
        rows.append(row)

    def chunk(tag: bytes, data: bytes):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + r for r in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(png)


# ───────────────────────────── запуск ──────────────────────────────
async def main():
    SOUNDS.mkdir(parents=True, exist_ok=True)
    (STATIC / "icons").mkdir(parents=True, exist_ok=True)
    for s in (180, 192, 512):
        icon = STATIC / "icons" / f"icon-{s}.png"
        if not icon.exists():
            make_icon(icon, s)
            log("иконка создана:", icon.name)

    app = web.Application(middlewares=[guard], client_max_size=256 << 20)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/cmd", api_cmd)
    app.router.add_post("/api/upload", upload)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC, show_index=False)

    asyncio.create_task(panel.obs.run())
    asyncio.create_task(panel.tick())
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"пульт: http://192.168.3.153:{PORT}/  (папка звуков: {SOUNDS})")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
