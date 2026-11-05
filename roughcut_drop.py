"""roughcut_drop.bat から呼ばれる入口。ドロップされたファイルを名前の順に並べ直して roughcut.py に渡す。

  python roughcut_drop.py --mode talk C0002.MP4 C0001.MP4 C0003.MP4
  → python roughcut.py C0001.MP4 C0002.MP4 C0003.MP4 --mode talk と同じ
"""
from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401
from rc_drop import drop_inputs


def main():
    args = sys.argv[1:]
    opts, files = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--mode" and i + 1 < len(args):
            opts += [a, args[i + 1]]
            i += 2
            continue
        files.append(a)
        i += 1
    paths = drop_inputs(files, os.environ.get("RC_CMDLINE"))
    if not paths:
        sys.exit("[エラー] 動画ファイルが渡されていません。roughcut_drop.bat に動画ファイルをドラッグ＆ドロップしてください。")
    if [str(p) for p in paths] != files:
        print("  ファイル名の順に並べました: " + " → ".join(p.name for p in paths))
    import roughcut
    sys.argv = ["roughcut.py", *map(str, paths), *opts]
    roughcut.main()


if __name__ == "__main__":
    main()
