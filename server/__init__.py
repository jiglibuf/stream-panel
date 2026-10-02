"""Пакет ядра «Пульт стрима»: клиент obs-websocket v5."""
from __future__ import annotations

from .obs import Obs, compute_auth, load_connection

__all__ = ["Obs", "compute_auth", "load_connection"]
