"""Чтение config.json obs-websocket: порт и пароль (unit, без сети)."""
from __future__ import annotations

import json

import pytest

from server.obs import load_connection

pytestmark = pytest.mark.unit


def test_reads_port_and_password(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_port": 4466, "server_password": "секрет"}),
                   encoding="utf-8")
    assert load_connection(cfg) == ("127.0.0.1", 4466, "секрет")


def test_missing_file_returns_defaults(tmp_path):
    assert load_connection(tmp_path / "нет-такого.json") == ("127.0.0.1", 4455, "")


def test_broken_json_returns_defaults(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{это не json", encoding="utf-8")
    assert load_connection(cfg) == ("127.0.0.1", 4455, "")


def test_empty_password_becomes_empty_string(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_port": 4455, "server_password": None}), encoding="utf-8")
    assert load_connection(cfg) == ("127.0.0.1", 4455, "")


def test_missing_password_key_returns_empty(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_port": 4455}), encoding="utf-8")
    assert load_connection(cfg) == ("127.0.0.1", 4455, "")
