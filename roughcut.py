"""AI荒編集ツール：撮影素材 → Premiere Pro 用のカット済みシーケンス（XML）＋字幕（SRT）

使い方の例:
  python roughcut.py 素材.mp4 --mode talk
  python roughcut.py C0001.MP4 C0002.MP4 --mode craft --cam2 スマホ.mp4
  python roughcut.py 素材.mp4 --mode outdoor --llm manual
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401  (Windowsでコンソール画面を出さない)
import rc_chapters
import rc_llm
from rc_export import build_captions, write_srt, write_xmeml
from rc_media import SourceSet, probe, require_ffmpeg
from rc_timeline import PRESETS, Timeline, compute_keep
from rc_transcribe import Chunk, fmt_ts, is_filler_only, make_chunks, save_chunks, transcribe

TYPE_JA = {"retake": "言い直し", "mistake": "言い間違い", "filler": "フィラー", "offscript": "段取り・独り言", "other": "その他"}


def load_json_loose(path: Path) -> dict:
    """claude.ai からコピーした文（```json などが付いていても可）からJSONを取り出す。"""
    s = path.read_text(encoding="utf-8-sig")
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        sys.exit(f"[エラー] {path} にJSONが見つかりません。")
    return json.loads(s[a:b + 1])


def parse_args():
    ap = argparse.ArgumentParser(description="AI荒編集ツール（Premiere Pro用）")
    ap.add_argument("inputs", nargs="+", type=Path, help="素材ファイル。分割ファイルは順番に並べると1本として扱います")
    ap.add_argument("--mode", choices=PRESETS.keys(), default="talk",
                    help="talk=レビュー・解説 / craft=工作 / outdoor=屋外企画（初期値: talk）")
    ap.add_argument("--cam2", nargs="+", type=Path, help="2台目カメラの素材（音声で自動同期してV2に配置）")
    ap.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                    help="api=Claude APIで自動判定 / manual=claude.aiに貼るプロンプトを作る / none=無音カットのみ")
    ap.add_argument("--decisions", type=Path, help="manualモードでClaudeから返ってきたJSONを保存したファイル")
    ap.add_argument("--meta", type=Path, help="manualモードでClaudeから返ってきたタイトル等のJSON")
    ap.add_argument("--glossary", type=Path, help="固有名詞リスト（1行1語）。文字起こしと字幕の精度が上がります")
    ap.add_argument("--whisper-model", default="large-v3-turbo", help="文字起こしモデル（初期値: large-v3-turbo）")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--batch-size", type=int, default=8, help="GPUの同時処理数。VRAM不足なら4に下げる")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium", help="Claudeの考える深さ")
    ap.add_argument("--caption-chars", type=int, default=24, help="字幕1枚の最大文字数")
    ap.add_argument("--no-ai-cuts", action="store_true", help="AIの判定はマーカーだけにして、カットは無音とフィラーのみ")
    ap.add_argument("--apply-ai-cuts", action="store_true", help="outdoorモードでもAIのカットを適用する")
    ap.add_argument("--out", type=Path, help="出力フォルダ（初期値: 素材と同じ場所に「<名前>_roughcut」）")
    ap.add_argument("--retranscribe", action="store_true", help="前回の文字起こし結果を使わずにやり直す")
    ap.add_argument("--finish", action="store_true", help="荒編集のあと、続けて仕上げ（演出・書き出し）まで行う")
    ap.add_argument("--template", type=Path, default=Path(__file__).parent / "templates" / "default",
                    help="仕上げに使う見た目のテンプレートフォルダ")
    ap.add_argument("--speakers", nargs="?", const="auto", metavar="人数",
                    help="（実験的）発言ごとに話者を推定して、字幕を人ごとに色分けする。人数（2 など）か auto")
    return ap.parse_args()


def main():
    args = parse_args()
    require_ffmpeg()
    for p in args.inputs + (args.cam2 or []):
        if not p.exists():
            sys.exit(f"[エラー] ファイルが見つかりません: {p}")

    preset = dict(PRESETS[args.mode])
    if args.no_ai_cuts:
        preset["apply_ai_cuts"] = False
    if args.apply_ai_cuts:
        preset["apply_ai_cuts"] = True

    stem = args.inputs[0].stem
    out = args.out or args.inputs[0].parent / f"{stem}_roughcut"
    out = out.resolve()   # 相対パスでも、仕上げ（作業フォルダで FFmpeg を動かす）で迷わないように
    out.mkdir(parents=True, exist_ok=True)
    glossary = []
    if args.glossary:
        glossary = [w.strip() for w in args.glossary.read_text(encoding="utf-8-sig").splitlines() if w.strip()]

    print(f"■ 素材を確認しています（モード: {args.mode}）")
    src = SourceSet([probe(p) for p in args.inputs])
    for f in src.files:
        print(f"  {f.path.name}: {fmt_ts(f.duration)}  {f.width}x{f.height}  {float(f.fps):.3f}fps")
        if f.vfr_suspect:
            print("    [注意] 可変フレームレートの可能性があります。ズレる場合はスマホの設定を固定fpsにしてください。")

    print("■ 文字起こし")
    tpath = out / "transcript.json"
    if args.retranscribe and tpath.exists():
        tpath.unlink()
    if args.retranscribe:
        from rc_transcribe import clear_partial
        clear_partial(tpath)   # 途中保存も使わずに、最初からやり直す
    segments = transcribe(src, tpath, args.whisper_model, args.device, glossary, args.batch_size)
    chunks = make_chunks(segments)
    save_chunks(chunks, out / "chunks.json")
    by_id = {c.id: c for c in chunks}
    print(f"  {len(chunks)}チャンク")
    if args.speakers:
        import rc_speakers   # 話者の区別（実験的な機能）。結果は speakers.json
        rc_speakers.detect(src, chunks, out, args.speakers)

    # ---- AIの判定 ----
    print("■ カットの判定")
    decisions = {"cuts": [], "review": [], "highlights": [], "corrections": []}
    usage_total = {"in": 0, "out": 0}
    if args.decisions:
        decisions.update(load_json_loose(args.decisions))
        print(f"  {args.decisions.name} の判定を使います")
    elif args.llm == "api":
        res, usage = rc_llm.analyze_cuts_api(chunks, args.mode, glossary, args.effort)
        decisions.update(res)
        for k in usage:
            usage_total[k] = usage_total.get(k, 0) + usage[k]
    elif args.llm == "manual":
        prompt_path = out / "prompt_cuts.md"
        rc_llm.write_manual_cut_prompt(chunks, args.mode, glossary, prompt_path)
        print(f"  claude.ai に貼るプロンプトを作りました: {prompt_path}")
        print("  → 返ってきたJSONを claude_cuts.json に保存して、--decisions claude_cuts.json を付けてもう一度実行してください。")
        print("  （今回は無音・フィラーだけでカットしたものを出力します）")
    (out / "decisions_used.json").write_text(json.dumps(decisions, ensure_ascii=False, indent=1), encoding="utf-8")

    ai_cut_ids, cut_info = set(), {}
    for cut in decisions.get("cuts", []):
        for i in cut["ids"]:
            if i in by_id:
                ai_cut_ids.add(i)
                cut_info[i] = cut
    filler_ids = {c.id for c in chunks if is_filler_only(c.text)}
    for i in filler_ids:
        cut_info.setdefault(i, {"type": "filler", "reason": "フィラーのみ（自動判定）"})
    cut_ids = set(filler_ids) if preset["cut_silence"] else set()
    if preset["apply_ai_cuts"]:
        cut_ids |= ai_cut_ids
    # 確認画面（GUI）で人が決めた「カットする／残す」は、AIや自動判定より優先する
    manual = decisions.get("manual") or {}
    manual_cut = {i for i in manual.get("cut", []) if i in by_id}
    manual_keep = {i for i in manual.get("keep", []) if i in by_id}
    for i in manual_cut:
        cut_info.setdefault(i, {"type": "other", "reason": "手動でカット"})
    cut_ids = (cut_ids | manual_cut) - manual_keep

    # ---- タイムライン ----
    keep, silent_work = compute_keep(chunks, cut_ids, src.duration, preset)
    tl = Timeline(src, keep, src.main.fps)
    print(f"  元の長さ {fmt_ts(src.duration)} → 編集後 {fmt_ts(tl.duration)}"
          f"（{max(0, 100 * (1 - tl.duration / max(src.duration, 1))):.0f}%短縮, {len(tl.pieces)}カット）")

    # ---- 2台目カメラ ----
    cam2 = None
    offset = 0.0
    if args.cam2:
        print("■ 2台目カメラを音声で同期しています")
        from rc_sync import find_offset
        cam2 = SourceSet([probe(p) for p in args.cam2])
        offset, conf = find_offset(src, cam2)
        print(f"  ズレ: {offset:+.2f}秒（信頼度 {conf:.1f}）")
        if conf < 8:
            print("  [注意] 同期の信頼度が低めです。Premiereで波形を見て確認してください。")
        (out / "sync.json").write_text(json.dumps({"offset_sec": offset, "confidence": conf}), encoding="utf-8")

    # ---- マーカー ----
    markers = []

    def add_marker(t, name, comment=""):
        m = tl.map_time_nearest(t)
        markers.append((m, name, comment))

    for h in decisions.get("highlights", []):
        if h["id"] in by_id:
            add_marker(by_id[h["id"]].start, f"★ {h['label']}", by_id[h["id"]].text)
    for r in decisions.get("review", []):
        if r["id"] in by_id:
            add_marker(by_id[r["id"]].start, "要確認", f"{r['note']}｜{by_id[r['id']].text}")
    if not preset["apply_ai_cuts"]:
        # 「残す」に決めた候補も、Premiere で見直せるように印は残す（屋外モードは元々すべて残すため）
        for i in sorted(ai_cut_ids - cut_ids):
            c, info = by_id[i], cut_info[i]
            add_marker(c.start, f"カット候補: {TYPE_JA.get(info['type'], info['type'])}", f"{info['reason']}｜{c.text}")
    for s, e in silent_work:
        add_marker(s, f"無言の作業 {e - s:.0f}秒（早送り候補）")
    markers.sort()

    # ---- 字幕 ----
    corr = {c["id"]: c["text"] for c in decisions.get("corrections", []) if c.get("id") in by_id}
    items, item_ids = [], []
    for c in chunks:
        if c.id in cut_ids:
            continue
        text = corr.get(c.id, c.text)
        for s, e in tl.map_range(c.start, c.end):
            items.append((s, e, text))
            item_ids.append(c.id)
    caps = build_captions(items, args.caption_chars)
    if (out / "speakers.json").exists():
        import rc_speakers   # 話者名を字幕に入れる設定のときだけ、名前を付ける
        caps = rc_speakers.label_srt(out, items, item_ids, args.caption_chars, caps, _template_or_none(args.template),
                                     chunks)
    write_srt(out / f"{stem}_字幕.srt", caps)

    # ---- XML ----
    xml_path = out / f"{stem}_荒編集.xml"
    cam2_shots = None   # 演出プランで手元カメラを選んだ区間があれば、その所だけ V2 を有効にする
    if cam2 is not None:
        import rc_multicam
        cam2_shots = rc_multicam.xml_shots(out, tl, cam2, offset, args.template)
    write_xmeml(xml_path, f"{stem}_荒編集", tl, markers, cam2, offset, cam2_shots)

    # ---- タイトル・概要欄・チャプター ----
    meta_items, line_ids = rc_chapters.build_lines(items, item_ids)   # 10秒ごとくらいに1行にまとめる
    meta = {}
    if args.meta:
        meta = load_json_loose(args.meta)
        # claude.ai の返答の行番号は、プロンプトを作ったときの行。そのときの対応表があれば使う
        rc_chapters.attach_ids(meta, line_ids, rc_chapters.load_lines(out))
    elif args.llm == "api":
        meta, usage = rc_llm.generate_meta_api(meta_items, args.mode, args.effort)
        for k in usage:
            usage_total[k] = usage_total.get(k, 0) + usage[k]
        rc_chapters.attach_ids(meta, line_ids)
    elif args.llm == "manual":
        rc_llm.write_manual_meta_prompt(meta_items, args.mode, out / "prompt_youtube.md")
        rc_chapters.save_lines(out, line_ids)
    chapters = rc_chapters.resolve(meta, tl, by_id)

    write_youtube_md(out / "youtube.md", meta, chapters)
    write_report(out / "report.md", src, tl, chunks, cut_ids, cut_info, ai_cut_ids, preset, decisions, by_id)

    # 仕上げ工程（finish.py）に引き継ぐ情報
    session = {
        "version": 1,
        "mode": args.mode,
        "args": {
            "cam2": [str(p.resolve()) for p in (args.cam2 or [])],
            "glossary": str(args.glossary.resolve()) if args.glossary else None,
            "whisper_model": args.whisper_model,
            "caption_chars": args.caption_chars,
        },
        "stem": stem,
        "inputs": [str(f.path.resolve()) for f in src.files],
        "keep": keep,
        "cut_ids": sorted(cut_ids),
        "corrections": {str(k): v for k, v in corr.items()},
        "markers": [{"t": t, "name": n, "comment": c} for t, n, c in markers],
        "meta": meta,
        "chapters": chapters,   # 発言ID（id）と元素材の時刻（src）付き。カットを変えても同じ発言を指す
        "silent_work": [[round(s, 3), round(e, 3)] for s, e in silent_work],   # 工作モードの無言の作業（早送りに使う）
    }
    (out / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")

    print("■ 完了")
    print(f"  Premiereで読み込むファイル: {xml_path}")
    print(f"  字幕: {out / (stem + '_字幕.srt')}")
    print(f"  カットの一覧: {out / 'report.md'}")
    if meta:
        print(f"  タイトル・概要欄・チャプター: {out / 'youtube.md'}")
    elif args.llm == "manual":
        print(f"  タイトル等のプロンプト: {out / 'prompt_youtube.md'}（返答を保存して --meta で指定）")
    if usage_total["in"]:
        print(f"  Claude API: 入力{usage_total['in']:,} / 出力{usage_total['out']:,}トークン"
              f"（{rc_llm.cost_text(usage_total)}）")

    if args.finish:
        import finish
        finish.run(out, template=args.template, llm=args.llm, effort=args.effort)


def _template_or_none(path: Path):
    try:
        from rc_template import Template
        return Template(path) if (path / "template.toml").exists() else None
    except (Exception, SystemExit):  # noqa: BLE001  見た目の設定が読めなくても荒編集は続ける
        return None


def write_youtube_md(path: Path, meta: dict, chapters: list[dict]):
    if not meta:
        return
    lines = ["# 投稿用の情報（AI案）", "", "## タイトル案"]
    lines += [f"- {t}" for t in meta.get("titles", [])]
    lines += ["", "## 概要欄", "", meta.get("description", ""), "", "## チャプター（概要欄にそのまま貼れます）", ""]
    for ch in chapters:
        lines.append(f"{fmt_ts(ch['t'])} {ch['title']}")
    lines += ["", "## サムネイル文字案"] + [f"- {t}" for t in meta.get("thumbnail_texts", [])]
    lines += ["", "## タグ", "", ", ".join(meta.get("tags", []))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(path, src, tl, chunks, cut_ids, cut_info, ai_cut_ids, preset, decisions, by_id):
    lines = [
        "# 荒編集レポート", "",
        f"- 元の長さ: {fmt_ts(src.duration)}",
        f"- 編集後: {fmt_ts(tl.duration)}（{max(0, 100 * (1 - tl.duration / max(src.duration, 1))):.0f}%短縮）",
        f"- AIのカット判定: {len(ai_cut_ids)}チャンク（{'適用済み' if preset['apply_ai_cuts'] else 'マーカーのみ'}）",
        "", "## カットした発言（元素材の時刻）", "",
        "| 時刻 | 種類 | 理由 | 発言 |", "|---|---|---|---|",
    ]
    for c in chunks:
        if c.id in cut_ids:
            info = cut_info.get(c.id, {})
            lines.append(f"| {fmt_ts(c.start)} | {TYPE_JA.get(info.get('type'), '')} | "
                         f"{info.get('reason', '')} | {c.text.replace('|', '｜')} |")
    if decisions.get("review"):
        lines += ["", "## 要確認（カットしていません）", ""]
        for r in decisions["review"]:
            if r["id"] in by_id:
                c = by_id[r["id"]]
                lines.append(f"- {fmt_ts(c.start)} {r['note']}：「{c.text}」")
    if decisions.get("corrections"):
        lines += ["", "## 字幕の修正", ""]
        for r in decisions["corrections"]:
            if r.get("id") in by_id:
                lines.append(f"- 「{by_id[r['id']].text}」→「{r['text']}」")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
