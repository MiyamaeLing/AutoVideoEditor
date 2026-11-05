"""見た目テンプレートの見本画像と、AIによるデザイン案（画面の「テンプレートを編集」からも使われます）。

使い方の例:
  python design.py preview --template templates/default                       # 見本画像（一覧）を作る
  python design.py preview --template templates/default --bg "D:\\撮影\\C0001.MP4" --at 30   # 自分の素材の30秒目で
  python design.py propose --template templates/default --mood "落ち着いた・ガジェット系"   # AIに3案作ってもらう
  python design.py propose --template templates/default --mood "ポップ" --llm manual       # claude.ai 用のプロンプトを作る
  python design.py propose --template templates/default --mood "ポップ" --answer claude_design.json
  python design.py save --config 案.json --template templates/default --name 新しい見た目   # 設定を別名で保存
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
import rc_design
from rc_media import require_ffmpeg

DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "templates" / "default"
VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".mkv", ".avi", ".webm"}


def background(bg: Path | None, at: float, work: Path) -> Path | None:
    """背景の指定（画像・動画）を画像にする。動画なら at 秒目の1コマ"""
    if not bg:
        return None
    bg = Path(bg)
    if not bg.exists():
        sys.exit(f"[エラー] 背景のファイルが見つかりません: {bg}")
    if bg.suffix.lower() in VIDEO_EXT:
        try:
            return rc_design.extract_frame(bg, at, work / "bg_frame.png")
        except RuntimeError as e:
            sys.exit(f"[エラー] {e}")
    return bg


def load_fonts(path: Path | None, tdir: Path) -> list[str]:
    """AI に選んでもらうフォントの一覧（画面から渡されたもの、なければこのPCを調べる）"""
    if path and Path(path).exists():
        names = [s.strip() for s in Path(path).read_text(encoding="utf-8-sig").splitlines() if s.strip()]
        if names:
            return names[:150]
    tpl = rc_design.Template(tdir)
    fams = rc_design.font_families(rc_design.scan_fonts([tpl.fonts_dir] if tpl.fonts_dir else []))
    return [f["name"] for f in fams][:150]


def cmd_preview(a):
    require_ffmpeg()
    tdir = Path(a.template).resolve()
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8")) if a.config else rc_design.load_cfg(tdir)
    work = tdir / rc_design.WORK_NAME
    work.mkdir(exist_ok=True)
    bg = background(a.bg, a.at, work)
    tpl = rc_design.Template(tdir)
    faces = rc_design.scan_fonts([tpl.fonts_dir] if tpl.fonts_dir else [])
    print("■ 見本画像を作成中", flush=True)
    try:
        r = rc_design.render_preview(cfg, tdir, bg, bg_kind=a.sample, faces=faces)
    except RuntimeError as e:
        sys.exit(f"[エラー] {e}")
    out = Path(a.out) if a.out else tdir / "design_preview.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r["grid"].read_bytes())
    print(f"■ 見本画像を作りました: {out}")


def cmd_propose(a):
    require_ffmpeg()
    tdir = Path(a.template).resolve()
    base = json.loads(Path(a.base_config).read_text(encoding="utf-8")) if a.base_config else rc_design.load_cfg(tdir)
    out = Path(a.out).resolve() if a.out else tdir / rc_design.WORK_NAME / "proposals"
    out.mkdir(parents=True, exist_ok=True)
    (out / "proposals.json").unlink(missing_ok=True)
    fonts = load_fonts(a.fonts, tdir)
    tpl = rc_design.Template(tdir)
    faces = rc_design.scan_fonts([tpl.fonts_dir] if tpl.fonts_dir else [])
    print(f"■ デザイン案を作成（雰囲気: {a.mood or 'おまかせ'} / 使えるフォント {len(fonts)}種類）", flush=True)

    usage = {"in": 0, "out": 0}
    if a.answer:
        try:
            props = rc_design.parse_answer(Path(a.answer).read_text(encoding="utf-8-sig"))
        except (ValueError, json.JSONDecodeError) as e:
            sys.exit(f"[エラー] Claude の返答を読み取れませんでした: {e}\n  返答を最初から最後まで全部コピーしてください。")
    elif a.llm == "api":
        try:
            props, usage = rc_design.propose_api(a.mood, base, fonts, a.effort, a.genre)
        except RuntimeError as e:
            sys.exit(f"[エラー] {e}")
    elif a.llm == "manual":
        p = out / "prompt_design.md"
        rc_design.write_manual_prompt(a.mood, base, fonts, p, a.genre)
        print(f"  claude.ai に貼るプロンプトを作りました: {p}")
        print("  → 返答を保存して、--answer でそのファイルを指定してもう一度実行してください。")
        return
    else:
        print("  （AIを使わない設定なので、用意してある案から選びます）")
        props = rc_design.preset_proposals(a.mood)
    if not props:
        sys.exit("[エラー] デザイン案が1つも返ってきませんでした。もう一度試してください。")

    bg = background(a.bg, a.at, tdir / rc_design.WORK_NAME)
    results = []
    for i, p in enumerate(props, 1):
        name = str(p.get("name") or f"案{i}")[:30]
        print(f"■ 見本画像を作成中 {i}/{len(props)}（{name}）", flush=True)
        cfg = rc_design.apply_proposal(base, p, fonts, faces, tdir)
        try:
            imgs = rc_design.render_preview(cfg, tdir, bg, out, prefix=f"p{i}", bg_kind=a.sample, faces=faces)
        except RuntimeError as e:
            sys.exit(f"[エラー] {e}")
        results.append({"name": name, "concept": str(p.get("concept", "")), "cfg": cfg,
                        "images": {k: v.name for k, v in imgs.items()}})
    (out / "proposals.json").write_text(json.dumps({"mood": a.mood, "template": str(tdir), "proposals": results},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"■ デザイン案を {len(results)} つ作りました: {out / 'proposals.json'}")
    for i, r in enumerate(results, 1):
        print(f"  案{i}: {r['name']} … {r['concept']}")
        print(f"        見本: {out / r['images']['grid']}")
    if usage["in"]:
        import rc_llm
        print(f"  Claude API: 約${rc_llm.cost_usd(usage):.2f}")


def cmd_save(a):
    tdir = Path(a.template).resolve()
    data = json.loads(Path(a.config).read_text(encoding="utf-8-sig"))
    cfg = data.get("cfg", data) if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        sys.exit("[エラー] 設定のJSONが読み取れません。")
    err = rc_design.check_name(a.name)
    if err:
        sys.exit(f"[エラー] {err}")
    dest = rc_design.TEMPLATES_DIR / a.name.strip()
    if dest.exists() and not a.overwrite:
        sys.exit(f"[エラー] 同じ名前のテンプレートがあります: {dest}\n  別の名前にするか、--overwrite を付けてください。")
    tpl = rc_design.Template(tdir)
    faces = rc_design.scan_fonts([tpl.fonts_dir] if tpl.fonts_dir else [])
    rc_design.save_template(cfg, tdir, dest, faces)
    print(f"■ テンプレートを保存しました: {dest}")


def main():
    ap = argparse.ArgumentParser(description="見た目テンプレートの見本とデザイン案")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="元にするテンプレートのフォルダ")
    common.add_argument("--bg", type=Path, help="背景にする画像または動画（なければサンプルの背景）")
    common.add_argument("--at", type=float, default=5.0, help="背景が動画のとき、何秒目のコマを使うか")
    common.add_argument("--sample", choices=["light", "dark"], default="light", help="サンプルの背景（明るい / 暗い）")

    p = sub.add_parser("preview", parents=[common], help="見本画像を作る")
    p.add_argument("--config", type=Path, help="テンプレートの代わりに使う設定（JSON）")
    p.add_argument("--out", type=Path, help="保存先（PNG）")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("propose", parents=[common], help="AIにデザイン案を3つ作ってもらう")
    p.add_argument("--mood", default="", help="チャンネルの雰囲気（例: 落ち着いた・ポップ・ガジェット系）")
    p.add_argument("--genre", default="", help="主な動画のジャンル（任意）")
    p.add_argument("--llm", choices=["api", "manual", "none"], default="api")
    p.add_argument("--answer", type=Path, help="claude.ai の返答（JSON）を保存したファイル")
    p.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--fonts", type=Path, help="使えるフォントの一覧（1行に1つ）")
    p.add_argument("--base-config", type=Path, help="元にする設定（JSON）。なければ --template の内容")
    p.add_argument("--out", type=Path, help="結果を置くフォルダ")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("save", help="設定（JSON）を新しいテンプレートとして保存する")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="BGMなどを複製する元のテンプレート")
    p.add_argument("--config", type=Path, required=True, help="設定のJSON（proposals.json の cfg など）")
    p.add_argument("--name", required=True, help="新しいテンプレートの名前（templates/<名前>/ に作ります）")
    p.add_argument("--overwrite", action="store_true", help="同じ名前があれば template.toml を上書きする")
    p.set_defaults(func=cmd_save)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
