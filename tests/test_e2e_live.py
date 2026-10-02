"""Живой сквозной E2E пульта против настоящего OBS (маркер e2e_live).

Тест поднимает сервис «как в бою» (server.app.create_app + AppRunner/TCPSite) на
127.0.0.1:8091 с временным config.json в tmp-каталоге и гоняет по нему настоящие
HTTP/WS-запросы: /api/state, /api/cmd, /ws. Управляет реальным OBS по
obs-websocket v5 (127.0.0.1:4455).

Если OBS не запущен — тест поднимает его сам (профиль и коллекция «Стрим») и
гасит в конце; если OBS уже был запущен — не трогает его. Тест обязан вернуть
состояние OBS (сцену, mute и громкости «Микрофон»/«Звук системы», буфер повтора)
даже при падении. Короткий тест-эфир идёт под сторожем автостопа: отдельный поток
гасит эфир, если тест завис и не дошёл до finally.

Секреты (пароль obs-websocket, токен пульта, ключ потока) в вывод не попадают.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import threading
import time

import aiohttp
import pytest
from aiohttp import web

from server.app import create_app
from server.commands import Commands
from server.config import load_or_create
from server.journal import Journal
from server.obs import Obs, load_connection
from server.panel import Panel

pytestmark = pytest.mark.e2e_live

HOST = "127.0.0.1"
PANEL_PORT = 8091
OBS_PORT = 4455

MIC = "Микрофон"
SYS = "Звук системы"
BLACK = "Чёрный экран"

# Ожидаемый порядок сцен (numpad 1..9, ТЗ §8.6/§2.7).
SCENES = ["Dota 2", "Hearthstone", "WoW", "Just Chatting", "Рабочий стол",
          "Перерыв", "Начало стрима", "Конец стрима", "Чёрный экран"]

# Как в бою (§8.1), но интервалы короче — тест не должен ждать десятками секунд.
REFRESH_INTERVAL = 0.5
STALE_AFTER = 3.0
METER_INTERVAL = 0.2

# Сторож автостопа: столько секунд тест-эфир может жить, если тест завис.
STREAM_BUDGET = 120.0

# Окружение владельца для запуска OBS на его дисплее (значения — запасные, если
# переменных нет в окружении процесса тестов).
_DISPLAY_ENV = {
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-0",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "XAUTHORITY": "/run/user/1000/xauth_zPLJZO",
}


# ── окружение OBS ──
def obs_ws_config_path() -> pathlib.Path:
    """Путь к config.json obs-websocket (переопределяется через OBS_WS_CONFIG)."""
    override = os.environ.get("OBS_WS_CONFIG")
    if override:
        return pathlib.Path(override).expanduser()
    return (pathlib.Path.home() / ".config" / "obs-studio" / "plugin_config"
            / "obs-websocket" / "config.json")


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) == 0


def _obs_env() -> dict:
    env = dict(os.environ)
    for key, value in _DISPLAY_ENV.items():
        env.setdefault(key, value)
    return env


class ObsLauncher:
    """Поднимает и гасит настоящий OBS; уже запущенный OBS не трогает."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def ensure(self, timeout: float = 90.0) -> bool:
        """Гарантировать живой OBS. True — запустили сами, False — уже работал."""
        if port_open(OBS_PORT):
            return False
        # start_new_session=True = setsid: OBS переживёт команду и текущий процесс.
        self.proc = subprocess.Popen(
            ["obs", "--profile", "Стрим", "--collection", "Стрим",
             "--disable-missing-files-check", "--minimize-to-tray"],
            env=_obs_env(), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port_open(OBS_PORT):
                return True
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"OBS завершился при запуске, код {self.proc.returncode}")
            time.sleep(0.3)
        raise AssertionError(
            f"OBS не поднял obs-websocket на {HOST}:{OBS_PORT} за {timeout:.0f} с")

    def stop(self, timeout: float = 30.0) -> None:
        """Погасить только тот OBS, который запустили мы сами."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and port_open(OBS_PORT):
            time.sleep(0.2)


# ── сторож автостопа ──
async def _stop_stream_once(host: str, port: int, password: str) -> None:
    obs = Obs(host=host, port=port, password=password,
              retry_delay=0.2, request_timeout=3.0)
    task = asyncio.create_task(obs.run())
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not obs.connected:
            await asyncio.sleep(0.1)
        if obs.connected:
            await obs.req("StopStream")
    finally:
        await obs.close()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


class StreamWatchdog:
    """Поток, который глушит эфир, если тест не дошёл до штатного стопа.

    Работает независимо от цикла событий теста: если тот завис, сторож всё равно
    откроет своё соединение с OBS и дёрнет StopStream.
    """

    def __init__(self, host: str, port: int, password: str, timeout: float) -> None:
        self._host, self._port, self._password = host, port, password
        self._timeout = float(timeout)
        self._armed = False
        self._deadline = 0.0
        self.fired = False
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def arm(self) -> None:
        self._armed = True
        self._deadline = time.monotonic() + self._timeout
        self.fired = False
        self._done.clear()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="stream-autostop")
            self._thread.start()

    def disarm(self) -> None:
        self._armed = False
        self._done.set()

    def _run(self) -> None:
        while not self._done.is_set():
            if self._armed and time.monotonic() >= self._deadline:
                self.fired = True
                self._armed = False
                try:
                    asyncio.run(_stop_stream_once(self._host, self._port,
                                                  self._password))
                except Exception:
                    pass
            self._done.wait(0.2)


# ── HTTP-клиент пульта ──
def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def get_state(session, token) -> dict:
    url = f"http://{HOST}:{PANEL_PORT}/api/state"
    async with session.get(url, headers=_auth(token)) as resp:
        assert resp.status == 200, f"/api/state -> {resp.status}"
        return await resp.json()


async def post_cmd(session, token, name: str, args=None):
    url = f"http://{HOST}:{PANEL_PORT}/api/cmd"
    body = {"name": name}
    if args is not None:
        body["args"] = args
    async with session.post(url, json=body, headers=_auth(token)) as resp:
        return resp.status, await resp.json()


async def wait_state(session, token, predicate, timeout: float, what: str) -> dict:
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        state = await get_state(session, token)
        if predicate(state):
            return state
        await asyncio.sleep(0.25)
    raise AssertionError(
        f"не дождались «{what}» за {timeout:.0f} с; состояние: "
        + json.dumps(state, ensure_ascii=False))


async def _wait_connected(obs, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if obs.connected:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"клиент OBS не подключился за {timeout:.0f} с")


async def _wait_ready(obs, timeout: float = 120.0) -> None:
    """Дождаться, когда OBS догрузит коллекцию: сразу после старта запросы отвергаются.

    obs-websocket открывается раньше, чем OBS готов отдавать сцены, и отвечает
    «OBS is not ready to perform the request» — это ожидаемо, надо подождать.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await asyncio.wait_for(obs.req("GetSceneList"), timeout=10)
            return
        except Exception as exc:  # ещё грузится (или запрос не прошёл) — пробуем снова
            last = exc
        await asyncio.sleep(0.3)
    raise AssertionError(f"OBS не готов отдавать сцены за {timeout:.0f} с: {last}")


