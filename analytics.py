"""公開後の分析（コマンド版）。

  python analytics.py --login                                       # 分析の許可も含めて Google と連携し直す
  python analytics.py "D:\\撮影\\レビュー_roughcut" --retention        # 視聴維持率を分析してレポートを作る
  python analytics.py "D:\\撮影\\レビュー_roughcut" --comments         # コメントの要約と返信案
  python analytics.py "D:\\撮影\\レビュー_roughcut" --promo            # X・Instagram の告知文、ショートの説明文
  python analytics.py "D:\\撮影\\レビュー_roughcut" --all              # 上の3つをまとめて
  python analytics.py --retention --video-id XXXXXXXXXXX           # このツールで作っていない動画も分析できる
  python analytics.py --trends                                      # 分析した動画から、チャンネルの傾向をまとめ直す
  python analytics.py "D:\\撮影\\レビュー_roughcut" --reply コメントID --text "返信の文"
  python analytics.py --status                                      # 連携の状態と、チャンネルの傾向を表示
  python analytics.py --purge [フォルダ]                             # 保存した分析データを消す

--llm manual にすると claude.ai に貼るプロンプトを作ります（返答は --answer で渡す）。
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
import rc_analytics as A


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="公開後の分析（視聴維持率・コメント・告知文）")
    ap.add_argument("out_dir", nargs="?", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--retention", action="store_true", help="視聴維持率を分析する")
    g.add_argument("--comments", action="store_true", help="コメントの要約と返信案を作る")
    g.add_argument("--promo", action="store_true", help="X・Instagram の告知文と、ショートの説明文を作る")
    g.add_argument("--all", action="store_true", help="視聴維持率・コメント・告知文をまとめて")
    g.add_argument("--trends", action="store_true", help="チャンネルの傾向（channel_insights.json）をまとめ直す")
    g.add_argument("--reply", metavar="COMMENT_ID", help="このコメントに返信を投稿する（--text と一緒に）")
    g.add_argument("--login", action="store_true", help="分析の許可も含めて Google アカウントと連携し直す")
    g.add_argument("--status", action="store_true", help="連携の状態とチャンネルの傾向を表示する")
    g.add_argument("--purge", action="store_true", help="保存した分析データを消す")
    g.add_argument("--use-in-prompts", choices=["on", "off"],
                   help="チャンネルの傾向を、次のカット判定・演出プランのAIに伝えるか")
    ap.add_argument("--video-id", help="YouTube の動画ID（省略すると「YouTubeへ」で設定した動画を使う）")
    ap.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                    help="api=Claude APIで分析 / manual=claude.aiに貼るプロンプトを作る / none=AIを使わない")
    ap.add_argument("--answer", type=Path, help="manual のとき、claude.ai から返ってきたJSONを保存したファイル")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--text", help="--reply で投稿する文")
    ap.add_argument("--max-comments", type=int, default=300, help="読むコメントの最大数（初期値: 300）")
    ap.add_argument("--no-frames", action="store_true", help="離脱場面の静止画を取り出さない")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out = args.out_dir.resolve() if args.out_dir else None
    if out and not out.is_dir():
        sys.exit(f"[エラー] フォルダが見つかりません: {out}")

    if args.login:
        A.relink()
        return
    A.prune_all()   # 取得してから30日たったコメントのデータを消す（YouTube の規約）
    if args.status:
        st = {"none": "未連携", "old": "連携済み（分析の許可がないので、--login で連携し直してください）",
              "ok": "連携済み（分析も使えます）"}[A.link_state()]
        print(f"  Google: {st}")
        d = A.load_insights()
        print(f"  分析した動画: {len(d['videos'])}本 / 次のAIに伝える: {'する' if d['use_in_prompts'] else 'しない'}")
        for k, label in (("cut", "カット判定"), ("plan", "演出プラン")):
            for m in ("talk", "craft", "outdoor"):
                note = A.prompt_notes(k, m)
                if note:
                    print(f"  ▼ {label}（{A.MODES_JA[m]}）に足す指示:{note.split('参考にしてください。')[-1]}")
        return
    if args.use_in_prompts:
        d = A.load_insights()
        d["use_in_prompts"] = args.use_in_prompts == "on"
        A.save_insights(d)
        print(f"  チャンネルの傾向を次のAIに{'伝えます' if d['use_in_prompts'] else '伝えません'}")
        return
    if args.purge:
        removed = A.purge(out)
        print("  消しました: " + ("、".join(str(p) for p in removed) if removed else "（消すデータはありませんでした）"))
        return
    if args.trends:
        A.build_trends(args.llm, args.effort, args.answer)
        return

    if out and not (out / "session.json").exists():
        sys.exit("[エラー] roughcut.py の出力フォルダ（session.json があるフォルダ）を指定してください。")
    if not out and not args.video_id:
        sys.exit("[エラー] roughcut.py の出力フォルダか、--video-id を指定してください。")

    vid = (args.video_id or "").strip() or A.guess_video_id(out)
    services = None
    needs_google = args.retention or args.comments or args.all or args.reply
    reuse = args.answer is not None and not args.all   # claude.ai の返答を読むだけなら、取り直さない
    if needs_google and not reuse:
        services = A.get_services()
        if not vid:
            vid = A.find_video_id(services[0], out)
        print(f"  対象の動画: https://youtu.be/{vid}")

    if args.reply:
        if not args.text:
            sys.exit("[エラー] --text で返信の文を指定してください。")
        A.post_reply(services[0], args.reply, args.text, A.work_dir(out, vid))
        print("■ 返信を投稿しました")
        return

    if args.retention or args.all:
        A.analyze_retention(out, vid, args.llm, args.effort, args.answer if args.retention else None,
                            not args.no_frames, services)
    if args.comments or args.all:
        A.analyze_comments(out, vid, args.llm, args.effort, args.answer if args.comments else None, services,
                           args.max_comments)
    if args.promo or args.all:
        if not out:
            sys.exit("[エラー] 告知文を作るには、roughcut.py の出力フォルダを指定してください。")
        A.make_promo(out, vid, args.llm, args.effort, args.answer if args.promo else None)
    if not (args.retention or args.comments or args.promo or args.all):
        sys.exit("[エラー] --retention / --comments / --promo / --all のどれかを指定してください。")


if __name__ == "__main__":
    main()
