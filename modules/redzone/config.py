"""Конфиг модуля RedZone: значения по умолчанию, файл, переменные окружения.

Порядок разрешения (позже перекрывает раньше):

  1. разумные значения по умолчанию (стандартные каталоги OBS, localhost-порты);
  2. файл `~/.config/stream-panel/redzone.json` (путь можно задать `REDZONE_CONFIG`);
  3. переменные окружения `REDZONE_*`.

Никаких абсолютных путей конкретной машины в коде нет: домашний каталог берётся
через `Path.home()`, а всё, что относится к этой машине, живёт в конфиге.

Секреты (ключи хаба, строки доступа) в значениях по умолчанию отсутствуют —
их место в конфиге или в переменных окружения.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── значения по умолчанию ────────────────────────────────────────────────────

CONFIG_ENV = "REDZONE_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "stream-panel" / "redzone.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "stream-panel" / "redzone"

# Имена источников OBS по умолчанию. У владельца сцены уже собраны под эти имена;
# другой пользователь переопределяет их в конфиге в блоке `sources`.
DEFAULT_SOURCES = {
    "background": "RZ · фон",
    "panel": "RZ · плашка",
    "title": "RZ · заголовок",
    "subtitle": "RZ · подпись",
    "webcam": "Вебка",
    "webcam_frame": "Рамка вебки",
    "alerts": "Алерты (фоллоу)",
    "chat": "Чат (заготовка)",
    "chat_rz": "Чат RZ",
    "grid": "сетка",
    "bracket": "Турнирная сетка",
    "local_game": "Захват игры (Vulkan)",
    "system_audio": "Звук системы",
    "discord_audio": "Дискорд",
    "microphone": "Микрофон",
}

# Шаблоны имён источников карт: {n} — номер карты.
DEFAULT_CARD_SOURCES = {
    "stream": "RZ К{n} эфир",
    "game": "RZ К{n} игра",
    "frame": "RZ К{n} рамка",
    "focus": "RZ К{n} рамка фокус",
    "label": "RZ К{n} подпись",
    "hub": "RZ К{n} хаб",
}

# Сцены раскладки: имя -> номер карты в фокусе, [миниатюры по порядку полосы].
DEFAULT_MOSAIC_SCENES = {
    "RZ3 · 1": (1, [2, 3]),
    "RZ3 · 2": (2, [3, 1]),
    "RZ3 · 3": (3, [1, 2]),
}
DEFAULT_SOLO_SCENES = {"RZ · К1": 1, "RZ · К2": 2, "RZ · К3": 3}

# Хоткеи сцен раскладки (плоские модификаторы: control=True — Ctrl+Num).
DEFAULT_HOTKEYS = {
    "RZ3 · 1": "OBS_KEY_NUM1",
    "RZ3 · 2": "OBS_KEY_NUM2",
    "RZ3 · 3": "OBS_KEY_NUM3",
}
DEFAULT_SCENE_ORDER = ["RZ3 · 1", "RZ3 · 2", "RZ3 · 3", "RZ · К1", "RZ · К2", "RZ · К3"]

# Сцены, в которые модуль раскладывает колонку (сетка + чат) и звук.
DEFAULT_COLUMN_SCENES = ["RZ3 · 1", "RZ3 · 2", "RZ3 · 3"]


def _flat(data: dict, prefix: str = "") -> dict[str, Any]:
    """Разворачивает вложенный конфиг в плоские ключи через точку."""
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flat(value, path))
        else:
            out[path] = value
    return out


def _as_path(value: Any, default: Path) -> Path:
    if value is None or value == "":
        return default
    return Path(str(value)).expanduser()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Разрешённый конфиг модуля: плоские поля, всё уже с раскрытым `~`."""

    config_file: Path
    state_dir: Path
    tiles_dir: Path
    overlay_dir: Path

    obs_host: str = "127.0.0.1"
    obs_port: int = 4455
    obs_ws_config: Path = field(default_factory=lambda: (
        Path.home() / ".config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"))
    scenes_dir: Path = field(default_factory=lambda: (
        Path.home() / ".config" / "obs-studio" / "basic" / "scenes"))
    collection: str = "Стрим"
    spare_collection: str = "Безымянный"

    chat_host: str = "127.0.0.1"
    chat_port: int = 8097
    chat_channel: str = ""
    chat_unit: str = "rz-chat.service"
    bracket_timer: str = "rz-bracket.timer"

    # правая колонка сцены: сетка сверху, чат снизу
    column_x: int = 2024
    column_width: int = 420
    column_bracket_y: int = 632
    column_bracket_height: int = 460
    column_chat_y: int = 1100
    column_chat_height: int = 340

    bracket_url: str = ""
    bracket_chrome: str = "google-chrome"
    bracket_width: int = 420
    bracket_height: int = 460

    hub_api: str = "http://127.0.0.1:9997"
    hub_rtsp: str = "rtsp://127.0.0.1:8554"
    hub_rtmp: str = "rtmp://127.0.0.1:1935"
    hub_webrtc: str = "http://127.0.0.1:8889"
    hub_program: str = "program"
    hub_card: str = "card{n}"
    hub_config: Path = field(default_factory=lambda: DEFAULT_STATE_DIR / "mediamtx.yml")
    hub_credentials: Path = field(default_factory=lambda: DEFAULT_STATE_DIR / "credentials.txt")
    hub_compose_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    hub_users: dict[str, str] = field(default_factory=dict)

    cocast_relay_unit: str = "rz-cocast-relay.service"
    cocast_target: Path = field(default_factory=lambda: DEFAULT_STATE_DIR / "twitch-target.json")
    cocast_public_host: str = ""

    audio_unit: str = "rz-mix.service"
    audio_all_sink: str = "rz_mix"
    audio_game_sink: str = "game_mix"
    audio_discord_sink: str = "discord_mix"
    audio_headphones_hint: str = ""
    audio_dota_watch: bool = True

    sources: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SOURCES))
    card_sources: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CARD_SOURCES))
    mosaic_scenes: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_MOSAIC_SCENES))
    solo_scenes: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SOLO_SCENES))
    hotkeys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))
    scene_order: list[str] = field(default_factory=lambda: list(DEFAULT_SCENE_ORDER))
    column_scenes: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMN_SCENES))

    # ── производные имена источников ──
    def card(self, kind: str, n: int) -> str:
        """Имя источника карты: `cfg.card("stream", 2)` → «RZ К2 эфир»."""
        return self.card_sources[kind].format(n=n)

    def src(self, name: str) -> str:
        """Имя общего источника: `cfg.src("webcam")` → «Вебка»."""
        return self.sources[name]

    @property
    def bracket_output(self) -> Path:
        return self.state_dir / "bracket.png"

    @property
    def audio_device_file(self) -> Path:
        return self.state_dir / "device"

    def path(self, name: str) -> Path:
        """Путь внутри каталога состояния (для файлов модуля)."""
        return self.state_dir / name

    def script_env(self) -> dict[str, str]:
        """Окружение для вспомогательных скриптов: пути и параметры из конфига."""
        return {
            **os.environ,
            "REDZONE_CONFIG": str(self.config_file),
            "REDZONE_STATE_DIR": str(self.state_dir),
            "REDZONE_OBS_WS_CONFIG": str(self.obs_ws_config),
            "REDZONE_AUDIO_UNIT": self.audio_unit,
            "REDZONE_AUDIO_ALL_SINK": self.audio_all_sink,
            "REDZONE_AUDIO_GAME_SINK": self.audio_game_sink,
            "REDZONE_AUDIO_DISCORD_SINK": self.audio_discord_sink,
            "REDZONE_AUDIO_HEADPHONES_HINT": self.audio_headphones_hint,
            "REDZONE_HUB_API": self.hub_api,
            "REDZONE_HUB_RTSP": self.hub_rtsp,
            "REDZONE_STREAM_HOST": self.cocast_public_host,
            "REDZONE_COCAST_TARGET": str(self.cocast_target),
            "REDZONE_COCAST_OUTPUT_UNIT": self.cocast_relay_unit,
        }