# ── снятие и возврат состояния OBS ──
async def capture_state(obs) -> dict:
    scene = (await obs.req("GetCurrentProgramScene"))["currentProgramSceneName"]
    return {
        "scene": scene,
        "mic_mute": (await obs.req("GetInputMute", inputName=MIC))["inputMuted"],
        "sys_mute": (await obs.req("GetInputMute", inputName=SYS))["inputMuted"],
        "mic_vol": (await obs.req("GetInputVolume", inputName=MIC))["inputVolumeMul"],
        "sys_vol": (await obs.req("GetInputVolume", inputName=SYS))["inputVolumeMul"],
        "replay": (await obs.req("GetReplayBufferStatus"))["outputActive"],
    }


async def restore_state(obs, before: dict) -> None:
    """Вернуть сцену, mute, громкости и буфер повтора; каждый шаг независим."""
    async def step(label, coro):
        try:
            await coro
        except Exception as exc:
            print(f"  возврат: {label} не удался: {exc}", flush=True)

    await step("сцена", obs.req("SetCurrentProgramScene", sceneName=before["scene"]))
    await step("mute микрофона",
               obs.req("SetInputMute", inputName=MIC, inputMuted=before["mic_mute"]))
    await step("mute системы",
               obs.req("SetInputMute", inputName=SYS, inputMuted=before["sys_mute"]))
    await step("громкость микрофона",
               obs.req("SetInputVolume", inputName=MIC, inputVolumeMul=before["mic_vol"]))
    await step("громкость системы",
               obs.req("SetInputVolume", inputName=SYS, inputVolumeMul=before["sys_vol"]))
    try:
        active = (await obs.req("GetReplayBufferStatus"))["outputActive"]
    except Exception as exc:
        print(f"  возврат: буфер повтора не прочитан: {exc}", flush=True)
        return
    if before["replay"] and not active:
        await step("включить буфер повтора", obs.req("StartReplayBuffer"))
    elif not before["replay"] and active:
        await step("выключить буфер повтора", obs.req("StopReplayBuffer"))


