"""Запуск модуля как `python3 -m modules.redzone`."""
from __future__ import annotations

import sys

from .redzone import main

if __name__ == "__main__":
    sys.exit(main())
