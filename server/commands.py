"""Слой команд пульта: валидация, отказы и выполнение поверх Panel.

Здесь нет ни HTTP, ни транспорта: Commands получает объект с `connected` и
`async req(...)` (клиент OBS) и Panel со снапшотом состояния. Наружу торчит один
метод `execute(name, args)`, который возвращает конверт
`{ok, name, changed, state, error}` (+ `steps` у «Паники»).

Два класса ошибок несут HTTP-код для верхнего слоя: CommandError = 400 (команда
неизвестна или аргументы неверны), RefusedError = 409 (команда неуместна в
текущем состоянии: нет подтверждения, OBS офлайн, данные устарели, буфер повтора
выключен). После успешной команды слой делает полный ресинк (F3) и возвращает
свежий снапшот; после отказа к OBS не ходит вовсе.
"""
from __future__ import annotations

# Сцена «Паника»: имя из коллекции «Стрим» (ТЗ §8.6).
BLACK_SCENE = "Чёрный экран"

# Разрушительные команды: при устаревших данных отказывают (ТЗ §8.2).
DESTRUCTIVE = frozenset({"stream.start", "stream.stop", "record.toggle", "replay.save"})

# Команды без аргументов.
NO_ARGS = frozenset({"record.toggle", "replay.save", "panic"})

# Команды транспорта: единственный допустимый ключ — confirm.
CONFIRM_ARGS = frozenset({"stream.start", "stream.stop"})

TRACKS = frozenset({"mic", "sys"})

COMMANDS = frozenset({
    "scene.set", "stream.start", "stream.stop", "record.toggle", "replay.save",
    "audio.toggleMute", "audio.setVolume", "panic",
})

# Команда -> имя метода-обработчика.
DISPATCH = {
    "scene.set": "_scene_set",
    "stream.start": "_stream_start",
    "stream.stop": "_stream_stop",
    "record.toggle": "_record_toggle",
    "replay.save": "_replay_save",
    "audio.toggleMute": "_audio_toggle_mute",
    "audio.setVolume": "_audio_set_volume",
    "panic": "_panic",
}


class CommandError(Exception):
    """400: команда неизвестна или аргументы неверны."""

    code = 400


class RefusedError(CommandError):
    """409: команда неуместна в текущем состоянии (но команда известна)."""

    code = 409


