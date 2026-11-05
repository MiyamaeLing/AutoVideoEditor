"""Windowsで、GUIから実行したときに FFmpeg などの黒いコンソール画面が開かないようにする。"""
import os
import subprocess

if os.name == "nt":
    _orig_init = subprocess.Popen.__init__

    def _no_window_init(self, *args, **kwargs):
        kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _no_window_init
