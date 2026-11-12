"""画面（gui.py と gui_ext_*.py・gui_*.py）で共通に使う小さな処理。

gui.py を python gui.py で起動するとモジュール名が __main__ になるので、ほかのファイルから gui を import できない。
共通の処理はここに置き、gui.py そのものが要るときは gui_module(win) で取る。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def fmt(t: float) -> str:
    t = max(0, t)
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def open_path(p: Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def gui_module(win):
    """gui.py のモジュール（python gui.py で起動したときは __main__）"""
    return sys.modules[type(win).__module__]
