"""サムネイル案を作る。

使い方の例:
  python thumbs.py "D:\\撮影\\レビュー_roughcut"                 # AIが場面と文字を選ぶ
  python thumbs.py "D:\\撮影\\レビュー_roughcut" --text "軽すぎ！"  # 文字を指定して作り直す
  python thumbs.py "D:\\撮影\\レビュー_roughcut" --request "案2は文字を左に。もっと明るく"  # 今の案を見てAIが作り直す
  python thumbs.py "D:\\撮影\\レビュー_roughcut" --scenes "1:23, 4:56"   # 使う場面を完成動画の時刻で決める
  python thumbs.py "D:\\撮影\\レビュー_roughcut" --fresh                 # 今の案とは別の場面から選び直す
  python thumbs.py "D:\\撮影\\レビュー_roughcut" --more-scenes           # 場面を選ぶ画面の候補を増やす
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
from rc_thumb import generate, more_scenes, parse_times

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
    ap.add_argument("--request", default="",
                    help="今のサムネイル案を見て、AIに指示して作り直す（例: 「案2は文字を左に」「もっと明るく」。--llm api のとき）")
    ap.add_argument("--scenes", default="",
                    help="使う場面を完成動画の時刻で（例: \"1:23, 4:56\"。3つまで、この順で案1から）。"
                         "足りない分はAI（または自動）がほかの場面から選ぶ")
    ap.add_argument("--fresh", action="store_true", help="今の案に使っている場面を外し、別の場面から選び直す")
    ap.add_argument("--more-scenes", action="store_true",
                    help="場面を選ぶ画面の候補を増やす（今の候補とは別の場面を足す。サムネイル案は作り直さない）")
    args = ap.parse_args()
    try:
        scenes = parse_times(args.scenes)
    except ValueError as e:
        sys.exit(f"[エラー] --scenes の{e}")
    require_ffmpeg()
    if not (args.out_dir / "session.json").exists():
        sys.exit(f"[エラー] {args.out_dir / 'session.json'} がありません。先に荒編集を実行してください。")
    if args.more_scenes:
        more_scenes(args.out_dir, Template(args.template))
        return
    generate(args.out_dir, Template(args.template), args.llm, args.effort, args.text, args.request,
             scenes[:3] or None, args.fresh)


if __name__ == "__main__":
    main()
