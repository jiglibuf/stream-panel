"""Токен не должен попадать в логи (в т.ч. access-лог с query-строкой)."""
from __future__ import annotations

import logging

import aiohttp
import pytest

from tests.http_support import TOKEN, make_stack, running

pytestmark = pytest.mark.integration


async def test_access_log_never_contains_token(caplog):
    app, obs, panel = await make_stack()

    with caplog.at_level(logging.INFO, logger="aiohttp.access"):
        caplog.set_level(logging.DEBUG, logger="aiohttp.web")
        async with running(app) as cl:
            # токен в query у /api/* (штатно читается из заголовка) — классический
            # случай утечки через access-лог: строка запроса логируется целиком.
            resp = await cl.get("/api/state?token=" + TOKEN)
            assert resp.status == 401

            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(cl.server.make_url("/ws?token=" + TOKEN))
                await ws.receive_json()
                await ws.close()

    assert TOKEN not in caplog.text
    assert "token=" not in caplog.text
