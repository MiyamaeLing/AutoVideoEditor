"""仕上げ：荒編集の結果から、テロップ・ズーム・BGM・効果音・字幕入りの完成動画を書き出す。

使い方の例:
  python finish.py "D:\\撮影\\レビュー_roughcut"                 # AIの演出プラン → 書き出し
  python finish.py "D:\\撮影\\レビュー_roughcut" --preview 60    # 最初の60秒だけ試し書き出し
  python finish.py "D:\\撮影\\レビュー_roughcut" --plan plan_used.json  # プランを手直しして書き出し直す
  python finish.py --template-preview                            # テンプレートの見た目だけ確認
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import os
import shutil
import sys
import time
from fractions import Fraction
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401
import rc_broll
import rc_chapters
import rc_direct
import rc_graphics
import rc_llm
import rc_polish
import rc_style
import rc_multicam
import rc_plan
import rc_render
import rc_enhance
import rc_speedup
from rc_ass import build_ass
from rc_export import build_captions, mend_items, write_srt
from rc_media import SourceSet, probe, require_ffmpeg, snap_fps
from rc_template import Template
from rc_timeline import Timeline
from rc_transcribe import Chunk, fmt_ts

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def load_json_loose(path: Path) -> dict:
    s = path.read_text(encoding="utf-8-sig")
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        sys.exit(f"[エラー] {path} にJSONが見つかりません。")
    return json.loads(s[a:b + 1])


def output_fps(tpl: Template, src_fps: Fraction) -> Fraction:
    v = str(tpl.get("video", {}).get("fps", "auto"))
    return src_fps if v == "auto" else snap_fps(Fraction(v))


def split_by_zoom(tl: Timeline, zooms: list[tuple[float, float, float]]) -> list[rc_render.Seg]:
    """タイムラインの各クリップを、ズーム区間の境目で分ける。"""
    fps = tl.fps
    segs = []
    for p in tl.pieces:
        f = tl.sources.files[p.file_index]
        n = p.seq_end - p.seq_start
        local0 = p.g_in - tl.sources.offsets[p.file_index]
        cuts = {0, n}
        for s, e, *_ in zooms:
            for b in (s, e):
                if p.g_in < b < p.g_out:
                    cuts.add(round((b - p.g_in) * fps))
        cuts = sorted(c for c in cuts if 0 <= c <= n)
        for k0, k1 in zip(cuts, cuts[1:]):
            if k1 <= k0:
                continue
            mid = p.g_in + float((k0 + k1) / 2 / fps)
            hit = next((zz for zz in zooms if zz[0] <= mid < zz[1]), None)   # (開始, 終了, 倍率[, 中心])
            segs.append(rc_render.Seg(f.path, local0 + float(k0 / fps), k1 - k0, hit[2] if hit else 1.0,
                                      focus=hit[3] if hit and len(hit) > 3 else None))
    return segs


def avoid_chapter_cards(telops, chapters, card_dur):
    """章カードの表示中に出るテロップは、カードが消えた後にずらす（短くなりすぎるものは出さない）

    telops の各要素は (開始, 終了, ...) 。3つ目以降（文字・種類・効果音など）はそのまま引き継ぐ。
    """
    out = []
    for s, e, *rest in telops:
        for t, _ in chapters:
            if t - 0.2 < s < t + card_dur:
                s = t + card_dur + 0.1
        if e - s >= 1.2:
            out.append((s, e, *rest))
    return out


def clear_work(work: Path) -> None:
    """前回の作業フォルダを消す。使用中で消せないときは、分かりやすく伝えて止める。"""
    if not work.exists():
        return
    import rc_ops
    for _ in range(5):
        try:
            rc_ops.clean_work(work)   # 書き出し済みの本編パーツは残して、続きの書き出しで使い回す
            return
        except OSError:
            time.sleep(1.0)   # 中止した直後は、FFmpeg がファイルを放すまで少しかかることがある
    sys.exit(f"[エラー] 作業フォルダを消せませんでした: {work}\n"
             "  前の書き出しの FFmpeg がまだ動いているか、ほかのアプリがファイルを開いています。\n"
             "  少し待ってからもう一度実行してください。直らないときは PC を再起動してください。")


def thumb_llm(llm: str) -> str:
    """書き出し後のサムネイル案で AI を使うか。--llm の指定に従う（APIキーがなければ自動で選ぶ）"""
    if llm == "api" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("  [お知らせ] APIキーが設定されていないので、サムネイルの場面は自動で選びます。")
        return "none"
    return llm


def clip_seg(path: Path, fps: Fraction) -> rc_render.Seg:
    return rc_render.Seg(path, 0.0, round(rc_render.media_duration(path) * fps), 1.0, rc_render.has_audio(path))


def run(out_dir: Path, template: Path = DEFAULT_TEMPLATE, llm: str = "api", plan_file: Path | None = None,
        effort: str = "medium", preview: float | None = None, keep_work: bool = False,
        plan_only: bool = False, thumbs: bool = True, fx: dict | None = None) -> Path | None:
    """fx: 声と映像の補正の切り替え {"audio_clean"/"color"/"stabilize": True/False}（無ければテンプレート通り）"""
    require_ffmpeg()
    t0 = time.time()
    out_dir = Path(out_dir).resolve()   # FFmpeg は作業フォルダで動かすので、相対パスのままだと迷子になる
    sess_path = out_dir / "session.json"
    if not sess_path.exists():
        sys.exit(f"[エラー] {sess_path} がありません。先に roughcut.py を実行してください。")
    sess = json.loads(sess_path.read_text(encoding="utf-8"))
    tpl = Template(template)
    look = rc_style.current(tpl)   # 演出のスタイル（にぎやか / 落ち着いた）
    rc_style.activate(look)
    print(f"■ 仕上げ（テンプレート: {tpl.dir.name}）")

    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    fps = output_fps(tpl, src.main.fps)
    w, h = tpl["video"]["width"], tpl["video"]["height"]
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], fps)

    chunks = [Chunk(**c) for c in json.loads((out_dir / "chunks.json").read_text(encoding="utf-8"))]
    by_id = {c.id: c for c in chunks}
    cut = set(sess["cut_ids"])
    corr = {int(k): v for k, v in sess.get("corrections", {}).items()}
    items = []   # (id, 編集後の開始, 終了, テキスト)
    for c in chunks:
        if c.id in cut:
            continue
        r = tl.map_range(c.start, c.end)
        if r:
            items.append((c.id, r[0][0], r[-1][1], corr.get(c.id, c.text)))
    span = {i: (s, e) for i, s, e, _ in items}

    # ---- 演出プラン ----
    moods = [m for m, files in tpl.bgm_files().items() if files]
    sp_cfg = rc_speedup.config(tpl)   # 工作の作業シーンの早送り
    works = rc_speedup.work_ranges(sess, chunks, src.duration) if sp_cfg.get("enabled", True) else []
    usage = {"in": 0, "out": 0}
    if plan_file:
        plan = load_json_loose(plan_file)
        print(f"  演出プラン: {plan_file.name} を使います")
    elif llm == "api" and works:
        plan, usage = rc_speedup.make_plan_api(items, sess["mode"], moods, effort, works, tl, sp_cfg)
    elif llm == "api":
        plan, usage = rc_plan.make_plan_api(items, sess["mode"], moods, effort)
    elif llm == "manual":
        p = out_dir / "prompt_plan.md"
        if works:
            rc_speedup.write_manual_prompt(items, sess["mode"], moods, p, works, tl, sp_cfg)
        else:
            rc_plan.write_manual_prompt(items, sess["mode"], moods, p)
        print(f"  claude.ai に貼るプロンプトを作りました: {p}")
        print("  → 返答を claude_plan.json に保存して、--plan claude_plan.json を付けてもう一度実行してください。")
        return None
    else:
        plan = rc_plan.default_plan(items, moods)
    if not plan_file:   # 素材フォルダ（broll）があれば、差し込む場所も決める
        rc_broll.auto_place(out_dir, sess, tpl, items, plan, llm, effort, usage)
    speedups = rc_speedup.sanitize(plan, works, by_id, sp_cfg)
    plan = rc_plan.sanitize(plan, set(span), moods)
    look = rc_style.current(tpl, plan)   # 手直ししたプランに書いてあれば、そのスタイルで仕上げる
    rc_style.activate(look)
    plan = rc_style.clean_plan(plan, look)
    print(f"  演出のスタイル: {rc_style.LOOKS[look][0].split('（')[0]}")
    rc_style.apply_template(tpl, look)   # 落ち着いたスタイルでは、字幕を小さめ・細い縁取りにする
    if not plan_file and look != "calm":   # テロップの位置・強調・出方とズームの中心を、その場面の映像を見て決める
        rc_direct.direct(out_dir, plan, by_id, src, tpl, llm, effort, usage, rc_graphics.CARD_STYLES)
    if not plan_file and look == "calm" and llm == "api" and rc_style.config(tpl)["polish_captions"]:
        # 字幕の文を読みやすく整える（言葉の途中で区切られた所をつなぎ直してから渡す）
        fixed = mend_items([(s, e, t) for _, s, e, t in items])
        plan["caption_text"] = rc_polish.polish_api([(i, m[2]) for (i, *_x), m in zip(items, fixed)], effort, usage)
    for k in ("speedups", "speedups_auto"):   # 早送りは rc_speedup.sanitize の結果だけを使う
        plan.pop(k, None)
    if works or speedups:
        plan.update(rc_speedup.plan_for_save(speedups))
    # 2台目カメラがあれば、手元カメラを使う場面（plan["angles"]）を用意する（無ければ None）
    mc = rc_multicam.prepare(out_dir, sess, tl, chunks, plan, tpl, llm, effort, usage)
    edited = out_dir / "plan_edited.json"
    if edited.exists() and not (plan_file and Path(plan_file).resolve() == edited.resolve()):
        edited.unlink()   # 新しいプランを使うので、前のプランへの手直し（画面の表）は使わない
    (out_dir / "plan_used.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    if plan.get("broll"):
        try:
            xml = rc_broll.write_xml(out_dir, sess, plan, tpl)
            if xml:
                print(f"  Premiere用XML（Bロール入り）: {xml}")
        except (Exception, SystemExit) as e:  # noqa: BLE001  XMLの失敗で書き出しを止めない
            print(f"  [注意] Bロール入りのXMLを作れませんでした: {e}")
    if plan_only:
        print(f"■ 演出プランを作りました: {out_dir / 'plan_used.json'}")
        if usage["in"]:
            print(f"  Claude API: {rc_llm.cost_text(usage)}")
        return None
    print(f"  テロップ {len(plan['telops'])} / ズーム {len(plan['zooms'])} / BGM区間 {len(plan['bgm'])}")
    if plan.get("speedups"):
        print(f"  早送り {len(plan['speedups'])}か所（" + "、".join(
            f"{fmt_ts(x['start'])}から ×{rc_speedup.fmt_speed(x['speed'])}" for x in plan["speedups"][:6]) + "）")
    if not moods:
        print("  [注意] BGMが登録されていません（テンプレートの bgm フォルダに曲を入れると使われます）")

    # ---- 本編の素材リスト ----
    zcfg = tpl.get("zoom", {})
    zooms = []
    for z in plan["zooms"]:
        c = by_id[z["id"]]
        zooms.append((c.start - 0.1, c.end + 0.3, float(zcfg.get(z["strength"], 1.15)), rc_direct.zoom_focus(z)))
    ftl = tl   # 完成（本編）の時刻に直すタイムライン。早送りがあれば、その分を縮めたもの
    if plan.get("speedups"):
        ftl = rc_speedup.SpeedTimeline(tl, plan["speedups"], zooms, sp_cfg["audio"], sp_cfg["audio_volume_db"])
        items, span = rc_speedup.retime_items(chunks, cut, corr, ftl, span)
        mc = rc_multicam.retime(mc, ftl)   # カメラの最短ショット長を、早送り後の時刻で守る
    segs = ftl.segs if ftl is not tl else split_by_zoom(tl, zooms)
    segs = rc_multicam.apply_segs(segs, src, fps, mc, w, h)   # 手元カメラの区間は映像を2台目に差し替える
    segs = rc_broll.attach_to_segs(segs, plan.get("broll", []), span, tpl, fps, w, h, out_dir)
    body_frames = sum(s.frames for s in segs)
    if preview:
        keep_frames, out = round(preview * fps), []
        for s in segs:
            if keep_frames <= 0:
                break
            out.append(dataclasses.replace(s, frames=min(s.frames, keep_frames)))
            keep_frames -= out[-1].frames
        segs, body_frames = out, sum(s.frames for s in out)
    body_dur = float(body_frames / fps)

    intro_p = tpl.path(tpl.get("intro", {}).get("file", ""))
    outro_p = tpl.path(tpl.get("outro", {}).get("file", ""))
    if not preview:   # [opening] / [ending] が有効なら、テンプレートから自動で作ったものを使う
        intro_p, outro_p = rc_graphics.auto_intro_outro(tpl, intro_p, outro_p)
    intro = clip_seg(intro_p, fps) if intro_p and not preview else None
    outro = clip_seg(outro_p, fps) if outro_p and not preview else None
    off = float(intro.frames / fps) if intro else 0.0
    all_segs = ([intro] if intro else []) + segs + ([outro] if outro else [])
    total = float(sum(s.frames for s in all_segs) / fps)

    def inside(t):
        return t < body_dur - 0.05

    # ---- 字幕・テロップ・章カード ----
    max_chars = tpl["caption"].get("max_chars", 24)
    # 文字起こしが言葉の途中で区切った所（「…にある工」「房と…」）は、言葉がつながるように直してから字幕にする
    cap_items = [(i, *m) for (i, *_x), m in zip(items, mend_items([(s, e, t) for _, s, e, t in items]))]
    cap_items = rc_polish.texts(cap_items, plan.get("caption_text"))   # AIが整えた文があれば、それを使う
    caps = [(s + off, e + off, t) for s, e, t in build_captions([(s, e, t) for _, s, e, t in cap_items], max_chars)
            if inside(s)]
    if look == "calm":   # 落ち着いたスタイルでは、字幕の終わりの「、」を出さない
        caps = [(s, e, t.rstrip("、，,")) for s, e, t in caps if t.rstrip("、，,")]
    telops, se_events = [], []
    order = [i for i, *_x in items]
    said = {i: (s, e, t) for i, s, e, t in items}
    all_telops = sorted(plan["telops"], key=lambda t: span[t["start_id"]][0])
    plan_telops = [t for t in all_telops if t["style"] != rc_style.LABEL]
    labels = []   # 左上の小さなラベル（落ち着いたスタイル）。その言葉を言う所で出して、章が終わるまで残す
    for t in all_telops:
        if t["style"] == rc_style.LABEL:
            k0, k1 = order.index(t["start_id"]), order.index(t["end_id"])
            s = rc_direct.cue(t["text"], [said[i] for i in order[k0:k1 + 1]])
            if inside(s):
                labels.append((s + off, t["text"]))
    for n, t in enumerate(plan_telops):
        # その言葉を言う所で出す（発言の頭で出すと、まだ言っていない結論が先に見えてしまう）
        k0, k1 = order.index(t["start_id"]), order.index(t["end_id"])
        s = rc_direct.cue(t["text"], [said[i] for i in order[k0:k1 + 1]])
        lo, hi = rc_direct.show_seconds(t["text"], t["style"])
        e = min(max(span[t["end_id"]][1] + 0.4, s + lo), s + hi)
        if n + 1 < len(plan_telops):   # 長く出すぶんが、次のテロップにかぶらないようにする
            nxt = span[plan_telops[n + 1]["start_id"]][0]
            if nxt - 0.2 > s + 1.8:
                e = min(e, max(nxt - 0.2, min(span[t["end_id"]][1], s + hi)))
        if not inside(s):
            continue
        se = tpl.se_file(t["se"]) if t.get("se", "none") != "none" else None
        telops.append((s + off, min(e, body_dur) + off, t["text"], t["style"], se,
                       rc_direct.telop_opts(t, t["style"] in rc_graphics.CARD_STYLES)))
    # チャプターは発言IDから、この書き出しのタイムライン（出力fps）で時刻を求め直す
    sess_chapters = rc_chapters.retime(sess.get("chapters", []), ftl, by_id)
    chapters = [(c["t"] + off, c["title"]) for c in sess_chapters if c["t"] > 0.5 and inside(c["t"])]
    labels = rc_style.schedule_labels(labels, [t for t, _ in chapters], off + body_dur, rc_style.config(tpl))
    if look == "calm":
        chapters = []   # 落ち着いたスタイルでは、章カードを画面に出さない（チャプターの一覧は今までどおり作る）
    telops = avoid_chapter_cards(telops, chapters, tpl.get("chapter_card", {}).get("duration", 2.5))
    # 効果音は、章カードを避けてずらした後のテロップの時刻で鳴らす（出さないテロップの音は鳴らさない）
    se_events = [(s, f) for s, _e, _t, _st, f, _o in telops if f]
    telops = [(s, e, text, style, opts) for s, e, text, style, _f, opts in telops]

    # ---- BGM ----
    bgm_files = tpl.bgm_files()
    cyc = {m: itertools.cycle(fs) for m, fs in bgm_files.items() if fs}
    sections = []
    starts = [(span[b["start_id"]][0], b["mood"]) for b in plan["bgm"]]
    if starts:
        starts[0] = (0.0, starts[0][1])
    for (s, mood), nxt in zip(starts, starts[1:] + [(body_dur, None)]):
        e = min(nxt[0], body_dur)
        if mood in cyc and e - s > 1 and inside(s):
            sections.append((s + off, e + off, next(cyc[mood])))

    # ---- 書き出し ----
    work = out_dir / "work"
    clear_work(work)
    work.mkdir(exist_ok=True)
    final_args, inter_args, enc = rc_render.pick_encoder(tpl["video"].get("encoder", "auto"),
                                                         int(tpl["video"].get("quality", 19)))
    print(f"  エンコーダ: {enc} / {w}x{h} {float(fps):.3f}fps / 完成の長さ {fmt_ts(total)}"
          + (f"（プレビュー {preview:.0f}秒）" if preview else ""))
    # 声と映像の補正（rc_enhance）。オープニング・エンディングにはかけない
    enh = rc_enhance.resolve(tpl, sess.get("mode", "talk"), fx)
    for n in enh.notes:
        print(f"  [注意] {n}")
    print(f"  補正: {enh.describe()}", flush=True)
    rc_enhance.apply_video(enh, segs, work, fps)
    body = rc_render.render_body(all_segs, w, h, fps, work, inter_args)
    print("    音声のミックス（BGM・効果音・音量の調整）", flush=True)
    loud = rc_render.mix_audio(body, total, sections, se_events, tpl, work,
                               voice_fx=rc_enhance.voice_fx(enh, off, off + body_dur, total,
                                                            [(f.path, 0.0, f.duration) for f in src.files]))
    # 話者の区別（実験的な機能）：speakers.json があれば、字幕を人ごとの色にする
    import rc_speakers
    caps_ass = rc_speakers.decorate_captions(out_dir, tpl, cap_items, max_chars, off, inside) or caps
    progress = ([(c["t"] + off, c["title"]) for c in sess_chapters], off, off + body_dur)   # 章の進み具合
    ass = build_ass(tpl, w, h, caps_ass, telops, chapters, progress, keywords=plan.get("caption_keywords") or ())
    pc = rc_graphics.progress_config(tpl)
    ass = rc_style.add_events(ass, rc_style.label_events(
        labels, w, h, tpl, top=pc["height"] + 40 if pc.get("enabled") and pc.get("position", "top") != "bottom" else 0),
        rc_style.label_styles(tpl, h))
    if ftl is not tl:   # 早送り中は画面の隅に「×4」などを出す
        ass = rc_speedup.add_badges(ass, tpl, w, h, ftl.badge_ranges(), off, body_dur)
    (work / "subs.ass").write_text(ass, encoding="utf-8")
    logo = tpl.path(tpl.get("logo", {}).get("file", ""))
    stem = sess["stem"]
    out_path = out_dir / (f"{stem}_プレビュー.mp4" if preview else f"{stem}_完成.mp4")
    print("    最終書き出し（字幕・テロップの焼き込み）", flush=True)
    rc_render.render_final(body, work / "mix.wav", loud, "subs.ass", tpl.fonts_dir, logo, tpl, w, h,
                           final_args, out_path, work)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)

    if not preview:
        # 完成動画に合わせた字幕とチャプター（オープニングの分もずらしてある）。YouTube への設定で使う
        write_srt(out_dir / f"{stem}_完成.srt", caps)
        final_chapters = ([{"t": 0.0, "title": "オープニング"}] if off > 0 else []) + \
            [{"t": round(c["t"] + off, 2), "title": c["title"]} for c in sess_chapters]
        (out_dir / "final_info.json").write_text(json.dumps({
            "video": str(out_path), "duration": round(total, 2), "intro_offset": round(off, 2),
            "chapters": final_chapters, "srt": str(out_dir / f"{stem}_完成.srt"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        if plan.get("speedups"):
            xp = rc_speedup.write_session_xml(out_dir, plan["speedups"])
            print(f"  Premiere用（早送り入り）: {xp}")

    if thumbs and not preview:
        try:
            import rc_thumb
            # サムネイルのAIの考える深さは、テンプレートの [thumbnail] の effort（動画作成の設定とは別。初期値 high）
            rc_thumb.generate(out_dir, tpl, thumb_llm(llm))
        except (Exception, SystemExit) as e:  # noqa: BLE001  サムネイルの失敗で全体を止めない
            print(f"  [注意] サムネイル案を作れませんでした: {e}")

    print("■ 仕上げ完了")
    print(f"  完成動画: {out_path}")
    print(f"  演出プラン: {out_dir / 'plan_used.json'}（書き換えて --plan で指定すると書き出し直せます）")
    print(f"  所要時間: {fmt_ts(time.time() - t0)}")
    if usage["in"]:
        print(f"  Claude API: {rc_llm.cost_text(usage)}")
    return out_path


def template_preview(template: Path, sample: Path | None) -> None:
    """テンプレートの見た目を、8秒のサンプル動画と静止画で確認する。"""
    require_ffmpeg()
    tpl = Template(template)
    w, h = tpl["video"]["width"], tpl["video"]["height"]
    fps = Fraction(30)
    work = tpl.dir / "_preview_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    if sample:
        segs = [rc_render.Seg(sample.resolve(), 0.0, 8 * 30, 1.0, rc_render.has_audio(sample))]
    else:
        run_lavfi = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                     f"gradients=s={w}x{h}:d=8:speed=0.01:c0=0x334455:c1=0x667788", "-c:v", "libx264", "-t", "8",
                     str(work / "bg.mp4")]
        rc_render.run(run_lavfi, what="背景の作成")
        segs = [rc_render.Seg(work / "bg.mp4", 0.0, 8 * 30, 1.0, False)]
    _, inter, _ = rc_render.pick_encoder("auto", 19)
    final, _, _ = rc_render.pick_encoder(tpl["video"].get("encoder", "auto"), 19)
    # テンプレートの色の設定（LUT など）も見本に反映する（手ブレ補正は見本ではしない）
    rc_enhance.apply_video(rc_enhance.resolve(tpl, "talk", {"stabilize": False}), segs, work, fps)
    body = rc_render.render_body(segs, w, h, fps, work, inter)
    caps = [(0.3, 2.8, "これは字幕のサンプルです"), (3.0, 5.4, "ここが一番大事なポイントです"),
            (5.6, 7.9, "最後まで見てくれてありがとう")]
    telops = [(0.6, 2.8, "重さ わずか1.2kg！", "emphasis"), (3.0, 5.4, "マキタ DF033D", "info"),
              (3.2, 5.4, "ここ注意", "warning"), (5.6, 7.9, "いや軽すぎ！", "funny")]
    chapters = [(5.6, "実際に使ってみた")]
    ass = rc_speedup.add_badges(build_ass(tpl, w, h, caps, telops, chapters), tpl, w, h, [(0.3, 2.8, 4, "fast")])
    (work / "subs.ass").write_text(ass, encoding="utf-8")
    logo = tpl.path(tpl.get("logo", {}).get("file", ""))
    out = tpl.dir / "template_preview.mp4"
    rc_render.render_final(body, body, {}, "subs.ass", tpl.fonts_dir, logo, tpl, w, h, final, out, work)
    for t, name in ((1.5, "preview_1.png"), (4.5, "preview_2.png"), (6.5, "preview_3.png")):
        rc_render.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                       str(tpl.dir / name)], what="静止画")
    shutil.rmtree(work, ignore_errors=True)
    print(f"  見た目の確認用: {out}")
    print(f"  静止画: {tpl.dir / 'preview_1.png'} など3枚")


def main():
    ap = argparse.ArgumentParser(description="仕上げ（演出・書き出し）")
    ap.add_argument("out_dir", nargs="?", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--llm", choices=["api", "manual", "none"], default="api",
                    help="api=Claude APIで演出プラン / manual=claude.ai用のプロンプトを作る / none=字幕とBGMだけ")
    ap.add_argument("--plan", type=Path, help="演出プランのJSON（手直ししたもの、またはclaude.aiの返答）")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--look", choices=list(rc_style.LOOKS), help="演出のスタイル（lively=にぎやか / calm=落ち着いた）")
    ap.add_argument("--preview", type=float, help="最初のN秒だけ書き出して確認する")
    ap.add_argument("--keep-work", action="store_true", help="中間ファイルを消さずに残す")
    ap.add_argument("--plan-only", action="store_true", help="演出プランだけ作って、書き出しはしない")
    ap.add_argument("--no-thumbs", action="store_true", help="書き出しのあとにサムネイル案を作らない")
    ap.add_argument("--template-preview", action="store_true", help="テンプレートの見た目だけを確認する")
    ap.add_argument("--sample", type=Path, help="--template-preview の背景に使う動画")
    for opt, what in (("--audio-clean", "声を聞き取りやすくする処理"), ("--color", "明るさ・色の補正"),
                      ("--stabilize", "手ブレ補正")):
        ap.add_argument(opt, choices=["auto", "on", "off"], default="auto",
                        help=f"{what}（auto=テンプレートと撮影ジャンルに合わせる）")
    args = ap.parse_args()
    if args.template_preview:
        template_preview(args.template, args.sample)
        return
    if not args.out_dir:
        ap.error("roughcut.py の出力フォルダを指定してください")
    if args.look:
        rc_style.activate(args.look)
    fx = {k: v == "on" for k, v in (("audio_clean", args.audio_clean), ("color", args.color),
                                     ("stabilize", args.stabilize)) if v != "auto"}
    run(args.out_dir, args.template, args.llm, args.plan, args.effort, args.preview, args.keep_work, args.plan_only,
        not args.no_thumbs, fx)


if __name__ == "__main__":
    main()
