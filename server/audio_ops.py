"""Системный слой звука: pactl, pw-dump/pw-link, systemctl --user и захваты OBS.

Две сущности:

* `SystemAudio` — провайдер: единственное место, где мы трогаем систему (модули
  PipeWire, выход по умолчанию, служба-сторож, файлы состояния, источники OBS).
  Интерфейс намеренно грубый, чтобы в тестах подменяться целиком (`FakeAudioOps`).
* `AudioController` — управляющий слой: по состоянию провайдера собирает снапшот
  для `GET /api/audio` и выполняет шаги включения/выключения режима строго в
  порядке из docs/AUDIO.md, отдавая конверт как у `/api/cmd`.

Почему не зовём скрипты владельца (`rz_mix_on.sh` и т.п.): они собирают звук через
`python3 - <<PY` с модулем `websockets`, которого в системном python нет. Пульт
делает ту же работу сам, теми же pactl/pw-link и своим клиентом OBS.

Сервис пульта — служба пользователя: `systemctl --user` и `pactl` доступны, но им
обязательны `XDG_RUNTIME_DIR` и `PULSE_SERVER` (иначе pactl падает с «Connection
refused»), поэтому провайдер подставляет их в окружение каждого вызова.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time

from .audio import (MIX_SINKS, NORMAL, PHYSICAL, STREAM, choose_device, classify,
                    collect_issues, compute_mode, confirm_required)
from .commands import CommandError, RefusedError

# Куда OBS смотрит «Звук системы» и откуда идёт Discord.
SYS_INPUT = "Звук системы"
DISCORD_INPUT = "Дискорд"

# Служба-сторож и человеческие описания виртуальных выходов.
WATCHDOG = "rz-mix.service"
DESCRIPTIONS = {"rz_mix": "RZ_Mix", "game_mix": "RZ_Игра", "discord_mix": "RZ_Discord"}

# Параметры петли — как в rz_mix_watch.py: небольшая задержка и запрет переносить
# концы петли (иначе PipeWire уводит их при переподключении устройства).
LATENCY_MS = 30
MONITOR_CAPTURE_TYPE = "pulse_output_capture"

HOME = pathlib.Path.home()
DEFAULT_DEVICE_FILE = HOME / "stream" / "redzone" / "rz_mix.device"
DEFAULT_STATE_FILE = HOME / "stream" / "redzone" / "rz_mix.state"


# ── провайдер ──
class SystemAudio:
    """Реальный системный провайдер: тонкая обёртка над pactl/pw-dump/pw-link/systemctl.

    `run` можно подменить (список argv -> stdout) — тогда ни один внешний процесс
    не запускается. По умолчанию — subprocess.run с окружением PipeWire.
    """

    def __init__(self, obs=None, *, device_file=DEFAULT_DEVICE_FILE,
                 state_file=DEFAULT_STATE_FILE, run=None):
        self.obs = obs
        self.device_file = pathlib.Path(device_file)
        self.state_file = pathlib.Path(state_file)
        self._run = run or self._subprocess

    # ── окружение и запуск ──
    def _env(self) -> dict:
        env = dict(os.environ)
        # Локаль обязательно C: pactl печатает поля на языке локали («Имя:»,
        # «Состояние:»), и разбор молча даёт пустой список устройств — в живом
        # сервисе вкладка «Звук» показывала «устройства не найдены».
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
        env["XDG_RUNTIME_DIR"] = runtime
        env.setdefault("PULSE_SERVER", f"unix:{runtime}/pulse/native")
        return env

    def _subprocess(self, args, *, check: bool = False) -> str:
        result = subprocess.run(args, capture_output=True, text=True, env=self._env())
        if check and result.returncode:
            raise RuntimeError(f"{' '.join(args)}: {result.stderr.strip() or result.returncode}")
        return result.stdout.strip()

    # ── выходы ──
    def list_sinks(self) -> list[dict]:
        """Выходы (sinks) с состоянием и человеческим описанием."""
        out = self._run(["pactl", "list", "sinks"])
        sinks = []
        for block in out.split("Sink #")[1:]:
            name = re.search(r"^\s*Name:\s*(\S+)", block, re.M)
            if not name:
                continue
            description = re.search(r"^\s*Description:\s*(.+)$", block, re.M)
            state = re.search(r"^\s*State:\s*(\S+)", block, re.M)
            sinks.append({"name": name.group(1),
                          "state": state.group(1) if state else "",
                          "description": (description.group(1).strip()
                                          if description else name.group(1))})
        return sinks

    def default_sink(self) -> str:
        return self._run(["pactl", "get-default-sink"]).strip()

    def set_default_sink(self, name: str) -> None:
        self._run(["pactl", "set-default-sink", name], check=True)

    def create_sink(self, name: str, description: str = "") -> None:
        self._run(["pactl", "load-module", "module-null-sink", f"sink_name={name}",
                   f"sink_properties=device.description={description or name}"], check=True)

    def remove_sink(self, name: str) -> None:
        """Выгрузить null-sink по имени: ищем его модуль в списке модулей."""
        for module_id in self._module_ids("module-null-sink", f"sink_name={name}"):
            self._run(["pactl", "unload-module", module_id], check=True)

    # ── петли (аргументы модулей + связи портов) ──
    def module_args(self, module_name: str) -> list[dict]:
        """Аргументы каждого модуля с таким именем: {source:…, sink:…, …}.

        Это ЗАЯВЛЕННОЕ петлёй: с чем её создали. Поля pactl английские только при
        LC_ALL=C — за это отвечает _env().
        """
        out = self._run(["pactl", "list", "modules"])
        found = []
        for block in out.split("Module #")[1:]:
            if f"Name: {module_name}" not in block:
                continue
            raw = re.search(r"^\s*Argument:\s*(.+)$", block, re.M)
            args = {}
            for pair in (raw.group(1).split() if raw else []):
                key, sep, value = pair.partition("=")
                if sep:
                    args[key] = value.strip('"')
            found.append(args)
        return found

    def loop_links(self) -> dict[str, list[str]]:
        """Куда петли подключены ФИЗИЧЕСКИ (связи портов pw-dump): база → устройства.

        Виртуальные узлы (easyeffects_sink и прочая обработка) пропускаем: это
        промежуточная ступень, а не «звук пошёл в колонки». Нужно это ровно для
        бага «петля играет сразу в два устройства».
        """
        try:
            data = json.loads(self._run(["pw-dump"]) or "[]")
        except ValueError:
            return {}
        nodes, links = {}, []
        for obj in data:
            kind = obj.get("type")
            info = obj.get("info") or {}
            if kind == "PipeWire:Interface:Node":
                nodes[obj.get("id")] = (info.get("props") or {}).get("node.name", "")
            elif kind == "PipeWire:Interface:Link":
                links.append((info.get("output-node-id"), info.get("input-node-id")))
        by_base = {}
        for nid, name in nodes.items():
            if not name.startswith("output.loopback"):
                continue
            feeds = [nodes.get(inp, "") for out, inp in links if out == nid]
            physical = [f for f in dict.fromkeys(feeds)
                        if f and classify(f) == PHYSICAL]
            sources = [nodes.get(out, "") for out, inp in links if inp == nid]
            source = next((s for s in sources if s), "")
            if source:
                base = source[:-len(".monitor")] if source.endswith(".monitor") else source
                by_base[base] = list(dict.fromkeys(by_base.get(base, []) + physical))
        return by_base

    def list_loops(self) -> list[dict]:
        """Петли и их РЕАЛЬНЫЕ цели.

        Основа — аргументы module-loopback (source=/sink=): связь с источником и
        заявленное устройство. Дополнительно подмешиваем физические связи портов:
        если петля «прилипла» ещё к одному устройству, владелец услышит звук сразу
        в двух местах, и такой случай обязан быть виден в списке проблем.
        """
        loops = []
        for args in self.module_args("module-loopback"):
            source = args.get("source", "")
            if not source:
                continue
            targets = [args["sink"]] if args.get("sink") else []
            loops.append({"source": source, "targets": targets})
        for base, physical in self.loop_links().items():
            for loop in loops:
                if loop["source"].split(".monitor")[0] == base:
                    loop["targets"] = list(dict.fromkeys(loop["targets"] + physical))
        return loops

    def create_loop(self, source: str, target: str) -> None:
        self._run(["pactl", "load-module", "module-loopback", f"source={source}",
                   f"sink={target}", f"latency_msec={LATENCY_MS}",
                   "source_dont_move=true", "sink_dont_move=true"], check=True)

    def remove_loops(self, sources) -> None:
        for source in sources:
            for module_id in self._module_ids("module-loopback", f"source={source}"):
                self._run(["pactl", "unload-module", module_id], check=True)

    def drop_loop_links(self, target: str) -> None:
        """Снять связи петель с указанного устройства (по FL и FR).

        `pactl move-sink-input` для потока петли бесполезен («Invalid argument») —
        связи рвём только через `pw-link -d` по id узлов из pw-dump.
        """
        try:
            data = json.loads(self._run(["pw-dump"]) or "[]")
        except ValueError:
            return
        nodes, links = {}, []
        for obj in data:
            kind = obj.get("type")
            info = obj.get("info") or {}
            props = info.get("props") or {}
            if kind == "PipeWire:Interface:Node":
                nodes[obj.get("id")] = props.get("node.name", "")
            elif kind == "PipeWire:Interface:Link":
                links.append(info)
        target_ids = {nid for nid, name in nodes.items() if name == target}
        loop_ids = {nid for nid, name in nodes.items() if name.startswith("output.loopback")}
        for link in links:
            out, inp = link.get("output-node-id"), link.get("input-node-id")
            if out in loop_ids and inp in target_ids:
                self._run(["pw-link", "-d", str(out), str(inp)])

    def _module_ids(self, module: str, needle: str) -> list[str]:
        ids = []
        for line in self._run(["pactl", "list", "short", "modules"]).splitlines():
            if module in line and needle in line:
                ids.append(line.split("\t", 1)[0])
        return ids

    # ── потоки ──
    def list_streams(self) -> list[dict]:
        text = self._run(["pactl", "list", "sink-inputs"])
        streams = []
        for block in text.split("Sink Input #")[1:]:
            def prop(key):
                match = re.search(rf"^\s*{key} = \"?([^\"\n]+)\"?", block, re.M)
                return match.group(1).strip() if match else ""
            sink = re.search(r"^\s*Sink:\s*(\S+)", block, re.M)
            streams.append({"id": block.split()[0],
                            "sink": sink.group(1) if sink else "",
                            "app": prop("application.name")})
        return streams

    def move_stream(self, stream_id, target: str) -> None:
        self._run(["pactl", "move-sink-input", str(stream_id), target], check=True)

    # ── служба ──
    def service(self) -> dict:
        enabled = self._run(["systemctl", "--user", "is-enabled", WATCHDOG]).strip() == "enabled"
        active = self._run(["systemctl", "--user", "is-active", WATCHDOG]).strip() == "active"
        return {"enabled": enabled, "active": active}

    def enable_service(self) -> None:
        self._run(["systemctl", "--user", "enable", "--now", WATCHDOG], check=True)

    def disable_service(self) -> None:
        self._run(["systemctl", "--user", "disable", "--now", WATCHDOG], check=True)

    # ── файлы состояния ──
    def read_device(self) -> str:
        try:
            return self.device_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""

    def write_device(self, name: str) -> None:
        self.device_file.parent.mkdir(parents=True, exist_ok=True)
        self.device_file.write_text(f"{name}\n", encoding="utf-8")

    def read_state(self) -> dict:
        data = {}
        try:
            for line in self.state_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key:
                    data[key] = value
        except OSError:
            return {}
        return data

    def write_state(self, data: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(f"{key}={data[key]}\n" for key in ("prev_default", "obs_device", "headphones")
                       if key in data)
        self.state_file.write_text(text, encoding="utf-8")

    def remove_state(self) -> None:
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    # ── OBS ──
    def obs_online(self) -> bool:
        return bool(self.obs is not None and getattr(self.obs, "connected", False))

    async def set_input_device(self, name: str, device: str) -> None:
        """Навести источник OBS на устройство; если источника нет — создать.

        `overlay=True` обновляет только переданный ключ настроек, не затирая
        остальные; на отсутствующем источнике это отказ — тогда создаём.
        """
        settings = {"device_id": device}
        try:
            await self.obs.req("SetInputSettings", inputName=name,
                              inputSettings=settings, overlay=True)
            return
        except Exception:
            pass
        await self.obs.req("CreateInput", inputName=name,
                          inputType=MONITOR_CAPTURE_TYPE, inputSettings=settings)

    async def remove_input(self, name: str) -> None:
        try:
            await self.obs.req("RemoveInput", inputName=name)
        except Exception:
            pass  # источника могло не быть — это не ошибка выключения


# ── управляющий слой ──
class AudioController:
    """Собирает снапшот звука и выполняет переключение режима/устройства.

    ops — системный провайдер (в тестах — подставной), panel — снапшот OBS панели
    (нужен только для чтения obs/эфир/запись). panel допускается None: тогда
    считаем, что эфира нет, а связь с OBS — как скажет провайдер.
    """

    def __init__(self, ops, panel=None, *, clock=time.time):
        self.ops = ops
        self.panel = panel
        self.clock = clock

    # ── чтение состояния ──
    def snapshot(self) -> dict:
        service = self.ops.service() or {}
        mode = compute_mode(service.get("enabled"), service.get("active"))
        sinks = self.ops.list_sinks() or []
        names = [s.get("name") for s in sinks if s.get("name")]
        chosen = choose_device(names, self.ops.read_device())
        default = self.ops.default_sink()
        loops = self.ops.list_loops() or []
        return {
            "mode": mode,
            "service": {"enabled": bool(service.get("enabled")),
                        "active": bool(service.get("active"))},
            "default_sink": default,
            "chosen": chosen,
            "devices": [self._device(s, default, chosen) for s in sinks],
            "mixes": {"present": any(n in MIX_SINKS for n in names), "loops": loops},
            "obs": self._obs(),
            "issues": collect_issues(mode, service, loops, chosen, names),
            "updated_at": self.clock(),
        }

    @staticmethod
    def _device(sink: dict, default: str, chosen: str) -> dict:
        name = sink.get("name") or ""
        return {"name": name,
                "label": sink.get("description") or name,
                "kind": classify(name),
                "state": sink.get("state") or "",
                "is_default": bool(name) and name == default,
                "is_chosen": bool(chosen) and name == chosen}

    def _obs(self) -> dict:
        """Состояние OBS для блока звука.

        Когда OBS офлайн, залипшие от прошлого раза `stream.active`/`record.active`
        не считаются живыми: эфира нет, прерывать нечего. Иначе пульт отказывает в
        переключении режима «идёт эфир» при выключенном OBS, и владелец не может
        вернуть себе звук — а миксы со сторожем продолжают работать.
        """
        state = getattr(self.panel, "state", None) if self.panel is not None else None
        if not isinstance(state, dict):
            return {"online": bool(self.ops.obs_online()),
                    "streaming": False, "recording": False}
        online = bool(state.get("obs"))
        return {"online": online,
                "streaming": online and bool((state.get("stream") or {}).get("active")),
                "recording": online and bool((state.get("record") or {}).get("active"))}

    def _names(self) -> list[str]:
        return [s.get("name") for s in (self.ops.list_sinks() or []) if s.get("name")]

    # ── переключение режима ──
    async def set_mode(self, mode, *, confirm: bool = False) -> dict:
        """Переключить режим; 400 — неизвестный, 409 — эфир без подтверждения."""
        if mode not in (STREAM, NORMAL):
            raise CommandError(f"audio.mode: неизвестный режим {mode!r}")
        obs = self._obs()
        if confirm_required(obs["streaming"], obs["recording"]) and not confirm:
            raise RefusedError("идёт эфир — переключение прервёт звук")
        service = self.ops.service() or {}
        current = compute_mode(service.get("enabled"), service.get("active"))
        if current == mode and not self._mode_is_broken(mode, service):
            return self._noop()  # идемпотентно: режим уже такой и он здоров
        if mode == STREAM:
            return await self._enable_stream()
        return await self._disable_stream()

    def _mode_is_broken(self, mode, service) -> bool:
        """Режим уже выставлен, но состояние нездорово: сторож не работает, петли
        играют не туда или сразу в два места, в обычном режиме остались виртуальные
        выходы. Тогда повторный тап по тому же режиму должен ПОЧИНИТЬ, а не молчать:
        владелец жмёт «Стрим» именно в такой ситуации, и no-op выглядел бы как
        «кнопка не работает». Идемпотентность режима — про здоровое состояние.
        """
        names = self._names()
        chosen = choose_device(names, self.ops.read_device())
        return bool(collect_issues(mode, service, self.ops.list_loops() or [],
                                   chosen, names))

    async def _enable_stream(self) -> dict:
        """Порядок шагов (docs/AUDIO.md): device → sinks → loops → default → obs → watchdog → state."""
        device = choose_device(self._names(), self.ops.read_device())
        previous = self.ops.default_sink()

        async def step_device():
            changed = self._persist_device(device)
            return f"устройство {device or '—'}", changed

        async def step_sinks():
            created = False
            for sink in MIX_SINKS:
                if sink not in self._names():
                    self.ops.create_sink(sink, DESCRIPTIONS.get(sink, sink))
                    created = True
            return f"выходы {', '.join(MIX_SINKS)}", created

        async def step_loops():
            changed = self._apply_loops(device)
            return f"петли → {device or '—'}", changed

        async def step_default():
            if self.ops.default_sink() != MIX_SINKS[0]:
                self.ops.set_default_sink(MIX_SINKS[0])
                return f"выход по умолчанию → {MIX_SINKS[0]}", True
            return f"выход по умолчанию → {MIX_SINKS[0]}", False

        async def step_obs():
            if not self.ops.obs_online():
                return "OBS офлайн — захваты не наведены", False
            await self.ops.set_input_device(SYS_INPUT, f"{MIX_SINKS[1]}.monitor")
            await self.ops.set_input_device(DISCORD_INPUT, f"{MIX_SINKS[2]}.monitor")
            return (f"«{SYS_INPUT}» → {MIX_SINKS[1]}.monitor, "
                    f"«{DISCORD_INPUT}» → {MIX_SINKS[2]}.monitor"), True

        async def step_watchdog():
            self.ops.enable_service()
            return f"служба {WATCHDOG} включена", True

        async def step_state():
            self.ops.write_state({"prev_default": previous,
                                  "obs_device": f"{device}.monitor",
                                  "headphones": device})
            return "записан rz_mix.state", True

        plan = [("device", step_device), ("sinks", step_sinks), ("loops", step_loops),
                ("default", step_default), ("obs", step_obs), ("watchdog", step_watchdog),
                ("state", step_state)]
        return await self._run_plan("audio.mode", plan)

    async def _disable_stream(self) -> dict:
        """Порядок шагов (docs/AUDIO.md): watchdog → default → streams → loops → sinks → obs → state."""
        device = choose_device(self._names(), self.ops.read_device())
        target = device or self.ops.default_sink()

        async def step_watchdog():
            self.ops.disable_service()
            return f"служба {WATCHDOG} выключена", True

        async def step_default():
            if target:
                self.ops.set_default_sink(target)
            return f"выход по умолчанию → {target or '—'}", bool(target)

        async def step_streams():
            moved = 0
            for stream in self.ops.list_streams() or []:
                if stream.get("sink") in MIX_SINKS:
                    self.ops.move_stream(stream.get("id"), target)
                    moved += 1
            return f"потоки уведены в {target or '—'} ({moved})", bool(moved)

        async def step_loops():
            self.ops.remove_loops([f"{sink}.monitor" for sink in MIX_SINKS])
            return "петли выгружены", True

        async def step_sinks():
            removed = False
            for sink in MIX_SINKS:
                if sink in self._names():
                    self.ops.remove_sink(sink)
                    removed = True
            return "виртуальные выходы выгружены", removed

        async def step_obs():
            if not self.ops.obs_online():
                return "OBS офлайн — захваты не тронуты", False
            monitor = f"{device}.monitor" if device else f"{target}.monitor"
            await self.ops.set_input_device(SYS_INPUT, monitor)
            await self.ops.remove_input(DISCORD_INPUT)
            return f"«{SYS_INPUT}» → {monitor}, «{DISCORD_INPUT}» удалён", True

        async def step_state():
            self.ops.remove_state()
            return "rz_mix.state удалён", True

        plan = [("watchdog", step_watchdog), ("default", step_default),
                ("streams", step_streams), ("loops", step_loops), ("sinks", step_sinks),
                ("obs", step_obs), ("state", step_state)]
        return await self._run_plan("audio.mode", plan)

    # ── выбор устройства ──
    async def set_device(self, name) -> dict:
        """Проверить, что выход существует и физический, сохранить выбор и (в стриме)
        перенаправить петли на него, сняв связи со старыми устройствами."""
        if not isinstance(name, str) or not name:
            raise CommandError("audio.device: имя устройства должно быть строкой")
        names = self._names()
        if name not in names:
            raise CommandError(f"audio.device: устройства {name!r} нет")
        if classify(name) != PHYSICAL:
            raise CommandError("audio.device: можно выбрать только физический выход")

        steps, changed = [f"device: выбрано устройство {name}"], []
        if self.ops.read_device() != name:
            self.ops.write_device(name)
            changed.append("device")

        service = self.ops.service() or {}
        if compute_mode(service.get("enabled"), service.get("active")) == STREAM:
            if self._apply_loops(name):
                changed.append("loops")
                steps.append(f"loops: петли перенаправлены → {name}")
            else:
                steps.append(f"loops: петли уже смотрят в {name}")
        return {"ok": True, "name": "audio.device", "changed": changed,
                "steps": steps, "state": self.snapshot(), "error": None}

    # ── внутреннее ──
    def _persist_device(self, device: str) -> bool:
        if device and self.ops.read_device() != device:
            self.ops.write_device(device)
            return True
        return False

    def _apply_loops(self, device: str) -> bool:
        """Привести петли к выбранному устройству: снять связи со сторонними, добить нужную.

        Возврат True, если что-то изменили. Снятие связей с «не тем» физическим
        выходом — лечение бага «звук сразу в наушниках и колонках».
        """
        if not device:
            return False
        changed = False
        existing = {loop.get("source"): list(loop.get("targets") or [])
                    for loop in (self.ops.list_loops() or [])}
        for targets in existing.values():
            for target in targets:
                if target and target != device:
                    self.ops.drop_loop_links(target)
                    changed = True
        for sink in MIX_SINKS:
            source = f"{sink}.monitor"
            if device not in existing.get(source, []):
                self.ops.create_loop(source, device)
                changed = True
        return changed

    async def _run_plan(self, name: str, plan) -> dict:
        """Выполнить шаги по порядку; на сбое — конверт ok:false с тем, что успело сделаться."""
        steps, changed, error = [], [], None
        for key, action in plan:
            try:
                text, did_change = await action()
            except Exception as exc:  # шаг не выполнился — 500 с текстом и steps
                error = f"{key}: {exc}"
                break
            steps.append(f"{key}: {text}")
            if did_change:
                changed.append(key)
        return {"ok": error is None, "name": name, "changed": changed,
                "steps": steps, "state": self.snapshot(), "error": error}

    def _noop(self) -> dict:
        return {"ok": True, "name": "audio.mode", "changed": [], "steps": [],
                "state": self.snapshot(), "error": None}
