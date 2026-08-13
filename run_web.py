#!/usr/bin/env python3
"""Запуск Preflight.

    python run_web.py

Откроется http://127.0.0.1:8000 — обычный сайт в браузере, только работает
локально и читает папки сделок напрямую. Загружать файлы никуда не нужно.

Корень с папками сделок по умолчанию: ~/Desktop/Medali Store
Другой путь:  PREFLIGHT_ROOT="/путь" python run_web.py
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from threading import Timer

sys.path.insert(0, str(Path(__file__).resolve().parent))

from preflight import store, updater      # noqa: E402
from preflight.version import __version__  # noqa: E402

HOST = os.environ.get("PREFLIGHT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PREFLIGHT_PORT", "8000"))


def main() -> None:
    updater.run_forced_update()     # молча и обязательно, до старта сервера
    store.init()
    root = store.root_dir()
    print(f"  Preflight {__version__}")
    print(f"  папки сделок : {root}" + ("" if root.exists() else "   ← не найдено!"))
    print(f"  база         : {store.db_path()}")
    print(f"  адрес        : http://{HOST}:{PORT}\n")

    if HOST in {"127.0.0.1", "localhost"}:
        Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    import uvicorn
    uvicorn.run("web.app:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