def _build(flat: dict[str, Any], config_file: Path, env: dict[str, str]) -> Config:
    """Собирает Config из плоского словаря конфига и окружения."""
    def pick(env_name: str, key: str, default: Any) -> Any:
        if env_name in env:
            return env[env_name]
        if key in flat:
            return flat[key]
        return default

    def path_of(key: str, env_name: str, default: Path) -> Path:
        if env_name in env:
            return Path(env[env_name]).expanduser()
        return _as_path(flat.get(key), default)

    state_dir = path_of("paths.state_dir", "REDZONE_STATE_DIR", DEFAULT_STATE_DIR)
    module_dir = Path(__file__).resolve().parent

    def dict_of(key: str, default: dict) -> dict:
        merged = dict(default)
        value = flat.get(key)
        if isinstance(value, dict):
            merged.update({str(k): v for k, v in value.items()})
        return merged

    def list_of(key: str, default: list) -> list:
        value = flat.get(key)
        return list(value) if isinstance(value, list) else list(default)

    return Config(
        config_file=config_file,
        state_dir=state_dir,
        tiles_dir=path_of("paths.tiles_dir", "REDZONE_TILES_DIR", module_dir / "tiles"),
        overlay_dir=path_of("paths.overlay_dir", "REDZONE_OVERLAY_DIR", module_dir / "overlay"),
        obs_host=str(pick("REDZONE_OBS_HOST", "obs.host", "127.0.0.1")),
        obs_port=_as_int(pick("REDZONE_OBS_PORT", "obs.port", 4455), 4455),
        obs_ws_config=path_of("obs.ws_config", "REDZONE_OBS_WS_CONFIG",
                              Config.__dataclass_fields__["obs_ws_config"].default_factory()),
        scenes_dir=path_of("obs.scenes_dir", "REDZONE_SCENES_DIR",
                           Config.__dataclass_fields__["scenes_dir"].default_factory()),
        collection=str(pick("REDZONE_OBS_COLLECTION", "obs.collection", "Стрим")),
        spare_collection=str(pick("REDZONE_OBS_SPARE_COLLECTION", "obs.spare_collection", "Безымянный")),
        chat_host=str(pick("REDZONE_CHAT_HOST", "chat.host", "127.0.0.1")),
        chat_port=_as_int(pick("REDZONE_CHAT_PORT", "chat.port", 8097), 8097),
        chat_channel=str(pick("REDZONE_CHAT_CHANNEL", "chat.channel", "")),
        chat_unit=str(pick("REDZONE_CHAT_UNIT", "chat.unit", "rz-chat.service")),
        bracket_timer=str(pick("REDZONE_BRACKET_TIMER", "bracket.timer", "rz-bracket.timer")),
        column_x=_as_int(pick("REDZONE_COLUMN_X", "column.x", 2024), 2024),
        column_width=_as_int(pick("REDZONE_COLUMN_WIDTH", "column.width", 420), 420),
        column_bracket_y=_as_int(pick("REDZONE_COLUMN_BRACKET_Y", "column.bracket_y", 632), 632),
        column_bracket_height=_as_int(pick("REDZONE_COLUMN_BRACKET_HEIGHT", "column.bracket_height", 460), 460),
        column_chat_y=_as_int(pick("REDZONE_COLUMN_CHAT_Y", "column.chat_y", 1100), 1100),
        column_chat_height=_as_int(pick("REDZONE_COLUMN_CHAT_HEIGHT", "column.chat_height", 340), 340),
        bracket_url=str(pick("REDZONE_BRACKET_URL", "bracket.url", "")),
        bracket_chrome=str(pick("REDZONE_BRACKET_CHROME", "bracket.chrome", "google-chrome")),
        bracket_width=_as_int(pick("REDZONE_BRACKET_WIDTH", "bracket.width", 420), 420),
        bracket_height=_as_int(pick("REDZONE_BRACKET_HEIGHT", "bracket.height", 460), 460),
        hub_api=str(pick("REDZONE_HUB_API", "hub.api", "http://127.0.0.1:9997")),
        hub_rtsp=str(pick("REDZONE_HUB_RTSP", "hub.rtsp", "rtsp://127.0.0.1:8554")),
        hub_rtmp=str(pick("REDZONE_HUB_RTMP", "hub.rtmp", "rtmp://127.0.0.1:1935")),
        hub_webrtc=str(pick("REDZONE_HUB_WEBRTC", "hub.webrtc", "http://127.0.0.1:8889")),
        hub_program=str(pick("REDZONE_HUB_PROGRAM", "hub.program", "program")),
        hub_card=str(pick("REDZONE_HUB_CARD", "hub.card", "card{n}")),
        hub_config=path_of("hub.config", "REDZONE_HUB_CONFIG", state_dir / "mediamtx.yml"),
        hub_credentials=path_of("hub.credentials", "REDZONE_HUB_CREDENTIALS",
                                state_dir / "credentials.txt"),
        hub_compose_dir=path_of("hub.compose_dir", "REDZONE_HUB_COMPOSE_DIR", state_dir),
        hub_users=dict_of("hub.users", {}),
        cocast_relay_unit=str(pick("REDZONE_COCAST_RELAY_UNIT", "cocast.relay_unit",
                                   "rz-cocast-relay.service")),
        cocast_target=path_of("cocast.target", "REDZONE_COCAST_TARGET",
                              state_dir / "twitch-target.json"),
        cocast_public_host=str(pick("REDZONE_STREAM_HOST", "cocast.public_host", "")),
        audio_unit=str(pick("REDZONE_AUDIO_UNIT", "audio.unit", "rz-mix.service")),
        audio_all_sink=str(pick("REDZONE_AUDIO_ALL_SINK", "audio.all_sink", "rz_mix")),
        audio_game_sink=str(pick("REDZONE_AUDIO_GAME_SINK", "audio.game_sink", "game_mix")),
        audio_discord_sink=str(pick("REDZONE_AUDIO_DISCORD_SINK", "audio.discord_sink", "discord_mix")),
        audio_headphones_hint=str(pick("REDZONE_AUDIO_HEADPHONES_HINT", "audio.headphones_hint", "")),
        audio_dota_watch=_as_bool(pick("REDZONE_AUDIO_DOTA_WATCH", "audio.dota_watch", True), True),
        sources=dict_of("sources", DEFAULT_SOURCES),
        card_sources=dict_of("cards", DEFAULT_CARD_SOURCES),
        mosaic_scenes=dict_of("scenes.mosaic", DEFAULT_MOSAIC_SCENES),
        solo_scenes=dict_of("scenes.solo", DEFAULT_SOLO_SCENES),
        hotkeys=dict_of("scenes.hotkeys", DEFAULT_HOTKEYS),
        scene_order=list_of("scenes.order", DEFAULT_SCENE_ORDER),
        column_scenes=list_of("scenes.column", DEFAULT_COLUMN_SCENES),
    )


def load(path: Path | str | None = None, env: dict[str, str] | None = None) -> Config:
    """Читает конфиг и возвращает готовый `Config`.

    Отсутствие или порча файла конфига не роняют модуль: остаются значения по
    умолчанию и переменные окружения. Явно указанный путь (`--config`), которого
    нет, — это ошибка: пользователь ждал прочитать именно его.
    """
    env = dict(os.environ if env is None else env)
    explicit = path is not None
    config_file = Path(path).expanduser() if path else Path(
        env.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH).expanduser()

    data: dict[str, Any] = {}
    if config_file.exists():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if explicit:
                raise SystemExit(f"конфиг {config_file}: не читается ({exc})")
            data = {}
    elif explicit:
        raise SystemExit(f"конфиг {config_file}: файла нет")

    return _build(_flat(data if isinstance(data, dict) else {}), config_file, env)
