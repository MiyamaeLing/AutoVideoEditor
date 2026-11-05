"""困ったときの情報をまとめる（ログ・環境・設定。APIキーなどは伏せます）。

使い方の例:
  python support_info.py                         # 画面に表示して、zip にも保存
  python support_info.py --folder "D:\\撮影\\レビュー_roughcut"   # 作業中のフォルダのログも入れる

画面（GUI）では「ヘルプ → 困ったときの情報をコピー」で、同じ内容がクリップボードに入ります。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401
import rc_ops


def main():
    ap = argparse.ArgumentParser(description="困ったときの情報をまとめる")
    ap.add_argument("--folder", type=Path, help="作業中の ○○_roughcut フォルダ")
    ap.add_argument("--out", type=Path, help="zip を保存するフォルダ（初期値: %%APPDATA%%\\AutoVideoEditor\\困ったときの情報）")
    args = ap.parse_args()
    text, files = rc_ops.collect(folder=args.folder)
    z = rc_ops.save_zip(files, args.out)
    print(text)
    print(f"■ zip に保存しました: {z}")
    print("  相談するときは、この zip を送るか、上の文章をそのまま貼り付けてください（APIキーなどは伏せてあります）。")


if __name__ == "__main__":
    main()
