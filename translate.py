"""完成動画の字幕と、YouTube のタイトル・概要欄を、英語など別の言語に翻訳する。

使い方の例:
  python translate.py "D:\\撮影\\レビュー_roughcut"                   # 英語の字幕（○○_完成.en.srt）とタイトル・概要欄
  python translate.py "D:\\撮影\\レビュー_roughcut" --lang en,ko       # 英語と韓国語
  python translate.py "D:\\撮影\\レビュー_roughcut" --llm manual       # claude.ai に貼り付けて使う（API料金なし）
  python translate.py "D:\\撮影\\レビュー_roughcut" --only srt         # 字幕だけ

作る言語は、テンプレート（template.toml）の [translate] の languages でも決められます。
YouTube に設定するときは: python youtube.py "D:\\撮影\\レビュー_roughcut" --apply --localize
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
import rc_i18n as I

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"
PENDING = "translate_pending.json"   # claude.ai に貼り付けるプロンプトの一覧（画面が読む）


def _template_cfg(path: Path) -> dict:
    from rc_template import Template
    return Template(path).cfg if path and (Path(path) / "template.toml").exists() else {}


def run(out_dir: Path, template: Path = DEFAULT_TEMPLATE, codes: list[str] | None = None, llm: str = "api",
        effort: str = "medium", only: str = "all", force: bool = False, log=print) -> list[dict]:
    """翻訳する。claude.ai に貼り付けるプロンプトが残ったときは、その一覧を返す（全部できたら空）"""
    import rc_youtube as Y
    out_dir = Path(out_dir).resolve()
    if not (out_dir / "session.json").exists():
        sys.exit(f"[エラー] {out_dir / 'session.json'} がありません。先に荒編集を実行してください。")
    langs = I.languages(_template_cfg(template), codes)
    if not langs:
        sys.exit("[エラー] 翻訳する言語がありません（--lang en のように指定してください）。")
    meta = Y.default_meta(out_dir)   # 画面で直したタイトル・概要欄があれば、それを訳す
    do_srt, do_meta = only in ("all", "srt"), only in ("all", "meta")
    src = I.source_srt(out_dir) if do_srt else None
    log(f"■ 翻訳（{'・'.join(c['name_ja'] for c in langs)}）")
    claude = I.Claude(effort) if llm == "api" else None
    pending = []
    (out_dir / PENDING).unlink(missing_ok=True)
    for cfg in langs:
        if llm == "manual":
            rep = I.read_manual_reply(out_dir, cfg)
            replies = rep["lines"] if rep else None
            if do_meta and rep and rep.get("meta"):
                if rep["meta_source"] == I.meta_stamp(meta["title"], meta["description"]):
                    I.translate_meta(out_dir, cfg, meta["title"], meta["description"], None, log, reply=rep["meta"])
                else:
                    log(f"  [注意] {cfg['name_ja']}の返答は、前のタイトル・概要欄を訳したものなので使いません")
            redo = force and rep is None   # --force：前の訳を使わずに、全部をプロンプトに入れる
            need_meta = do_meta and (redo or not I.meta_is_fresh(out_dir, cfg["code"], meta["title"],
                                                                  meta["description"]))
            done = I.translate_srt(out_dir, cfg, None, log, force=redo, replies=replies) if do_srt else True
            if done is None or need_meta:
                groups = None
                if done is None:   # まだ訳していない字幕だけをプロンプトに入れる
                    groups = _untranslated(out_dir, cfg, src, redo)
                p = I.write_manual_prompt(out_dir, cfg, meta["title"], meta["description"], groups)
                _, reply = I.manual_paths(out_dir, cfg["code"])
                reply.unlink(missing_ok=True)
                pending.append({"code": cfg["code"], "name": cfg["name_ja"], "prompt": str(p), "reply": str(reply)})
            continue
        if do_meta:
            I.translate_meta(out_dir, cfg, meta["title"], meta["description"], claude, log, force)
        if do_srt:
            I.translate_srt(out_dir, cfg, claude, log, force)

    if pending:
        (out_dir / PENDING).write_text(json.dumps(pending, ensure_ascii=False, indent=1), encoding="utf-8")
        log("■ claude.ai に貼り付けて翻訳してください")
        for p in pending:
            log(f"  {p['name']}: {p['prompt']} の中身を claude.ai に貼り付けて、")
            log(f"        返答（JSON）を {Path(p['reply']).name} として同じフォルダに保存してください")
        log("  保存したら、もう一度 --llm manual を付けて実行すると仕上がります。")
        return pending
    log("■ 翻訳が終わりました")
    if do_srt:
        for cfg in langs:
            log(f"  字幕: {I.target_srt(src, cfg['code'])}")
    log(f"  タイトル・概要欄の訳: {out_dir / I.TRANSLATIONS}")
    log("  YouTube に設定するには、「YouTubeへ」画面で「英語字幕・英語タイトルも設定する」に"
        "チェックを入れてください（コマンドなら youtube.py に --localize）。")
    if claude and claude.usage["in"]:
        import rc_llm
        log(f"  Claude API: {rc_llm.cost_text(claude.usage)}")
    return []


def _untranslated(out_dir: Path, cfg: dict, src: Path, everything: bool = False) -> list[dict]:
    cache = {} if everything else I.load_json(out_dir / I.CACHE, {}).get(cfg["code"], {})
    return [g for g in I.group_captions(I.read_srt(src)) if I._sha(g["text"]) not in cache]


def main():
    ap = argparse.ArgumentParser(description="字幕とタイトル・概要欄の翻訳")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                    help="作る言語などの設定（[translate]）を読むテンプレート")
    ap.add_argument("--lang", help="作る言語（YouTubeの言語コードを「,」区切りで。例: en,ko,zh-Hans）")
    ap.add_argument("--llm", choices=["api", "manual"], default="api",
                    help="api=Claude APIで翻訳 / manual=claude.ai に貼り付けるプロンプトを作る（API料金なし）")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--only", choices=["all", "srt", "meta"], default="all",
                    help="srt=字幕だけ / meta=タイトル・概要欄だけ")
    ap.add_argument("--force", action="store_true", help="前に訳した内容を使わずに、全部訳し直す")
    args = ap.parse_args()
    codes = [c.strip() for c in args.lang.split(",")] if args.lang else None
    run(args.out_dir, args.template, codes, args.llm, args.effort, args.only, args.force)


if __name__ == "__main__":
    main()
