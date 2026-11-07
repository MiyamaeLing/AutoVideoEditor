"""完成動画の一部（区間）だけを書き出す。

使い方の例:
  python clip.py "D:\\撮影\\レビュー_roughcut" --start 1:23 --end 2:10       # 結果フォルダの完成動画から
  python clip.py "D:\\撮影\\レビュー_roughcut" --chapter 2                    # 2番目の章をまるごと
  python clip.py "D:\\撮影\\レビュー_roughcut" --list                         # 章の一覧
  python clip.py "D:\\撮影\\レビュー_roughcut\\レビュー_完成.mp4" --start 83 --end 130 --out 切り抜き.mp4

できるもの: 結果フォルダの clips\\○○_完成_1m23s-2m10s.mp4（と、字幕があれば同じ名前の .srt）
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
import rc_clip
from rc_media import probe, require_ffmpeg
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def main():
    ap = argparse.ArgumentParser(description="完成動画の一部（区間）だけを書き出す")
    ap.add_argument("target", type=Path, help="荒編集の結果フォルダ（○○_roughcut）か、完成動画（○○_完成.mp4）")
    ap.add_argument("--start", help="開始（例: 1:23 / 1:23.5 / 83）")
    ap.add_argument("--end", help="終了（例: 2:10）")
    ap.add_argument("--chapter", type=int, help="この番号の章（1から）をまるごと書き出す。--list で番号を確認できます")
    ap.add_argument("--list", action="store_true", help="章の一覧を表示するだけ")
    ap.add_argument("--out", type=Path, help="書き出すファイル（初期値: 結果フォルダの clips に「○○_完成_開始-終了.mp4」）")
    ap.add_argument("--no-srt", action="store_true", help="字幕ファイル（SRT）は作らない")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="画質とエンコーダ（[video]）を使うテンプレート")
    args = ap.parse_args()
    require_ffmpeg()
    try:
        video, out_dir = rc_clip.final_video(args.target)
    except ValueError as e:
        sys.exit(f"[エラー] {e}")
    total = probe(video).duration
    chs = rc_clip.chapters(out_dir, total)
    if args.list:
        print(f"■ {video.name}（{rc_clip.fmt_time(total)}）")
        for i, c in enumerate(chs, 1):
            print(f"  {i}. {rc_clip.fmt_time(c['start'])}〜{rc_clip.fmt_time(c['end'])}  {c['title']}")
        if not chs:
            print("  章はありません")
        return
    try:
        if args.chapter is not None:
            if not 1 <= args.chapter <= len(chs):
                raise ValueError(f"章の番号は 1〜{len(chs)} です（--list で確認できます）" if chs else "この動画には章がありません")
            start, end = chs[args.chapter - 1]["start"], chs[args.chapter - 1]["end"]
        elif args.start is None or args.end is None:
            raise ValueError("--start と --end（または --chapter）を指定してください")
        else:
            start, end = rc_clip.parse_time(args.start), rc_clip.parse_time(args.end)
        tpl = Template(args.template) if args.template else None
        print(f"■ 区間を書き出します: {rc_clip.fmt_time(start)}〜{rc_clip.fmt_time(min(end, total))}"
              f"（{video.name}）", flush=True)
        res = rc_clip.export(video, start, end, args.out, tpl, with_srt=not args.no_srt)
    except ValueError as e:
        sys.exit(f"[エラー] {e}")
    print("■ 書き出し完了")
    print(f"  動画: {res['video']}（{res['duration']:.1f}秒）")
    if res["srt"]:
        print(f"  字幕: {res['srt']}")


if __name__ == "__main__":
    main()
