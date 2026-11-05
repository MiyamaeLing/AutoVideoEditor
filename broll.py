"""Bロール（差し込み素材）を、発言に合わせて自動で配置する。

使い方の例:
  python broll.py "D:\\撮影\\レビュー_roughcut"                         # 本編と同じ場所の broll フォルダを使う
  python broll.py "D:\\撮影\\レビュー_roughcut" --folder "D:\\素材\\ドリル"  # 素材フォルダを指定
  python broll.py "D:\\撮影\\レビュー_roughcut" --llm none               # AIを使わず、ファイル名で合わせる
  python broll.py "D:\\撮影\\レビュー_roughcut" --xml-only               # 今のプランで Bロール入りの XML だけ作る

結果は演出プラン（plan_used.json と、画面で手直ししていれば plan_edited.json）の "broll" に入ります。
そのあと finish.py（画面では「完成動画を書き出す」）で、動画に反映されます。
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
import rc_broll
import rc_llm
import rc_plan
from rc_media import require_ffmpeg
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def current_plan(out_dir: Path) -> dict:
    for name in ("plan_edited.json", "plan_used.json"):
        p = out_dir / name
        if p.exists() and (name == "plan_used.json" or (out_dir / "plan_used.json").exists()):
            return json.loads(p.read_text(encoding="utf-8"))
    return {}


def main():
    ap = argparse.ArgumentParser(description="Bロール（差し込み素材）の自動配置")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--folder", type=Path, help="素材フォルダ（指定しないと、前に選んだフォルダ → 本編と同じ場所の broll）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                    help="api=Claudeが素材を見て決める / none・manual=ファイル名が発言に出てくる所に入れる")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--redescribe", action="store_true", help="素材の説明（前回AIが見た結果）を使わずに見直す")
    ap.add_argument("--xml-only", action="store_true", help="今の演出プランで、Bロール入りのXMLだけ作る")
    args = ap.parse_args()

    require_ffmpeg()
    out_dir = args.out_dir.resolve()
    sess_path = out_dir / "session.json"
    if not sess_path.exists():
        sys.exit(f"[エラー] {sess_path} がありません。先に roughcut.py を実行してください。")
    sess = json.loads(sess_path.read_text(encoding="utf-8"))
    tpl = Template(args.template)

    if args.xml_only:
        plan = current_plan(out_dir)
        if not plan.get("broll"):
            sys.exit("[エラー] 演出プランに Bロールがありません。先に Bロールを割り当ててください。")
        xml = rc_broll.write_xml(out_dir, sess, plan, tpl)
        if not xml:
            sys.exit("[エラー] Bロールの素材が見つからないか、出す場所がありません。")
        print(f"■ Premiere用XML（Bロール入り）: {xml}")
        return

    if args.folder:
        if not args.folder.is_dir():
            sys.exit(f"[エラー] 素材フォルダが見つかりません: {args.folder}")
        rc_broll.save_folder(out_dir, args.folder)
    if args.redescribe:
        (out_dir / rc_broll.CATALOG).unlink(missing_ok=True)

    tl, by_id, items = rc_broll.load_items(out_dir, sess)
    usage = {"in": 0, "out": 0}
    c = rc_broll.config(tpl)
    res = rc_broll.place(out_dir, sess, items, c, args.llm, args.effort, usage, args.folder)
    if res is None:
        sys.exit("[エラー] 素材フォルダがありません。本編の動画と同じ場所に「broll」フォルダを作って画像・動画を入れるか、"
                 "--folder で指定してください（画面では「素材フォルダを選ぶ」）。")

    if not (out_dir / "plan_used.json").exists():
        # 演出プランがまだ無いときは、字幕とBGMだけのプランに Bロールを足して作る
        moods = [m for m, files in tpl.bgm_files().items() if files]
        plan = rc_plan.default_plan(items, moods)
        plan["broll"] = res
        (out_dir / "plan_used.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    rc_broll.update_plan_files(out_dir, res)
    plan = current_plan(out_dir)
    for b in res:
        c0 = by_id.get(b["start_id"])
        print(f"  {Path(b['file']).name} → 「{c0.text if c0 else b['start_id']}」"
              f"（{rc_broll.MODE_JA[b['mode']]}）{b.get('reason', '')}")
    if res:
        try:
            xml = rc_broll.write_xml(out_dir, sess, plan, tpl)
            if xml:
                print(f"  Premiere用XML（Bロール入り）: {xml}")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            print(f"  [注意] Bロール入りのXMLを作れませんでした: {e}")
    print("■ Bロールの割り当て完了（「完成動画を書き出す」で動画に反映されます）")
    if usage["in"]:
        print(f"  Claude API: {rc_llm.cost_text(usage)}")


if __name__ == "__main__":
    main()
