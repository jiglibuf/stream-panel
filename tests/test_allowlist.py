"""Allowlist адресов клиента — чистая функция без aiohttp (unit)."""
from __future__ import annotations

import pytest

from server.app import is_allowed

pytestmark = pytest.mark.unit

ALLOWED = [
    "127.0.0.1",
    "127.0.0.2",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:192.168.3.50",
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.0.1",
    "192.168.3.50",
    "100.64.0.0",
    "100.87.166.82",
    "100.127.255.255",
]

DENIED = [
    "8.8.8.8",
    "1.1.1.1",
    "172.32.0.1",
    "172.15.255.255",
    "100.63.255.255",
    "100.128.0.0",
    "11.0.0.1",
    "192.169.0.1",
    "999.1.1.1",
    "",
    "не-адрес",
    None,
]


@pytest.mark.parametrize("ip", ALLOWED)
def test_allowed(ip):
    assert is_allowed(ip) is True


@pytest.mark.parametrize("ip", DENIED)
def test_denied(ip):
    assert is_allowed(ip) is False