class Commands:
    """Исполнитель команд: валидирует, дёргает OBS, синхронизирует Panel.

    obs — клиент с `connected` и `async req(request_type, **data)`;
    panel — снапшот состояния (источник истины о текущем режиме OBS).
    """

    def __init__(self, obs, panel, *, black_scene: str = BLACK_SCENE):
        self.obs = obs
        self.panel = panel
        self.black_scene = black_scene

    async def execute(self, name: str, args: dict | None = None) -> dict:
        """Выполнить команду и вернуть конверт; отказы — исключениями.

        Конверт: {ok, name, changed, state, error} (+ steps у «Паники»).
        """
        if not isinstance(name, str) or name not in COMMANDS:
            raise CommandError(f"неизвестная команда: {name!r}")
        clean = self._clean_args(name, args)
        if not self.obs.connected:
            raise RefusedError("OBS офлайн: команды не буферизуются")
        return await getattr(self, DISPATCH[name])(clean)

    # ── валидация аргументов ──
    def _clean_args(self, name: str, args) -> dict:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise CommandError(
                f"{name}: аргументы должны быть объектом, а не {type(args).__name__}")

        if name in NO_ARGS:
            if args:
                raise CommandError(f"{name}: лишние аргументы: {self._keys(args)}")
            return {}

        if name in CONFIRM_ARGS:
            extra = set(args) - {"confirm"}
            if extra:
                raise CommandError(f"{name}: лишние аргументы: {self._keys(extra)}")
            return args

        if name == "scene.set":
            self._exact_keys(args, name, {"name"})
            if not isinstance(args["name"], str):
                raise CommandError("scene.set: name должен быть строкой")
            return args

        if name == "audio.toggleMute":
            self._exact_keys(args, name, {"track"})
            self._check_track(args["track"])
            return args

        if name == "audio.setVolume":
            self._exact_keys(args, name, {"track", "value"})
            self._check_track(args["track"])
            self._check_value(args["value"])
            return args

        raise CommandError(f"неизвестная команда: {name!r}")  # недостижимо, защита

    @staticmethod
    def _keys(keys) -> str:
        return ", ".join(sorted(str(k) for k in keys))

    def _exact_keys(self, args: dict, name: str, required: set) -> None:
        extra = set(args) - required
        missing = required - set(args)
        if extra:
            raise CommandError(f"{name}: лишние аргументы: {self._keys(extra)}")
        if missing:
            raise CommandError(f"{name}: не хватает аргументов: {self._keys(missing)}")

    @staticmethod
    def _check_track(track) -> None:
        if track not in TRACKS:
            raise CommandError("track должен быть 'mic' или 'sys'")

    @staticmethod
    def _check_value(value) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CommandError("value должен быть числом 0..100")
        if not 0 <= value <= 100:
            raise CommandError("value вне диапазона 0..100")

    # ── команды ──
    async def _scene_set(self, args: dict) -> dict:
        name = args["name"]
        known = {s.get("name") for s in (self.panel.state.get("scenes") or [])}
        if name not in known:
            raise CommandError(f"scene.set: сцены {name!r} нет в списке")
        _, error = await self._send("SetCurrentProgramScene", sceneName=name)
        if error is not None:
            return self._failed("scene.set", error)
        return await self._success("scene.set", ["scene"])

    async def _stream_start(self, args: dict) -> dict:
        if args.get("confirm") is not True:
            raise RefusedError("stream.start: нет подтверждения (confirm: true)")
        self._guard_stale()
        if bool((self.panel.state.get("stream") or {}).get("active")):
            return self._noop("stream.start")
        _, error = await self._send("StartStream")
        if error is not None:
            return self._failed("stream.start", error)
        return await self._success("stream.start", ["stream"])

    async def _stream_stop(self, args: dict) -> dict:
        if args.get("confirm") is not True:
            raise RefusedError("stream.stop: нет подтверждения (confirm: true)")
        self._guard_stale()
        if not bool((self.panel.state.get("stream") or {}).get("active")):
            return self._noop("stream.stop")
        _, error = await self._send("StopStream")
        if error is not None:
            return self._failed("stream.stop", error)
        return await self._success("stream.stop", ["stream"])

    async def _record_toggle(self, args: dict) -> dict:
        self._guard_stale()
        active = bool((self.panel.state.get("record") or {}).get("active"))
        _, error = await self._send("StopRecord" if active else "StartRecord")
        if error is not None:
            return self._failed("record.toggle", error)
        return await self._success("record.toggle", ["record"])

    async def _replay_save(self, args: dict) -> dict:
        self._guard_stale()
        if not self.panel.state.get("replay"):
            raise RefusedError("replay.save: буфер повтора выключен")
        _, error = await self._send("SaveReplayBuffer")
        if error is not None:
            return self._failed("replay.save", error)
        return await self._success("replay.save", ["replay"])

    async def _audio_toggle_mute(self, args: dict) -> dict:
        key = args["track"]
        name = self._input_name(key)
        answer, error = await self._send("GetInputMute", inputName=name)
        if error is not None:
            return self._failed("audio.toggleMute", error)
        muted = bool((answer or {}).get("inputMuted"))
        _, error = await self._send("SetInputMute", inputName=name, inputMuted=not muted)
        if error is not None:
            return self._failed("audio.toggleMute", error)
        return await self._success("audio.toggleMute", [f"audio.{key}"])

    async def _audio_set_volume(self, args: dict) -> dict:
        key = args["track"]
        name = self._input_name(key)
        _, error = await self._send("SetInputVolume", inputName=name,
                                 inputVolumeMul=args["value"] / 100.0)
        if error is not None:
            return self._failed("audio.setVolume", error)
        return await self._success("audio.setVolume", [f"audio.{key}"])

    async def _panic(self, args: dict) -> dict:
        """Все три шага независимо: успешные не откатываются при отказе других."""
        steps = {
            "scene": await self._step("SetCurrentProgramScene", sceneName=self.black_scene),
            "mic": await self._step("SetInputMute", inputName=self.panel.mic, inputMuted=True),
            "sys": await self._step("SetInputMute", inputName=self.panel.sys, inputMuted=True),
        }
        failed = [key for key, value in steps.items() if value is not True]
        ok = not failed
        error = None if ok else "Паника: не удалось — " + "; ".join(
            f"{key}: {steps[key]}" for key in failed)
        state = await self.panel.refresh()
        return {"ok": ok, "name": "panic", "changed": ["panic"],
                "state": state, "error": error, "steps": steps}

    # ── выполнение запроса к OBS ──
    async def _send(self, request_type: str, **data):
        """Дёрнуть OBS: при успехе — (ответ, None), при отказе — (None, текст).

        Ответ возвращается вызывающему, а не хранится в поле объекта: команды
        может запускать параллельно HTTP-слой, и общий атрибут сломается на первом
        же добавленном между чтением и использованием `await` (например, записи в
        журнал). Локальное значение такой возможности не оставляет.
        """
        try:
            return await self.obs.req(request_type, **data), None
        except Exception as exc:  # таймаут/обрыв/отказ OBS — провал команды
            return None, str(exc) or exc.__class__.__name__

    async def _step(self, request_type: str, **data):
        """Шаг «Паники»: True либо текст ошибки — без исключений наружу."""
        _, error = await self._send(request_type, **data)
        return True if error is None else error

    # ── сборка конверта ──
    async def _success(self, name: str, changed: list[str]) -> dict:
        state = await self.panel.refresh()  # F3: после команды — полный ресинк
        return {"ok": True, "name": name, "changed": list(changed),
                "state": state, "error": None}

    def _noop(self, name: str) -> dict:
        return {"ok": True, "name": name, "changed": [],
                "state": self.panel.state, "error": None}

    def _failed(self, name: str, error: str) -> dict:
        return {"ok": False, "name": name, "changed": [],
                "state": self.panel.state, "error": error}

    def _guard_stale(self) -> None:
        if self.panel.is_stale():
            raise RefusedError("данные устарели: разрушительная команда отклонена")

    def _input_name(self, key: str) -> str:
        return self.panel.mic if key == "mic" else self.panel.sys
