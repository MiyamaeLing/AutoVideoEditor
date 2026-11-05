"""MVモード：曲のビートに合わせて映像素材をつなぎ、MVのラフ（mp4）と Premiere用XML を作る。

使い方の例:
  python mv.py 曲.wav 素材フォルダ                       # おまかせ（動き・明るさで自動）
  python mv.py 曲.mp3 素材フォルダ --order ai --note "夏・青春・疾走感"   # Claudeが場面を見て並べる
  python mv.py 曲.mp3 A.mp4 B.mp4 --order files          # 撮った順に並べる
  python mv.py 曲.mp3 素材フォルダ --part sabi --size 1080x1920   # サビだけ・縦長（ショート用）
  python mv.py 曲.mp3 --analyze-only                     # 曲の解析だけ（テンポ・サビの位置）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401  (Windowsでコンソール画面を出さない)
import rc_mv
from rc_media import require_ffmpeg, snap_fps
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="MVモード：ビートに合わせてカットをつないだMVのラフを作る")
    ap.add_argument("song", type=Path, help="曲の音声ファイル（mp3 / wav / m4a など）")
    ap.add_argument("materials", nargs="*", type=Path, help="映像素材のファイルまたはフォルダ（いくつでも）")
    ap.add_argument("--order", choices=["auto", "ai", "files"], default="auto",
                    help="素材の選び方: auto=動き・明るさで自動 / ai=Claudeが場面を見て並べる / files=撮った順")
    ap.add_argument("--note", default="", help="曲の雰囲気・歌詞のテーマなどのメモ（AIで並べるときに使う）")
    ap.add_argument("--pace", choices=["fast", "normal", "slow"], help="カットの速さ（初期値: テンプレートの [mv] pace）")
    ap.add_argument("--part", choices=["all", "sabi"], default="all", help="all=曲全体 / sabi=一番盛り上がるサビだけ")
    ap.add_argument("--start", type=float, help="使う範囲の開始（秒）")
    ap.add_argument("--end", type=float, help="使う範囲の終了（秒）")
    ap.add_argument("--bpm", type=float, help="テンポを手で指定する（自動の結果がずれるとき）")
    ap.add_argument("--beats-per-bar", type=int, default=4, help="1小節の拍の数（3拍子なら 3）")
    ap.add_argument("--size", help="画面の大きさ 例: 1920x1080 / 1080x1920（縦）/ 1080x1080")
    ap.add_argument("--fps", help="フレームレート 例: 30 / 29.97 / 24（初期値: 素材に合わせる）")
    ap.add_argument("--fit", choices=["cover", "contain"], help="cover=画面いっぱい（はみ出しは切る）/ contain=黒帯で全体を入れる")
    ap.add_argument("--markers", choices=["beats", "bars", "sections"], help="Premiereのマーカー: 拍ごと / 小節ごと / 区間だけ")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="見た目のテンプレートフォルダ")
    ap.add_argument("--out", type=Path, help="出力フォルダ（初期値: 曲と同じ場所に「<曲名>_mv」）")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium", help="Claudeの考える深さ")
    ap.add_argument("--seed", type=int, default=1, help="数字を変えると、おまかせの組み合わせが変わる")
    ap.add_argument("--preview", type=float, help="最初の○秒だけ試しに書き出す")
    ap.add_argument("--analyze-only", action="store_true", help="曲の解析だけ行う（素材は不要）")
    ap.add_argument("--no-render", action="store_true", help="動画は書き出さず、Premiere用XMLだけ作る")
    return ap.parse_args(argv)


def parse_size(s: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in s.lower().replace("×", "x").split("x"))
    except ValueError:
        sys.exit(f"[エラー] 画面の大きさの書き方が違います: {s}（例: 1920x1080）")
    return max(16, w // 2 * 2), max(16, h // 2 * 2)


def choose_fps(arg: str | None, cfg: str, clips: list[rc_mv.Clip]) -> Fraction:
    v = arg or str(cfg or "auto")
    if v != "auto":
        try:
            return snap_fps(Fraction(v))
        except (ValueError, ZeroDivisionError):
            sys.exit(f"[エラー] フレームレートの書き方が違います: {v}")
    # 素材の長さの合計が一番長いfpsに合わせる
    cnt: Counter = Counter()
    for c in clips:
        cnt[c.fps] += c.duration
    return Fraction(cnt.most_common(1)[0][0]) if cnt else Fraction(30)


def load_song(args, out: Path) -> dict:
    cache = out / "mv_song.json"
    key = rc_mv.file_key(args.song, f"{args.bpm}|{args.beats_per_bar}|v1")
    if cache.exists():
        try:
            an = json.loads(cache.read_text(encoding="utf-8"))
            if an.get("key") == key:
                rc_mv.log("  前回の解析結果を使います")
                return an
        except Exception:  # noqa: BLE001
            pass
    an = rc_mv.analyze_song(args.song, args.bpm, args.beats_per_bar)
    cache.write_text(json.dumps(an, ensure_ascii=False, indent=1), encoding="utf-8")
    return an


def print_song(an: dict) -> None:
    rc_mv.log(f"  テンポ {an['bpm']:.1f} BPM・{an['beats_per_bar']}拍子・拍 {len(an['beats'])}個・"
              f"長さ {rc_mv._fmt(an['duration'])}")
    for i, s in enumerate(an["sections"]):
        bar = "■" * max(1, int(s["energy"] * 10))
        rc_mv.log(f"    区間{i} {rc_mv._fmt(s['start'])}〜{rc_mv._fmt(s['end'])}  {s['label']:<8} {bar}")
    bc = an["best_chorus"]
    rc_mv.log(f"  一番の盛り上がり: {rc_mv._fmt(bc['start'])}〜{rc_mv._fmt(bc['end'])}")


def _ai_client_check():
    """APIキーがなければ、分かりやすく止める"""
    import rc_llm
    try:
        rc_llm._client()
    except SystemExit:
        sys.exit("[エラー] ANTHROPIC_API_KEY が設定されていません。素材の選び方を「おまかせ」（--order auto）"
                 "にすれば、APIキーなしで使えます。")


def main(argv=None):
    args = parse_args(argv)
    require_ffmpeg()
    if not args.song.exists():
        sys.exit(f"[エラー] 曲のファイルが見つかりません: {args.song}")
    if args.song.suffix.lower() not in rc_mv.AUDIO_EXT | rc_mv.VIDEO_EXT:
        sys.exit(f"[エラー] 曲のファイルの形式が分かりません: {args.song.name}（mp3・wav・m4a などを指定してください）")
    tpl = Template(args.template)
    cfg = {**rc_mv.DEFAULTS, **(tpl.get("mv", {}) or {})}
    out = (args.out or args.song.parent / f"{args.song.stem}_mv").resolve()
    out.mkdir(parents=True, exist_ok=True)

    rc_mv.log("■ 曲の解析（テンポ・拍・サビ）")
    song_info = rc_mv.probe_song(args.song)
    an = load_song(args, out)
    print_song(an)
    if args.analyze_only:
        rc_mv.log("■ 完了")
        rc_mv.log(f"  解析結果: {out / 'mv_song.json'}")
        return

    mats = rc_mv.collect_materials(args.materials, exclude=out)
    if not mats:
        sys.exit("[エラー] 映像素材がありません。動画ファイルか、動画の入ったフォルダを指定してください。")
    if args.order == "ai":
        _ai_client_check()

    rc_mv.log(f"■ 素材の解析（{len(mats)}本）")
    clips = rc_mv.analyze_clips(mats, out / "mv_clips.json")
    total_src = sum(c.duration for c in clips)
    rc_mv.log(f"  素材の合計 {rc_mv._fmt(total_src)}")

    w, h = parse_size(args.size) if args.size else (int(cfg["width"]), int(cfg["height"]))
    fps = choose_fps(args.fps, cfg["fps"], clips)
    pace = args.pace or str(cfg["pace"])
    fit = args.fit or str(cfg["fit"])
    markers_mode = args.markers or str(cfg["markers"])
    a, b = rc_mv.song_range(an, args.part, args.start, args.end)
    if args.preview:
        b = min(b, a + float(args.preview))

    rc_mv.log(f"■ カットの計画（{PACE(pace)}・{rc_mv.ORDER_JA[args.order]}）")
    cuts = rc_mv.plan_cuts(an, a, b, pace, fps, float(cfg["min_cut"]))
    longest = max(c.t1 - c.t0 for c in cuts)
    wins = rc_mv.build_windows(clips, min(12.0, max(2.0, longest + 0.2)))
    concept, usage = "", None
    if args.order == "files":
        rc_mv.assign_files(cuts, wins, clips)
    elif args.order == "ai":
        rc_mv.log("  Claudeが代表フレームを見て、並べ方を考えています…")
        try:
            res, usage, shown = rc_mv.ai_order(an, cuts, wins, clips, args.note, args.effort,
                                               out / "mv_frames", int(cfg["ai_frames"]))
            concept = res.get("concept", "")
            left = rc_mv.apply_ai(cuts, wins, shown, res, args.seed)
            if left:
                avoid = {shown[n - 1].no for n in res.get("avoid", []) if 0 < n <= len(shown)}
                rc_mv.assign_auto(cuts, wins, args.seed, avoid=avoid, only=left)
            if concept:
                rc_mv.log(f"  AIの考え: {concept}")
        except Exception as e:  # noqa: BLE001
            rc_mv.log(f"  [注意] AIでの並べ替えに失敗したので、おまかせ（自動）で並べます: {e}")
            for w_ in wins:
                w_.uses, w_.used = 0, []
            rc_mv.assign_auto(cuts, wins, args.seed)
    else:
        rc_mv.assign_auto(cuts, wins, args.seed)
    total_frames = cuts[-1].f1
    use = Counter(Path(clips[c.clip].path).name for c in cuts)
    rc_mv.log(f"  カット {len(cuts)}個（平均 {sum(c.t1 - c.t0 for c in cuts) / len(cuts):.2f}秒）")
    for name, n in use.most_common():
        rc_mv.log(f"    {name}: {n}カット")
    unused = [p.name for p in mats if p.name not in use]
    if unused:
        rc_mv.log(f"    使わなかった素材: {'、'.join(unused[:8])}{' ほか' if len(unused) > 8 else ''}")

    stem = args.song.stem
    suffix = "_試し" if args.preview else ""
    plan = {
        "song": str(args.song.resolve()), "range": [a, b], "fps": f"{fps.numerator}/{fps.denominator}",
        "size": [w, h], "order": args.order, "pace": pace, "fit": fit, "note": args.note, "concept": concept,
        "bpm": an["bpm"], "total_frames": total_frames, "files": [c.path for c in clips],
        "cuts": [{"t0": c.t0, "t1": c.t1, "f0": c.f0, "f1": c.f1, "section": c.section, "kind": c.kind,
                  "beats": c.beats, "file": c.clip, "src_in": c.src_in, "why": c.why} for c in cuts],
        "video": str(out / f"{stem}_MVラフ{suffix}.mp4"), "xml": str(out / f"{stem}_MV{suffix}.xml"),
    }
    (out / "mv_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    rc_mv.log("■ Premiere用XMLの書き出し")
    marks = rc_mv.beat_markers(an, a, b, markers_mode)
    xml_path = out / f"{stem}_MV{suffix}.xml"
    rc_mv.write_xml(xml_path, f"{stem} MV", cuts, clips, args.song, song_info, a, total_frames, fps, w, h, marks)
    rc_mv.log(f"  マーカー {len(marks)}個")

    video = out / f"{stem}_MVラフ{suffix}.mp4"
    if not args.no_render:
        import rc_render
        v = tpl.get("video", {})
        enc_final, enc_inter, enc_name = rc_render.pick_encoder(str(v.get("encoder", "auto")), int(v.get("quality", 19)))
        rc_mv.log(f"■ MVラフの書き出し（{w}x{h}・{float(fps):.3f}fps・{enc_name}）")
        rc_mv.render(cuts, clips, args.song, a, total_frames, w, h, fps, fit, enc_final, enc_inter,
                     float(cfg["fade_out"]) if b < an["duration"] - 0.5 else 0.0,
                     video, out / "_mv_work", int(cfg["batch"]))
        import shutil
        shutil.rmtree(out / "_mv_work", ignore_errors=True)

    rc_mv.log("■ 完了")
    if not args.no_render:
        rc_mv.log(f"  MVラフ: {video}")
    rc_mv.log(f"  Premiere用XML: {xml_path}（拍の位置にマーカー入り）")
    rc_mv.log(f"  カットの一覧: {out / 'mv_plan.json'}")
    if usage and usage.get("in"):
        import rc_llm
        rc_mv.log(f"  Claude API: 入力{usage['in']:,} / 出力{usage['out']:,}トークン（約${rc_llm.cost_usd(usage):.2f}）")


def PACE(p: str) -> str:  # noqa: N802
    return f"カット{rc_mv.PACE_JA.get(p, p)}"


if __name__ == "__main__":
    main()
