"""企画・台本づくりと、撮り漏れチェック（コマンド）

使い方の例:
  python preprod.py new "マキタの新しいドリルをレビュー" --genre talk --minutes 10
  python preprod.py new "本棚を作る" --genre craft --name 本棚 --notes "SPF材、電動ドリル" --llm manual
  python preprod.py new --name 本棚 --answer 返答.txt          （claude.ai の返答を取り込む）
  python preprod.py list
  python preprod.py export 本棚                                 （plan.json を手で直したあと、印刷用を作り直す）
  python preprod.py check 素材_roughcut --project 本棚          （撮り漏れチェック。AIなし・すぐ終わる）
  python preprod.py check 素材_roughcut --llm api               （Claude で詳しく照合）

企画は projects/<名前>/ に plan.json・plan.md・plan.html（印刷用）として保存します。
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

import rc_win  # noqa: F401,E402  (Windowsでコンソール画面を出さない)
import rc_preprod as P  # noqa: E402


def _loose(path: Path) -> dict:
    s = path.read_text(encoding="utf-8-sig")
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        sys.exit(f"[エラー] {path} にJSONが見つかりません。Claudeの返答を最初から最後まで全部保存してください。")
    try:
        return json.loads(s[a:b + 1])
    except json.JSONDecodeError as e:
        sys.exit(f"[エラー] {path} のJSONが読み取れません（{e}）。返答を全部コピーしてください。")


def cmd_new(a):
    if not a.theme and not a.name:
        sys.exit("[エラー] テーマ（または --name）を指定してください。例: python preprod.py new \"本棚を作る\" --genre craft")
    name = P.safe_name(a.name or a.theme)
    d = P.projects_dir(a.projects_dir) / name
    old = P._read_json(d / "plan.json")
    req = dict((old or {}).get("request") or {})
    req.update({k: v for k, v in {"theme": a.theme, "genre": a.genre, "minutes": a.minutes,
                                  "notes": a.notes, "name": a.name or a.theme}.items() if v})
    req.setdefault("genre", "talk")
    req.setdefault("theme", name)
    req.setdefault("name", name)
    if old and not (a.overwrite or a.answer or old.get("source") == "waiting"):
        sys.exit(f"[エラー] 「{name}」という企画はもうあります。作り直すときは --overwrite を付けてください。")
    print(f"■ 企画「{name}」を作ります（{P.GENRES.get(req['genre'], P.GENRES['other'])[0]}）")
    usage = None
    if a.answer:
        plan, source = _loose(a.answer), "manual"
        print(f"  {a.answer.name} の内容を取り込みます")
    elif a.llm == "api":
        plan, usage = P.generate_plan_api(req, a.effort)
        source = "api"
    elif a.llm == "manual":
        d.mkdir(parents=True, exist_ok=True)
        prompt = d / "prompt_plan.md"
        P.write_manual_plan_prompt(req, prompt)
        P.save_project(d, P.make_project(req, P.skeleton_plan(req), "waiting"))
        print(f"  claude.ai に貼るプロンプトを作りました: {prompt}")
        print(f"  → 返答を保存して、python preprod.py new --name {name} --answer 返答.txt で取り込んでください。")
        print("  （それまでは、ジャンルの定番の流れのひな形を保存しておきます）")
        return
    else:
        plan, source = P.skeleton_plan(req), "none"
        print("  AIを使わずに、ジャンルの定番の流れでひな形を作ります（中身は企画タブか plan.json で直せます）")
    data = P.make_project(req, plan, source)
    if old and old.get("created"):
        data["created"] = old["created"]
    P.save_project(d, data)
    n_pts = sum(len(s["points"]) for s in data["sections"])
    print(f"  構成 {len(data['sections'])}つ・話す要点 {n_pts}個・撮影リスト {len(data['shots'])}カット")
    print("■ 完了")
    print(f"  企画のフォルダ: {d}")
    print(f"  印刷用: {d / 'plan.html'}（ブラウザで開いて印刷）")
    print(f"  メモ用: {d / 'plan.md'}")
    if usage and usage["in"]:
        import rc_llm
        print(f"  Claude API: 入力{usage['in']:,} / 出力{usage['out']:,}トークン（約${rc_llm.cost_usd(usage):.2f}）")


def cmd_list(a):
    items = P.list_projects(a.projects_dir)
    if not items:
        print(f"企画はまだありません（保存場所: {P.projects_dir(a.projects_dir)}）")
        return
    for d, data in items:
        g = P.GENRES.get(data.get("genre"), P.GENRES["other"])[0]
        print(f"{d.name}\t{g}\t{data.get('title', '')}\t更新 {data.get('updated', '')}")


def cmd_export(a):
    d, data = P.load_project(a.project, a.projects_dir)
    P.save_project(d, data)
    print(f"印刷用を作り直しました: {d / 'plan.html'}")
    if a.show:
        print((d / "plan.md").read_text(encoding="utf-8"))


def cmd_check(a):
    out = a.roughcut.resolve()
    if a.project:
        pdir, data = P.load_project(a.project, a.projects_dir)
    else:
        pdir = P.linked_project(out)
        if not pdir:
            sys.exit("[エラー] どの企画（台本）と照らし合わせるかを --project で指定してください。"
                     "（python preprod.py list で一覧が出ます）")
        pdir, data = P.load_project(pdir)
    P.set_link(out, pdir)
    chunks, cut_ids, sess = P.load_roughcut(out)
    print(f"■ 撮り漏れチェック（企画「{pdir.name}」× {out.name}）")
    usage = None
    if a.answer:
        raw = P.merge_ai(data, [_loose(a.answer)], {c["id"] for c in chunks})
        method = "ai"
    elif a.llm == "api":
        raw, usage = P.ai_check(data, chunks, cut_ids, a.effort)
        method = "ai"
    elif a.llm == "manual":
        prompt = out / "prompt_preprod_check.md"
        P.write_manual_check_prompt(data, chunks, cut_ids, prompt)
        print(f"  claude.ai に貼るプロンプトを作りました: {prompt}")
        print("  → 返答を保存して、--answer 返答.txt を付けてもう一度実行してください。")
        print("  （今回は言葉の一致で照合した結果を保存します）")
        raw, method = P.local_check(data, chunks, cut_ids, bool(sess.get("args", {}).get("cam2"))), "local"
    else:
        raw, method = P.local_check(data, chunks, cut_ids, bool(sess.get("args", {}).get("cam2"))), "local"
    res = P.finalize_check(data, raw, chunks, cut_ids, method, pdir)
    P.save_check(out, res)
    c = res["counts"]
    print(f"  話していない要点: {c['missing']}/{c['points']}（一部だけ: {c['partial']}）")
    print(f"  撮った記録がないカット: {c['shots_missing']}/{c['shots']}")
    for p in res["points"]:
        if p["status"] in ("missing", "cut"):
            print(f"  ・[{P.POINT_STATUS[p['status']]}] {p['section_name']}：{p['text']}")
    print("■ 完了")
    print(f"  結果: {out / 'preprod_check.md'}")
    if usage and usage["in"]:
        import rc_llm
        print(f"  Claude API: 入力{usage['in']:,} / 出力{usage['out']:,}トークン（約${rc_llm.cost_usd(usage):.2f}）")


def build_parser():
    ap = argparse.ArgumentParser(description="企画・台本づくりと撮り漏れチェック")
    ap.add_argument("--projects-dir", type=Path, help="企画を保存するフォルダ（初期値: ツールの projects フォルダ）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="新しい企画（構成案・台本・撮影リスト）を作る")
    n.add_argument("theme", nargs="?", default="", help="動画のテーマ（例: 本棚を作る）")
    n.add_argument("--name", help="企画の名前（フォルダ名。初期値: テーマ）")
    n.add_argument("--genre", choices=list(P.GENRES), help="talk=レビュー・解説 / craft=工作 / outdoor=屋外企画 / mv=MV / other")
    n.add_argument("--minutes", type=float, help="完成動画の長さの目安（分）")
    n.add_argument("--notes", default="", help="伝えたいこと・使う道具・撮影場所などのメモ")
    n.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                   help="api=Claude APIで作る / manual=claude.aiに貼るプロンプトを作る / none=ひな形だけ")
    n.add_argument("--answer", type=Path, help="claude.ai から返ってきたJSONを保存したファイル")
    n.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    n.add_argument("--overwrite", action="store_true", help="同じ名前の企画を作り直す")
    n.set_defaults(func=cmd_new)
    li = sub.add_parser("list", help="企画の一覧")
    li.set_defaults(func=cmd_list)
    e = sub.add_parser("export", help="plan.json から印刷用（plan.md / plan.html）を作り直す")
    e.add_argument("project", help="企画の名前またはフォルダ")
    e.add_argument("--show", action="store_true", help="内容を表示する")
    e.set_defaults(func=cmd_export)
    c = sub.add_parser("check", help="荒編集の文字起こしと台本を照らし合わせる（撮り漏れチェック）")
    c.add_argument("roughcut", type=Path, help="荒編集の結果のフォルダ（○○_roughcut）")
    c.add_argument("--project", help="企画の名前またはフォルダ（前回と同じなら省略可）")
    c.add_argument("--llm", choices=["api", "manual", "none"], default="none",
                   help="none=言葉の一致（初期値・無料） / api=Claudeで照合 / manual=claude.aiに貼るプロンプトを作る")
    c.add_argument("--answer", type=Path, help="claude.ai から返ってきたJSONを保存したファイル")
    c.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    c.set_defaults(func=cmd_check)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