async def ensure_stream_record_off(obs) -> None:
    """Идемпотентно погасить эфир и запись (для finally)."""
    with contextlib.suppress(Exception):
        if (await obs.req("GetStreamStatus"))["outputActive"]:
            await obs.req("StopStream")
    with contextlib.suppress(Exception):
        if (await obs.req("GetRecordStatus"))["outputActive"]:
            await obs.req("StopRecord")
    # OBS завершает выход асинхронно — дать статусу устояться.
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            stream = (await obs.req("GetStreamStatus"))["outputActive"]
            record = (await obs.req("GetRecordStatus"))["outputActive"]
        except Exception:
            return
        if not stream and not record:
            return
        await asyncio.sleep(0.5)


async def probe_state(obs) -> dict:
    return {
        "stream": (await obs.req("GetStreamStatus"))["outputActive"],
        "record": (await obs.req("GetRecordStatus"))["outputActive"],
        "replay": (await obs.req("GetReplayBufferStatus"))["outputActive"],
        "scene": (await obs.req("GetCurrentProgramScene"))["currentProgramSceneName"],
        "mic_mute": (await obs.req("GetInputMute", inputName=MIC))["inputMuted"],
        "sys_mute": (await obs.req("GetInputMute", inputName=SYS))["inputMuted"],
    }


# ── уборка файлов записи ──
def list_files(directory: pathlib.Path) -> set:
    with contextlib.suppress(OSError):
        return {p.name for p in directory.iterdir() if p.is_file()}
    return set()


def drop_new_recordings(directory: pathlib.Path, before: set) -> list:
    removed = []
    for name in sorted(list_files(directory) - before):
        path = directory / name
        try:
            path.unlink()
            removed.append(name)
        except OSError:
            pass
    return removed


