"""HTTP-API пульта: доступ, форма снапшота, команды, статика (integration)."""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from server.app import make_guard
from tests.http_support import TOKEN, auth, make_stack, running

pytestmark = pytest.mark.integration

SNAP_KEYS = {"obs", "error", "scene", "scenes", "stream", "record", "replay",
             "audio", "stale", "updated_at"}


async def test_state_without_token_401():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        assert (await cl.get("/api/state")).status == 401


async def test_state_wrong_token_401():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        assert (await cl.get("/api/state", headers=auth("не-тот"))).status == 401


async def test_state_ok_and_snapshot_shape():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        resp = await cl.get("/api/state", headers=auth())
        assert resp.status == 200
        snap = await resp.json()
        assert SNAP_KEYS <= set(snap)
        assert snap["obs"] is True


async def test_cmd_without_and_wrong_token_401():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        body = {"name": "panic"}
        assert (await cl.post("/api/cmd", json=body)).status == 401
        assert (await cl.post("/api/cmd", json=body, headers=auth("не-тот"))).status == 401


async def test_cmd_success_returns_ok_true():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        resp = await cl.post("/api/cmd",
                             json={"name": "audio.toggleMute", "args": {"track": "mic"}},
                             headers=auth())
        assert resp.status == 200
        env = await resp.json()
        assert env["ok"] is True
        assert env["name"] == "audio.toggleMute"
        assert SNAP_KEYS <= set(env["state"])


async def test_cmd_unknown_400():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        resp = await cl.post("/api/cmd", json={"name": "nope"}, headers=auth())
        assert resp.status == 400
        env = await resp.json()
        assert env["ok"] is False and env["error"]


async def test_cmd_refused_409():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        resp = await cl.post("/api/cmd", json={"name": "stream.start", "args": {}},
                             headers=auth())
        assert resp.status == 409
        env = await resp.json()
        assert env["ok"] is False and env["error"]


async def test_cmd_garbage_bodies_400_not_500():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        # пустой объект: нет name
        assert (await cl.post("/api/cmd", json={}, headers=auth())).status == 400
        # не JSON
        resp = await cl.post("/api/cmd", data="это не json",
                             headers={**auth(), "Content-Type": "application/json"})
        assert resp.status == 400
        # name не строка
        assert (await cl.post("/api/cmd", json={"name": 123}, headers=auth())).status == 400
        # args не объект
        assert (await cl.post("/api/cmd", json={"name": "panic", "args": 5},
                              headers=auth())).status == 400


async def test_index_stub_without_token_and_without_secret():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        resp = await cl.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "фронт ещё не собран" in text
        assert TOKEN not in text


async def test_serves_built_frontend(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>собранный фронт</html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")

    app, obs, panel = await make_stack(frontend_dir=dist)
    async with running(app) as cl:
        assert (await cl.get("/")).status == 200
        assert "собранный фронт" in await (await cl.get("/")).text()
        js = await cl.get("/app.js")
        assert js.status == 200 and "console.log" in await js.text()
        # без токена, как и корень
        assert (await cl.get("/app.js")).status == 200
        # неизвестного файла нет
        assert (await cl.get("/нет-такого.js")).status == 404


async def test_unknown_path_404():
    app, obs, panel = await make_stack()
    async with running(app) as cl:
        assert (await cl.get("/нет-страницы", headers=auth())).status == 404


async def test_outside_allowlist_403_even_with_token():
    guard = make_guard(TOKEN)
    req = make_mocked_request("GET", "/api/state", headers=auth()).clone(remote="8.8.8.8")

    async def handler(request):
        return web.Response(text="ok")

    resp = await guard(req, handler)
    assert resp.status == 403


async def test_allowlist_loopback_passes():
    guard = make_guard(TOKEN)
    req = make_mocked_request("GET", "/api/state", headers=auth()).clone(remote="127.0.0.1")

    async def handler(request):
        return web.Response(text="ok")

    resp = await guard(req, handler)
    assert resp.status == 200


async def test_allowlist_403_without_token_first():
    """Вне allowlist — 403, даже если токена нет (allowlist раньше авторизации)."""
    guard = make_guard(TOKEN)
    req = make_mocked_request("GET", "/api/state").clone(remote="8.8.8.8")

    async def handler(request):
        return web.Response(text="ok")

    assert (await guard(req, handler)).status == 403
