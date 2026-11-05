"""YouTube への設定・アップロード（コマンド版）。

  python youtube.py "D:\\撮影\\レビュー_roughcut" --apply             # Studioでアップした動画を探して設定
  python youtube.py "D:\\撮影\\レビュー_roughcut" --apply --video-id XXXXXXXXXXX
  python youtube.py "D:\\撮影\\レビュー_roughcut" --upload            # 動画ファイルごとアップロード（審査済み向け）
  python youtube.py --login                                         # Googleアカウントと連携だけする
  python youtube.py "D:\\撮影\\レビュー_roughcut" --show              # 設定される内容を表示するだけ
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401
import rc_youtube as yt_mod


def main():
    ap = argparse.ArgumentParser(description="YouTube への設定・アップロード")
    ap.add_argument("out_dir", nargs="?", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="アップロード済みの動画にタイトル等を設定する")
    g.add_argument("--upload", action="store_true", help="動画ファイルごとAPIでアップロードする")
    g.add_argument("--login", action="store_true", help="Googleアカウントと連携する")
    g.add_argument("--list", action="store_true", help="最近アップロードした動画を表示する")
    g.add_argument("--show", action="store_true", help="設定される内容を表示する")
    ap.add_argument("--video-id", help="設定する動画のID（省略すると、ファイル名と同じタイトルの動画を探す）")
    args = ap.parse_args()

    if args.login or args.list:
        yt = yt_mod.get_service()
        ch = yt_mod.channel_info(yt)
        print(f"  連携済み: {ch['title']}")
        if args.list:
            for u in yt_mod.recent_uploads(yt):
                print(f"  {u['id']}  [{u['privacy']}/{u['upload_status']}]  {u['title']}")
        return
    if not args.out_dir or not (args.out_dir / "session.json").exists():
        ap.error("roughcut.py の出力フォルダ（session.json があるフォルダ）を指定してください")

    meta = yt_mod.default_meta(args.out_dir)
    if args.show or not (args.apply or args.upload):
        print(json.dumps({k: v for k, v in meta.items() if k != "title_candidates"}, ensure_ascii=False, indent=1))
        errs = yt_mod.check_meta(meta)
        print("  問題なし" if not errs else "  [注意] " + " / ".join(errs))
        return

    print("■ YouTube")
    yt = yt_mod.get_service()
    ch = yt_mod.channel_info(yt)
    print(f"  チャンネル: {ch['title']}")
    if args.upload:
        vid = yt_mod.upload_video(yt, meta)
        meta["video_id"] = vid
        yt_mod.save_meta(args.out_dir, meta)
        yt_mod.apply_meta(yt, vid, meta)   # サムネイル・字幕も設定
        print("  ※ 審査（audit）前のプロジェクトでは、この動画は非公開にロックされます。")
        return

    vid = args.video_id or meta.get("video_id")
    if not vid:
        uploads = yt_mod.recent_uploads(yt)
        hit = yt_mod.guess_video(uploads, meta["video"])
        if not hit:
            print("[エラー] ファイル名と同じタイトルの動画が見つかりませんでした。最近の動画:")
            for u in uploads[:10]:
                print(f"  {u['id']}  {u['title']}")
            sys.exit("  --video-id で動画のIDを指定してください。")
        vid = hit["id"]
        print(f"  対象の動画: {hit['title']}（{vid}）")
    yt_mod.apply_meta(yt, vid, meta)
    meta["video_id"] = vid
    yt_mod.save_meta(args.out_dir, meta)


if __name__ == "__main__":
    main()
