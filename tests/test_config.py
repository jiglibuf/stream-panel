"""Конфиг и токен пульта: создание, права 0600, переиспользование (unit)."""
from __future__ import annotations

import json
import stat

import pytest

from server.config import DEFAULT_CONFIG_PATH, load_or_create

pytestmark = pytest.mark.unit


def test_creates_dir_file_token_and_0600(tmp_path):
    path = tmp_path / "вложенный" / "config.json"
    cfg = load_or_create(path)

    assert path.exists()
    assert isinstance(cfg["token"], str) and len(cfg["token"]) >= 32
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == 8090

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["token"] == cfg["token"]


def test_default_path_is_project_root():
    assert DEFAULT_CONFIG_PATH.name == "config.json"
    assert DEFAULT_CONFIG_PATH.parent.name == "obs-panel"


def test_reuses_existing_token_and_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"token": "уже-есть", "host": "127.0.0.1", "port": 9001}),
                    encoding="utf-8")

    cfg = load_or_create(path)

    assert cfg["token"] == "уже-есть"
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 9001


def test_token_not_regenerated_on_second_read(tmp_path):
    path = tmp_path / "config.json"
    first = load_or_create(path)["token"]
    second = load_or_create(path)["token"]
    assert first == second


def test_broken_json_is_recreated_without_raising(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{это не json", encoding="utf-8")

    cfg = load_or_create(path)

    assert cfg["token"] and cfg["host"] == "0.0.0.0" and cfg["port"] == 8090
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == cfg["token"]


def test_missing_host_port_get_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"token": "т"}), encoding="utf-8")

    cfg = load_or_create(path)

    assert cfg["token"] == "т"
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == 8090


def test_non_object_json_is_recreated(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    cfg = load_or_create(path)

    assert isinstance(cfg, dict) and cfg["token"]
