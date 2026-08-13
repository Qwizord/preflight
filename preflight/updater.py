"""Принудительное обновление с GitHub Releases.

Проверка идёт при запуске, до того как поднимется сервер. Если на GitHub лежит
версия свежее — она скачивается и ставится молча, без кнопки «потом».
Отменить нельзя: у всех должна быть одна версия, иначе правила разъезжаются.

Приложение раздаётся папкой (сборка PyInstaller в режиме onedir). Заменить
запущенный исполняемый файл нельзя ни в Windows, ни в macOS, поэтому обновление
идёт в три шага: скачали рядом → написали скрипт подмены → вышли, скрипт
подменил папку и запустил заново.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .version import RELEASES_API, __version__, is_newer

TIMEOUT = 15
ASSET_FOR = {"win32": "preflight-windows.zip", "darwin": "preflight-macos.zip"}


def is_frozen() -> bool:
    """True, если запущена собранная версия, а не исходники."""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Папка, которую надо заменить при обновлении."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def check() -> dict | None:
    """Возвращает сведения о новой версии или None."""
    try:
        req = urllib.request.Request(
            RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                   "User-Agent": "preflight-updater"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except Exception:
        return None                      # нет сети — работаем на текущей версии

    tag = str(data.get("tag_name") or "")
    if not tag or not is_newer(tag, __version__):
        return None

    want = ASSET_FOR.get(sys.platform)
    if not want:
        return None
    for a in data.get("assets") or []:
        if a.get("name") == want:
            return {"version": tag.lstrip("vV"),
                    "url": a["browser_download_url"],
                    "notes": (data.get("body") or "").strip(),
                    "size": a.get("size") or 0}
    return None


def download(url: str, on_progress=None) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="preflight-upd-")) / "package.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "preflight-updater"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(done / total)
    return dest


def apply_and_restart(zip_path: Path) -> None:
    """Распаковывает рядом и передаёт управление скрипту подмены."""
    staging = zip_path.parent / "new"
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(staging)

    # если внутри архива одна папка — работаем с ней
    entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
    src = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging

    target = app_dir()
    relaunch = sys.executable if is_frozen() else f'"{sys.executable}" "{Path(sys.argv[0]).resolve()}"'

    if sys.platform == "win32":
        script = zip_path.parent / "apply.bat"
        script.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'robocopy "{src}" "{target}" /MIR /NFL /NDL /NJH /NJS /NC /NS >nul\r\n'
            f'start "" {relaunch}\r\n'
            f'rmdir /s /q "{zip_path.parent}"\r\n',
            encoding="cp866")
        subprocess.Popen(["cmd", "/c", str(script)],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        script = zip_path.parent / "apply.sh"
        script.write_text(
            "#!/bin/sh\n"
            "sleep 2\n"
            f'rsync -a --delete "{src}/" "{target}/" 2>/dev/null || cp -Rf "{src}/." "{target}/"\n'
            f"{relaunch} &\n"
            f'rm -rf "{zip_path.parent}"\n')
        script.chmod(0o755)
        subprocess.Popen(["/bin/sh", str(script)], start_new_session=True)

    sys.exit(0)


def run_forced_update(log=print) -> None:
    """Полный цикл. Ошибки не должны мешать работе — просто пропускаем."""
    if os.environ.get("PREFLIGHT_NO_UPDATE"):
        return
    if not is_frozen():
        return                            # из исходников обновляемся через git

    info = check()
    if not info:
        return

    log(f"  Обновление {__version__} → {info['version']}. Это обязательно.")
    try:
        pkg = download(info["url"],
                       on_progress=lambda p: log(f"  загрузка {p * 100:.0f}%"))
        log("  устанавливаю и перезапускаюсь…")
        apply_and_restart(pkg)
    except SystemExit:
        raise
    except Exception as e:
        log(f"  не получилось обновиться ({e}) — запускаю текущую версию")
