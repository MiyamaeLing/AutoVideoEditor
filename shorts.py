"""縦型ショート（1080x1920）を作る。

使い方の例:
  python shorts.py "D:\\撮影\\レビュー_roughcut"                 # Claudeが区間を選んで、最大3本作る
  python shorts.py "D:\\撮影\\レビュー_roughcut" --count 1       # 1本だけ
  python shorts.py "D:\\撮影\\レビュー_roughcut" --llm none      # AIなし（見どころ・強調テロップの周りから選ぶ）
  python shorts.py "D:\\撮影\\レビュー_roughcut" --llm manual    # claude.ai に貼るプロンプトを作る
  python shorts.py "D:\\撮影\\レビュー_roughcut" --picks claude_shorts.json   # claude.ai の返答で作る

できあがり: <出力フォルダ>/shorts/short_1.mp4 … と shorts.json（タイトル・説明文）
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

import rc_win  # noqa: F401  (Windowsでコンソール画面を出さない)
from rc_media import require_ffmpeg
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def main():
    ap = argparse.ArgumentParser(description="縦型ショートの自動生成")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="見た目のテンプレート（[shorts] の設定を使う）")
    ap.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                    help="api=Claudeが区間を選ぶ / manual=claude.ai用のプロンプトを作る / none=見どころの周りから自動で選ぶ")
    ap.add_argument("--picks", type=Path, help="claude.ai の返答（JSON）を保存したファイル")
    ap.add_argument("--count", type=int, choices=[1, 2, 3], help="作る本数の上限（初期値: テンプレートの count、ふつうは3）")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--keep-work", action="store_true", help="中間ファイルを消さずに残す")
    args = ap.parse_args()
    require_ffmpeg()
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.exit("[エラー] 顔検出のライブラリ（OpenCV）が入っていません: pip install opencv-python-headless")
    if not (args.out_dir / "session.json").exists():
        sys.exit(f"[エラー] {args.out_dir / 'session.json'} がありません。先に荒編集を実行してください。")
    import rc_shorts
    rc_shorts.generate(args.out_dir, Template(args.template), args.llm, args.effort, args.count, args.picks,
                       args.keep_work)


if __name__ == "__main__":
    main()