# ── сам тест ──
async def test_live_panel_against_obs(tmp_path):
    launcher = ObsLauncher()
    watchdog = StreamWatchdog(HOST, OBS_PORT, "", STREAM_BUDGET)
    raw = None
    raw_task = None
    runner = None
    tasks: list = []
    before: dict = {}
    record_dir: pathlib.Path | None = None
    files_before: set = set()

    try:
        started_obs = launcher.ensure()
        print(f"OBS: {'подняли сами' if started_obs else 'уже был запущен'} "
              f"на {HOST}:{OBS_PORT}", flush=True)
        host, port, password = load_connection(obs_ws_config_path())
        watchdog = StreamWatchdog(host, port, password, STREAM_BUDGET)

        # Сырой клиент — для снятия/возврата состояния и управления буфером повтора.
        raw = Obs(host=host, port=port, password=password, retry_delay=0.2)
        raw_task = asyncio.create_task(raw.run())
        await _wait_connected(raw)
        await _wait_ready(raw)
        before = await capture_state(raw)
        record_dir = pathlib.Path((await raw.req("GetRecordDirectory"))["recordDirectory"])
        files_before = list_files(record_dir)
        print("состояние ДО:", json.dumps(before, ensure_ascii=False), flush=True)

        # Стек «как в бою» на тестовом порту с временным config.json.
        cfg_path = tmp_path / "config.json"
        cfg = load_or_create(cfg_path)
        token = cfg["token"]
        assert (cfg_path.stat().st_mode & 0o777) == 0o600, "config.json не 0600"

        obs = Obs(host=host, port=port, password=password, retry_delay=0.2)
        panel = Panel(obs, refresh_interval=REFRESH_INTERVAL,
                      stale_after=STALE_AFTER, meter_interval=METER_INTERVAL)
        commands = Commands(obs, panel)
        journal = Journal(tmp_path / "journal.jsonl")
        app = create_app(obs=obs, panel=panel, commands=commands, token=token,
                         config_path=cfg_path, journal=journal)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PANEL_PORT)
        await site.start()
        tasks = [asyncio.create_task(obs.run(), name="obs"),
                 asyncio.create_task(panel.run(), name="panel")]
        obs_task, panel_task = tasks

        async with aiohttp.ClientSession() as session:
            # 0. Пульт видит живой OBS, снапшот свежий, контракт на месте.
            fresh = await wait_state(session, token,
                                     lambda s: s["obs"] and not s["stale"], 30,
                                     "OBS подключён и снапшот свежий")
            assert {"obs", "scene", "scenes", "stream", "record", "replay",
                    "audio", "stale", "updated_at"} <= set(fresh)
            assert [s["name"] for s in fresh["scenes"]] == SCENES, fresh["scenes"]
            print("сцены из OBS:", [s["name"] for s in fresh["scenes"]], flush=True)

            # 0b. Первое сообщение /ws — снапшот состояния.
            async with session.ws_connect(
                    f"ws://{HOST}:{PANEL_PORT}/ws?token={token}") as ws:
                first = await asyncio.wait_for(ws.receive_json(), timeout=5)
                assert first["type"] == "state" and first["state"]["obs"] is True
                print("WS первое сообщение: type=state, obs="
                      f"{first['state']['obs']}", flush=True)

            # 1. Все девять сцен: команда, снапшот в ответе и повторное чтение.
            for name in SCENES:
                status, env = await post_cmd(session, token, "scene.set", {"name": name})
                assert status == 200 and env["ok"] is True, (name, status, env)
                assert env["changed"] == ["scene"], env
                assert env["state"]["scene"] == name, (name, env["state"]["scene"])
                read_back = await get_state(session, token)
                assert read_back["scene"] == name, (name, read_back["scene"])
            print("сцены: все 9 переключены и подтверждены OBS", flush=True)

            # 2. Mute микрофона и системы — туда и обратно.
            base = await get_state(session, token)
            for track in ("mic", "sys"):
                was = base["audio"][track]["mute"]
                status, env = await post_cmd(session, token, "audio.toggleMute",
                                             {"track": track})
                assert status == 200 and env["ok"] is True, (track, status, env)
                assert env["state"]["audio"][track]["mute"] is (not was), (track, env)
                status, env = await post_cmd(session, token, "audio.toggleMute",
                                             {"track": track})
                assert status == 200 and env["ok"] is True, (track, status, env)
                assert env["state"]["audio"][track]["mute"] is was, (track, env)
            print("mute: микрофон и система переключены туда и обратно", flush=True)

            # 3. Громкость: выставить и вернуть (значение из снапшота — проценты).
            base = await get_state(session, token)
            for track in ("mic", "sys"):
                was = base["audio"][track]["vol"]
                target = 37 if was != 37 else 42
                status, env = await post_cmd(session, token, "audio.setVolume",
                                             {"track": track, "value": target})
                assert status == 200 and env["ok"] is True, (track, status, env)
                assert env["state"]["audio"][track]["vol"] == target, (track, env)
                status, env = await post_cmd(session, token, "audio.setVolume",
                                             {"track": track, "value": was})
                assert status == 200 and env["ok"] is True, (track, status, env)
                assert env["state"]["audio"][track]["vol"] == was, (track, env)
            print("громкость: выставлена и возвращена для обоих источников", flush=True)

            # 4. Запись: старт и стоп через пульт, с ожиданием фактического статуса.
            status, env = await post_cmd(session, token, "record.toggle")
            assert status == 200 and env["ok"] is True, (status, env)
            await wait_state(session, token, lambda s: s["record"]["active"] is True,
                             20, "запись пошла")
            status, env = await post_cmd(session, token, "record.toggle")
            assert status == 200 and env["ok"] is True, (status, env)
            await wait_state(session, token, lambda s: s["record"]["active"] is False,
                             30, "запись остановлена")
            print("запись: старт и стоп подтверждены", flush=True)

            # 5. Повтор при выключенном буфере — 409; при включённом — успех.
            if (await raw.req("GetReplayBufferStatus"))["outputActive"]:
                await raw.req("StopReplayBuffer")
            await wait_state(session, token, lambda s: s["replay"] is False, 20,
                             "буфер повтора выключен")
            status, env = await post_cmd(session, token, "replay.save")
            assert status == 409 and env["ok"] is False, (status, env)
            print(f"повтор при выключенном буфере -> {status} {env['error']}", flush=True)

            await raw.req("StartReplayBuffer")
            await wait_state(session, token, lambda s: s["replay"] is True, 20,
                             "буфер повтора включён")
            status, env = await post_cmd(session, token, "replay.save")
            assert status == 200 and env["ok"] is True, (status, env)
            assert env["changed"] == ["replay"], env
            print(f"повтор при включённом буфере -> {status} ok={env['ok']}", flush=True)

            # 6. «Паника»: чёрный экран + mute обоих источников, шаги и факт в OBS.
            status, env = await post_cmd(session, token, "panic")
            assert status == 200 and env["ok"] is True, (status, env)
            assert env["steps"] == {"scene": True, "mic": True, "sys": True}, env["steps"]
            actual = await probe_state(raw)
            assert actual["scene"] == BLACK, actual
            assert actual["mic_mute"] is True and actual["sys_mute"] is True, actual
            print("паника: шаги", env["steps"], "OBS:", actual["scene"], flush=True)

            # 7. Устаревшие данные гасят разрушительную команду (409), но не сцену.
            panel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await panel_task
            await asyncio.sleep(STALE_AFTER + 1.0)
            stale = await get_state(session, token)
            assert stale["stale"] is True and stale["obs"] is True, stale
            status, env = await post_cmd(session, token, "stream.stop", {"confirm": True})
            assert status == 409 and env["ok"] is False, (status, env)
            print(f"stale + stream.stop -> {status}: {env['error']}", flush=True)
            status, env = await post_cmd(session, token, "scene.set",
                                         {"name": before["scene"]})
            assert status == 200 and env["ok"] is True, (status, env)
            panel_task = asyncio.create_task(panel.run(), name="panel")
            tasks[1] = panel_task  # чтобы finally погасил именно актуальную задачу
            await wait_state(session, token, lambda s: not s["stale"], 20,
                             "снапшот снова свежий")

            # 8. Короткий тест-эфир под сторожем автостопа.
            watchdog.arm()
            try:
                status, env = await post_cmd(session, token, "stream.start",
                                             {"confirm": True})
                assert status == 200 and env["ok"] is True, (status, env)
                live = await wait_state(
                    session, token,
                    lambda s: s["stream"]["active"] and
                    s["stream"]["time"] not in ("", "00:00:00.00"), 60,
                    "эфир идёт, таймкод пошёл")
                assert live["stream"]["reconnect"] is False, live["stream"]
                first_time = live["stream"]["time"]
                assert (await raw.req("GetStreamStatus"))["outputActive"] is True

                await asyncio.sleep(2.5)
                later = await wait_state(session, token,
                                         lambda s: s["stream"]["active"], 10, "эфир держится")
                assert later["stream"]["reconnect"] is False, later["stream"]
                assert later["stream"]["time"] != first_time, \
                    (first_time, later["stream"]["time"])
                print(f"эфир: active=True, таймкод {first_time} -> "
                      f"{later['stream']['time']}, reconnect=False", flush=True)

                status, env = await post_cmd(session, token, "stream.stop",
                                             {"confirm": True})
                assert status == 200 and env["ok"] is True, (status, env)
                await wait_state(session, token,
                                 lambda s: s["stream"]["active"] is False, 30,
                                 "эфир остановлен")
                assert (await raw.req("GetStreamStatus"))["outputActive"] is False
                print("эфир: остановлен через пульт, OBS подтвердил", flush=True)
            finally:
                watchdog.disarm()

            # 9. Возврат состояния и финальная проверка, что эфир/запись выключены.
            await ensure_stream_record_off(raw)
            await restore_state(raw, before)
            await asyncio.sleep(0.5)
            final = await probe_state(raw)
            print("состояние ПОСЛЕ:", json.dumps(final, ensure_ascii=False), flush=True)
            assert final["stream"] is False, final
            assert final["record"] is False, final
            assert final["scene"] == before["scene"], (final, before)
            assert final["mic_mute"] == before["mic_mute"], (final, before)
            assert final["sys_mute"] == before["sys_mute"], (final, before)
            assert final["replay"] == before["replay"], (final, before)
            assert watchdog.fired is False, "сработал сторож автостопа"
            assert (tmp_path / "journal.jsonl").is_file(), "журнал не создан"

    finally:
        # Уборка при любом исходе: эфир, запись, состояние, задачи, OBS.
        watchdog.disarm()
        if raw is not None and raw.connected:
            with contextlib.suppress(Exception):
                await ensure_stream_record_off(raw)
            if before:
                with contextlib.suppress(Exception):
                    await restore_state(raw, before)
            try:
                print("финальная проверка:", json.dumps(await probe_state(raw),
                                                        ensure_ascii=False), flush=True)
            except Exception as exc:
                print(f"финальную проверку снять не удалось: {exc}", flush=True)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        if raw is not None:
            with contextlib.suppress(Exception):
                await raw.close()
        if raw_task is not None:
            raw_task.cancel()
            with contextlib.suppress(BaseException):
                await raw_task
        if record_dir is not None:
            removed = drop_new_recordings(record_dir, files_before)
            if removed:
                print(f"удалены новые файлы записи: {removed}", flush=True)
        launcher.stop()
