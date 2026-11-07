"""サムネイル案を作る。

使い方の例:
  python thumbs.py "D:\\撮影\\レビュー_roughcut"                 # AIが場面と文字を選ぶ
  python thumbs.py "D:\\撮影\\レビュー_roughcut" --text "軽すぎ！"  # 文字を指定して作り直す
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
from rc_media import require_ffmpeg
from rc_template import Template
from rc_thumb import generate

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def main():
    ap = argparse.ArgumentParser(description="サムネイル案の作成")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                    help="api=Claudeが画像を見て選ぶ / manual・none=くっきりした見どころの場面を自動で選ぶ")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default=None,
                    help="AIの考える深さ（省略するとテンプレートの [thumbnail] の effort。初期値 high）")
    ap.add_argument("--text", help="サムネイルの文字を指定する（2行にするときは \\n で区切る）")
    args = ap.parse_args()
    require_ffmpeg()
    if not (args.out_dir / "session.json").exists():
        sys.exit(f"[エラー] {args.out_dir / 'session.json'} がありません。先に荒編集を実行してください。")
    generate(args.out_dir, Template(args.template), args.llm, args.effort, args.text)


if __name__ == "__main__":
    main()
