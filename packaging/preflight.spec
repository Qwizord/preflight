# -*- mode: python ; coding: utf-8 -*-
"""Сборка Preflight в папку (onedir) — так работает автообновление."""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("../web/templates", "web/templates")]
datas += collect_data_files("pymorphy3_dicts_ru")

hidden = collect_submodules("uvicorn") + collect_submodules("pymorphy3") + [
    "web.app", "preflight.checks", "preflight.cdr", "preflight.vector",
    "preflight.deal", "preflight.spec", "preflight.store", "preflight.textcheck",
    "preflight.updater", "preflight.version",
]

a = Analysis(["../run_web.py"], pathex=[".."], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "PySide6", "PyQt5"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="Preflight",
          console=True, icon=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Preflight")
