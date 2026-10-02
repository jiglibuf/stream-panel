"""Журнал на HTTP-слое: команды, доступ, устойчивость к сбою ФС (integration)."""
from __future__ import annotations

import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from server.app import make_guard
from server.journal import Journal
from tests.http_support import TOKEN, auth, make_stack, running

pytestmark = pytest.mark.integration


class FailingJournal:
    """Журнал, запись в который всегда падает (эмуляция ФС только для чтения)."""

    def entry(self, **fields):
        return {"time": "1970-01-01T00:00:00+00:00", **fields}

    async def write(self, entry):
        raise OSError("read-only file system")


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


async def test_command_success_is_logged(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    app, obs, panel = await make_stack(journal=journal)
    async with running(app) as cl:
        resp = await cl.post("/api/cmd",
                             json={"name": "audio.toggleMute", "args": {"track": "mic"}},
                             headers=auth())
        assert resp.status == 200

    records = _records(tmp_path / "j.jsonl")
    last = records[-1]
    assert last["action"] == "audio.toggleMute"
    assert last["result"] == "ok"
    assert last["source"]
    assert isinstance(last["duration_ms"], int)
    assert last["changed"] == ["audio.mic"]
    assert last["error"] is None


async def test_refused_command_is_logged(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    app, obs, panel = await make_stack(journal=journal)
    async with running(app) as cl:
        resp = await cl.post("/api/cmd",
                             json={"name": "stream.start", "args": {}},
                             headers=auth())
        assert resp.status == 409

    last = _records(tmp_path / "j.jsonl")[-1]
    assert last["action"] == "stream.start"
    assert last["result"] == "refused"
    assert last["error"]


async def test_access_denied_and_foreign_are_logged_without_token(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    app, obs, panel = await make_stack(journal=journal)
    async with running(app) as cl:
        # 401: токена нет
        assert (await cl.post("/api/cmd", json={"name": "panic"})).status == 401

    records = _records(tmp_path / "j.jsonl")
    denied = records[-1]
    assert denied["action"] == "auth"
    assert denied["result"] == "denied"
    assert denied["path"] == "/api/cmd"
    assert denied["source"]

    # 403: чужой адрес — тем же guard с журналом
    guard = make_guard(TOKEN, journal)

    async def handler(request):
        return web.Response(text="ok")

    req = make_mocked_request("GET", "/api/state",
                              headers=auth()).clone(remote="8.8.8.8")
    assert (await guard(req, handler)).status == 403

    foreign = _records(tmp_path / "j.jsonl")[-1]
    assert foreign["action"] == "auth"
    assert foreign["result"] == "foreign"
    assert foreign["path"] == "/api/state"
    assert TOKEN not in (tmp_path / "j.jsonl").read_text(encoding="utf-8")


async def test_401_without_token_is_recorded(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    app, obs, panel = await make_stack(journal=journal)
    async with running(app) as cl:
        assert (await cl.get("/api/state")).status == 401

    records = _records(tmp_path / "j.jsonl")
    assert records and records[-1]["result"] == "denied"


async def test_write_failure_does_not_affect_response(tmp_path, caplog):
    app, obs, panel = await make_stack(journal=FailingJournal())
    with caplog.at_level(logging.ERROR, logger="panel.journal"):
        async with running(app) as cl:
            ok = await cl.post("/api/cmd",
                               json={"name": "audio.toggleMute", "args": {"track": "mic"}},
                               headers=auth())
            assert ok.status == 200
            refused = await cl.post("/api/cmd", json={"name": "stream.start", "args": {}},
                                    headers=auth())
            assert refused.status == 409
            # сервис жив
            assert (await cl.get("/api/state", headers=auth())).status == 200

    assert any(r.name == "panel.journal" for r in caplog.records)


async def test_query_token_never_reaches_journal(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    # Токен в query не совпадает с ожидаемым -> отказ, но путь должен быть чистым.
    guard = make_guard("правильный-токен", journal)

    async def handler(request):
        return web.Response(text="ok")

    req = make_mocked_request("GET", "/ws?token=" + TOKEN).clone(remote="127.0.0.1")
    assert (await guard(req, handler)).status == 401

    text = (tmp_path / "j.jsonl").read_text(encoding="utf-8")
    record = json.loads(text.splitlines()[-1])
    assert record["path"] == "/ws"
    assert TOKEN not in text
    assert "token=" not in text
