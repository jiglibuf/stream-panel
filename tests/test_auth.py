"""Формула аутентификации obs-websocket v5 (unit, без сети)."""
from __future__ import annotations

import pytest

from server.obs import compute_auth
from tests.fakes import compute_auth as reference_auth

pytestmark = pytest.mark.unit


def test_matches_fake_reference():
    """Хэш совпадает с эталоном, которым пользуется фейковый сервер."""
    assert compute_auth("пароль", "соль", "челлендж") == reference_auth("пароль", "соль", "челлендж")


def test_known_answer_vector():
    """Жёсткий вектор: ловит одинаково неверную реализацию в клиенте и фейке."""
    assert compute_auth("secret", "salt123", "challenge456") == \
        "xgzgHJ5CaCNvrzkqxH6D2xMsV17ODXfIyB12Cj4aV1o="


def test_empty_inputs_are_deterministic():
    assert compute_auth("", "", "") == reference_auth("", "", "")
    assert compute_auth("", "", "") == "XEB0z23rR/W2r5xf4+C70OQrlZb+iKxU1ca275h+DyA="


def test_distinct_inputs_give_distinct_hashes():
    assert compute_auth("a", "s", "c") != compute_auth("b", "s", "c")
