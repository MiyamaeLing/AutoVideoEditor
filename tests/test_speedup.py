"""工作の作業シーンの自動早送りのテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_speedup.py          # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 倍率の自動決定（2〜8倍・タイムラプス）と、プランの "speedups" の読み方（自動・AIの返答・手直し）
  - 早送り後の時刻の変換（字幕・テロップ・チャプター・BGM・サムネイル候補）が映像と同じ計算になる
  - 3時間の素材・数千カットでも、メモリと時間をあまり使わずに変換できる
  - 工作モードの荒編集で session.json に作業区間が入る
  - 書き出し: 作業の場面が短くなる・音が消える・隅に「×2」が出る・字幕とチャプターの時刻が合う
  - Premiere 用の「早送り入り」XML（速度とマーカー）
  - AI（偽の Claude）が倍率を変えたり早送りを足したりできる・claude.ai 用のプロンプトにも入る
  - GUI の「早送り」の表（使う／使わない・速さ・再生の速さ・保存）
  - レビューモード（talk）の結果は今まで通り
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable
WORK = (29.85, 39.8)   # テスト素材で、工作モードの無言の作業になる区間（発言8の終わり〜発言9の始まり）


def _run(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=900)


def _tail(p: subprocess.CompletedProcess, n: int = 15) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def _max_volume(p: Path, ss: float, t: float) -> float:
    e = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{ss:.3f}", "-t", f"{t:.3f}", "-i", str(p), "-vn",
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"max_volume: (-?[\d.]+) dB", e)
    return float(m.group(1)) if m else 0.0


# ---------------------------------------------------------------- 部品
def t_units(check):
    import rc_render
    import rc_speedup as S
    cfg = S.config(None)
    got = {d: S.auto_speed(d, cfg) for d in (4, 10, 25, 40, 80, 200, 600)}
    check("早送り: 短い作業は早送りにしない", got[4] is None, str(got[4]))
    check("早送り: 長さに応じて2〜8倍", [got[d][0] for d in (10, 25, 40, 80, 200)] == [2, 2, 4, 8, 8]
          and all(got[d][1] == "fast" for d in (10, 25, 40, 80, 200)), str(got))
    check("早送り: とても長い作業はタイムラプス", got[600] == (30.0, "timelapse"), str(got[600]))

    works = [(10.0, 50.0), (100.0, 103.0), (200.0, 900.0)]
    by_id = {1: {"start": 300.0, "end": 305.0}, 2: {"start": 306.0, "end": 330.0}}
    auto = S.sanitize({}, works, by_id, cfg)
    check("早送り: プランに無ければ自動", [(x["work"], x["speed"], x["style"]) for x in auto]
          == [(1, 4.0, "fast"), (3, 30.0, "timelapse")], str(auto))
    ai = S.sanitize({"speedups": [{"work": 1, "start_id": 0, "end_id": 0, "speed": 7, "style": "fast"},
                                  {"work": 3, "start_id": 0, "end_id": 0, "speed": 0, "style": "none"},
                                  {"work": 0, "start_id": 1, "end_id": 2, "speed": 3, "style": "fast"},
                                  {"work": 0, "start_id": 99, "end_id": 2, "speed": 3, "style": "fast"},
                                  {"work": 2, "start_id": 0, "end_id": 0, "speed": 50, "style": "fast"}]},
                    works, by_id, cfg)
    check("早送り: AIの返答で倍率を変える・やめる・足す（倍率は範囲内に）",
          [(x["start"], x["end"], x["speed"], x["work"]) for x in ai]
          == [(10.0, 50.0, 7.0, 1), (100.0, 103.0, 8.0, 2), (300.0, 330.0, 3.0, 0)], str(ai))
    fixed = S.sanitize({"speedups": [], "speedups_auto": False}, works, by_id, cfg)
    check("早送り: 手直しで全部外したら自動で足さない", fixed == [])
    off = S.sanitize({}, works, by_id, {**cfg, "enabled": False})
    check("早送り: テンプレートで自動をやめられる", off == [])
    ov = S.sanitize({"speedups": [{"start": 10, "end": 30, "speed": 4, "style": "fast"},
                                  {"start": 20, "end": 40, "speed": 4, "style": "fast"}], "speedups_auto": False},
                    works, by_id, cfg)
    check("早送り: 重なった早送りは1つにする", len(ov) == 1)

    check("書き出し: 早送りの音の速さ", rc_render._tempo(4) == "atempo=2,atempo=2,"
          and rc_render._tempo(3) == "atempo=2,atempo=1.5," and rc_render._tempo(1) == ""
          and rc_render._tempo(2, -18) == "atempo=2,volume=-18dB,")
    seg1 = rc_render.Seg(Path("a.mp4"), 1.0, 30)
    seg4 = rc_render.Seg(Path("b.mp4"), 2.0, 30, has_audio=False, speed=4.0)
    cmd = rc_render._batch_cmd([seg1, seg4], 1920, 1080, Fraction(30), Path("o.mkv"), [])
    fc = cmd[cmd.index("-filter_complex") + 1]
    check("書き出し: ふつうのカットは今まで通り・早送りは setpts と無音",
          "[0:v]crop" not in fc and fc.startswith("[0:v]scale=") and "[1:v]setpts=(PTS-STARTPTS)/4," in fc
          and "anullsrc" in fc and cmd[cmd.index("b.mp4") - 2] == "4.1000", fc[:200])

    from rc_template import Template
    import make_fixtures
    tpl = Template(make_fixtures.FX / "tpl")
    ass = S.add_badges("[V4+ Styles]\nFormat: x\nStyle: Caption,a\n\n[Events]\nFormat: y\n", tpl, 1920, 1080,
                       [(5.0, 9.0, 4.0, "fast"), (20.0, 30.0, 20.0, "timelapse"), (40.0, 50.0, 2.0, "fast")],
                       offset=1.0, until=45.0)
    check("早送り: 画面の隅の表示（倍率・タイムラプス・試し書き出しの範囲）",
          "Style: Speedup," in ass.split("[Events]")[0] and "▶▶ ×4" in ass and "タイムラプス ×20" in ass
          and "0:00:06.00,0:00:10.00" in ass and "0:00:41.00,0:00:46.00" in ass, ass[-300:])


def t_long_material(check):
    """3時間・カット2000個・早送り150か所でも、すばやく変換できる"""
    import tracemalloc

    import rc_speedup as S
    from rc_media import MediaInfo, SourceSet
    from rc_timeline import Timeline
    files = [MediaInfo(Path(f"C{i:04d}.MP4"), 3600 * 0.75, Fraction(30000, 1001), 1920, 1080, 2, 48000)
             for i in range(4)]   # 45分×4本 = 3時間
    src = SourceSet(files)
    keep = [(t, t + 4.0) for t in [i * 5.4 for i in range(2000)]]
    speedups = [{"start": t + 0.5, "end": t + 3.5, "speed": 4.0, "style": "fast"}
                for t in [i * 5.4 for i in range(0, 2000, 13)]]
    tracemalloc.start()
    t0 = time.time()
    tl = Timeline(src, keep, src.main.fps)
    stl = S.SpeedTimeline(tl, speedups)
    probes = [i * 3.7 for i in range(3000)]
    mapped = [stl.map_time_nearest(t) for t in probes]
    _ = [stl.map_range(t, t + 2.0) for t in probes]
    dt = time.time() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    mono = all(b >= a - 1e-6 for a, b in zip(mapped, mapped[1:]))
    expect = tl.duration - sum(3.0 - 3.0 / 4 for _ in speedups)
    frames_ok = stl.total_frames == sum(s.frames for s in stl.segs)
    # 早送りの区間ごとに、フレーム単位の端数（1フレーム未満）だけずれてよい
    check("早送り: 3時間の素材でも速くて軽い", dt < 20 and peak < 200 * 1024 * 1024 and mono and frames_ok
          and abs(stl.duration - expect) < len(speedups) / float(src.main.fps) and len(stl.segs) >= len(tl.pieces),
          f"{dt:.1f}秒 / 最大{peak / 1e6:.0f}MB / 長さ {stl.duration:.1f}（期待 {expect:.1f}）")


# ---------------------------------------------------------------- 荒編集 → 仕上げ
def _craft(fx: Path, out: Path) -> Path | None:
    cd = fx / "speedup" / "craft"
    if cd.exists():
        shutil.rmtree(cd)
    cd.mkdir(parents=True)
    shutil.copy(out / "transcript.json", cd / "transcript.json")
    p = _run(["roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--mode", "craft", "--llm", "none",
              "--decisions", str(fx / "claude_cuts.json"), "--meta", str(fx / "claude_meta.json"),
              "--cam2", str(fx / "phone.mp4"), "--out", str(cd)])
    if p.returncode != 0:
        return None
    return cd


def _timelines(cd: Path, plan: dict, zooms_cfg=None):
    import rc_speedup as S
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    sess = json.loads((cd / "session.json").read_text(encoding="utf-8"))
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    return sess, tl, S.SpeedTimeline(tl, plan.get("speedups") or [], zooms_cfg or ())


def t_render(check, out: Path, fx: Path):
    import rc_speedup as S
    cd = _craft(fx, out)
    check("早送り: 工作モードの荒編集", cd is not None)
    if not cd:
        return
    sess = json.loads((cd / "session.json").read_text(encoding="utf-8"))
    sw = sess.get("silent_work", [])
    check("早送り: session.json に作業区間", len(sw) == 1 and abs(sw[0][0] - WORK[0]) < 0.01
          and abs(sw[0][1] - WORK[1]) < 0.01, str(sw))

    # plan.json には speedups がない → 自動で早送り
    # （2台目カメラへの切り替え（rc_multicam）は止めて、早送りだけを確かめる。組み合わせは test_integration）
    plan_in = cd / "plan_in.json"
    plan_in.write_text(json.dumps({**json.loads((fx / "plan.json").read_text(encoding="utf-8")), "angles": []},
                                  ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(cd), "--template", str(fx / "tpl"), "--plan", str(plan_in), "--llm", "none",
              "--keep-work"])
    if p.returncode != 0:
        check("早送り: 書き出し", False, _tail(p))
        return
    plan = json.loads((cd / "plan_used.json").read_text(encoding="utf-8"))
    sp = plan.get("speedups", [])
    check("早送り: プランに自動で入る", len(sp) == 1 and sp[0]["speed"] == 2.0 and sp[0]["work"] == 1
          and plan.get("speedups_auto") is False, str(sp))
    _sess, tl, stl = _timelines(cd, plan)
    mp4 = cd / "C0001_完成.mp4"
    d = _duration(mp4)
    saved = (WORK[1] - WORK[0]) / 2
    check("早送り: 作業の場面が短くなる", abs(d - (tl.duration - saved)) < 0.25 and abs(d - stl.duration) < 0.15,
          f"完成 {d:.2f}秒 / 早送りなし {tl.duration:.2f}秒 / 計算 {stl.duration:.2f}秒")
    (bs, be, k, kind), = stl.badge_ranges()
    vol_fast = _max_volume(cd / "work" / "body.mkv", bs + 0.3, be - bs - 0.6)
    vol_talk = _max_volume(cd / "work" / "body.mkv", bs - 3.0, 2.5)
    check("早送り: 作業の音は消える（BGMだけ）", vol_fast < -60 and vol_talk > -40, f"{vol_fast} / {vol_talk} dB")
    mix = _max_volume(cd / "work" / "mix.wav", bs + 0.3, be - bs - 0.6)
    check("早送り: BGMは流れる", mix > -40, f"{mix} dB")
    ass = (cd / "work" / "subs.ass").read_text(encoding="utf-8")
    check("早送り: 画面の隅に「×2」", "Speedup,,0,0,0,,{" in ass and "▶▶ ×2" in ass)

    # 字幕・チャプターが早送り後の時刻に合う
    import rc_chapters
    from rc_transcribe import Chunk
    chunks = {c["id"]: Chunk(**c) for c in json.loads((cd / "chunks.json").read_text(encoding="utf-8"))}
    srt = (cd / "C0001_完成.srt").read_text(encoding="utf-8-sig")
    want9 = stl.map_range(chunks[9].start, chunks[9].end)[0][0]
    m = re.search(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> .*\nはい、きれいに開きました", srt)
    got9 = int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000 if m else -1
    check("早送り: 作業のあとの字幕の時刻", abs(got9 - want9) < 0.01 and got9 < tl.map_time(chunks[9].start) - 4,
          f"{got9} / 期待 {want9:.3f}（早送りなしなら {tl.map_time(chunks[9].start):.3f}）")
    fi = json.loads((cd / "final_info.json").read_text(encoding="utf-8"))
    ok = all(abs(c["t"] - rc_chapters.chapter_time(stl, chunks[sc["id"]])) < 0.02
             for c, sc in zip(fi["chapters"][1:], sess["chapters"][1:]))
    check("早送り: チャプターも早送り後の時刻", ok and abs(fi["duration"] - d) < 0.2, str(fi["chapters"]))

    # 映像の中身：完成の時刻 t の絵が、計算した元素材の時刻の絵と同じ
    import numpy as np
    from rc_render import has_audio  # noqa: F401  (rc_render が読み込めることも確認)

    def frame(path, t):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                            "-vf", "scale=192:108", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
        return np.frombuffer(r.stdout, np.uint8).astype(np.float32)
    t_out = bs + (be - bs) * 0.6
    g = stl.parts[[i for i, pt in enumerate(stl.parts) if pt.sid is not None][-1]]
    t_src = g.g0 + (t_out - g.o0 / float(stl.fps)) * g.speed
    fi_list = stl.sources.split(t_src, t_src + 0.01)
    a = frame(cd / "work" / "body.mkv", t_out)
    b = frame(stl.sources.files[fi_list[0][0]].path, fi_list[0][1])
    wrong = frame(stl.sources.files[fi_list[0][0]].path, max(0.0, fi_list[0][1] - 2.0))
    diff, diff_wrong = float(np.abs(a - b).mean()), float(np.abs(a - wrong).mean())
    check("早送り: 映像の位置が計算と合う", a.size and diff < diff_wrong / 3, f"差 {diff:.1f}（2秒ずれた絵との差 {diff_wrong:.1f}）")

    # Premiere 用 XML
    xp = cd / "C0001_早送り入り.xml"
    try:
        root = ET.parse(xp).getroot()
        clips = [c for c in root.iter("clipitem") if c.get("id", "").startswith("clipitem-v")]
        fast = [c for c in clips if c.find("filter") is not None]
        speeds = {e.find("value").text for c in fast for e in c.iter("parameter") if e.find("parameterid").text == "speed"}
        seq_dur = int(root.find("sequence/duration").text)
        names = [m.find("name").text for m in root.iter("marker")]
        cam2 = [c for c in root.iter("clipitem") if c.get("id", "").startswith("clipitem-cam2")]
        cam2_fast = [c for c in cam2 if c.find("filter") is not None]
        ok = bool(fast) and speeds == {"200"} and abs(seq_dur / float(stl.fps) - stl.duration) < 0.1 \
            and "早送り ×2" in names and bool(cam2_fast) and all(
                int(c.find("end").text) - int(c.find("start").text) == int(c.find("out").text) - int(c.find("in").text)
                for c in fast)
        check("早送り: Premiere用XML（速度とマーカー）", ok, f"速度{speeds} 長さ{seq_dur} マーカー{names}")
    except Exception as e:  # noqa: BLE001
        check("早送り: Premiere用XML（速度とマーカー）", False, str(e))
    # audio = "fast"（作業の音を速くして小さく流す）とタイムラプスの倍率でも、FFmpeg で書き出せる
    import rc_render
    fast_tl = S.SpeedTimeline(tl, [{"start": 31.0, "end": 39.0, "speed": 4.0, "style": "fast"},
                                         {"start": 0.5, "end": 9.5, "speed": 20.0, "style": "timelapse"}],
                                    (), "fast", -18)
    segs = [s for s in fast_tl.segs if s.speed != 1]
    fw = cd / "fastaudio"
    fw.mkdir(exist_ok=True)
    _, inter, _ = rc_render.pick_encoder("libx264", 23)
    body = rc_render.render_body(segs, 640, 360, fast_tl.fps, fw, inter)
    want = sum(s.frames for s in segs) / float(fast_tl.fps)
    vol = _max_volume(body, 0.0, want)
    check("早送り: 速くした音（audio=\"fast\"）とタイムラプスも書き出せる",
          all(s.has_audio and s.gain_db == -18 for s in segs) and abs(_duration(body) - want) < 0.1 and vol > -60,
          f"{_duration(body):.2f}秒（期待 {want:.2f}）/ {vol} dB")

    p = _run(["speedup.py", str(cd), "--template", str(fx / "tpl")])
    check("早送り: speedup.py で一覧", p.returncode == 0 and "作業1" in p.stdout and "×2" in p.stdout, _tail(p, 4))

    # サムネイル候補：完成の時刻で均等に選ぶ（元素材の時刻は、残した場面の中）
    import rc_thumb
    times = rc_thumb.candidate_times(sess, S.wrap(tl, plan), {i: {"start": c.start, "end": c.end} for i, c in
                                                              chunks.items()}, {}, plan)
    check("早送り: サムネイル候補の時刻", bool(times) and all(tl.map_time(t) is not None for t, _ in times)
          and len(list((cd / "thumbnails").glob("thumb_*.jpg"))) == 9, str([round(t, 1) for t, _ in times]))
    return cd


def t_plan_variants(check, cd: Path, fx: Path):
    """AIの返答の形のプラン（倍率の変更・追加）で試し書き出し、手直しで全部外したプラン、テンプレートで止める"""
    v = fx / "speedup" / "variants"
    if v.exists():
        shutil.rmtree(v)
    shutil.copytree(cd, v, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "plan_*.json"))
    plan = json.loads((fx / "plan.json").read_text(encoding="utf-8"))
    plan["speedups"] = [{"work": 1, "start_id": 0, "end_id": 0, "speed": 4, "style": "fast"},
                        {"work": 0, "start_id": 12, "end_id": 12, "speed": 2, "style": "fast"}]
    (v / "claude_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(v), "--template", str(fx / "tpl"), "--plan", str(v / "claude_plan.json"),
              "--llm", "none", "--no-thumbs", "--keep-work"])
    if p.returncode != 0:
        check("早送り: AIの返答の形のプランで書き出し", False, _tail(p))
        return
    used = json.loads((v / "plan_used.json").read_text(encoding="utf-8"))
    sp = [(x["work"], x["speed"]) for x in used["speedups"]]
    _sess, _tl, stl = _timelines(v, used)
    d = _duration(v / "C0001_完成.mp4")
    ass = (v / "work" / "subs.ass").read_text(encoding="utf-8")
    check("早送り: AIの返答で倍率を変えて足す（足した所は声と字幕を消す）", sp == [(1, 4.0), (0, 2.0)]
          and abs(d - stl.duration) < 0.15 and "▶▶ ×4" in ass and "▶▶ ×2" in ass and "登録お願い" not in ass
          and "マキタのドリル" in ass, f"{sp} {d:.2f}秒 / 計算 {stl.duration:.2f}秒")

    # 試し書き出しで、早送りの途中で切れる（書き出したプランをそのまま使う）
    (bs, be, _k, _kind) = stl.badge_ranges()[0]
    cut_at = round((bs + be) / 2, 2)
    p = _run(["finish.py", str(v), "--template", str(fx / "tpl"), "--plan", str(v / "plan_used.json"),
              "--llm", "none", "--preview", str(cut_at), "--no-thumbs", "--keep-work"])
    d = _duration(v / "C0001_プレビュー.mp4") if p.returncode == 0 else 0
    ass = (v / "work" / "subs.ass").read_text(encoding="utf-8") if p.returncode == 0 else ""
    from rc_ass import _t
    check("早送り: 試し書き出しが早送りの途中で切れても大丈夫", abs(d - cut_at) < 0.1 and f",{_t(cut_at)},Speedup" in ass
          and "▶▶ ×2" not in ass, f"{d:.2f}秒（指定 {cut_at}）" + ("" if p.returncode == 0 else _tail(p)))

    used["speedups"] = []
    (v / "plan_none.json").write_text(json.dumps(used, ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(v), "--template", str(fx / "tpl"), "--plan", str(v / "plan_none.json"), "--plan-only"])
    none = json.loads((v / "plan_used.json").read_text(encoding="utf-8")).get("speedups") if p.returncode == 0 else None
    check("早送り: 全部外したプランなら早送りなし", none == [], str(none))

    tdir = fx / "speedup" / "tpl_off"
    if tdir.exists():
        shutil.rmtree(tdir)
    shutil.copytree(fx / "tpl", tdir)
    t = (tdir / "template.toml").read_text(encoding="utf-8")
    if "[speedup]" in t:
        t = t.replace("[speedup]\nenabled = true", "[speedup]\nenabled = false")
    else:   # 古いテスト素材のテンプレート（[speedup] を足す前にコピーしたもの）
        t += "\n[speedup]\nenabled = false\n"
    (tdir / "template.toml").write_text(t, encoding="utf-8")
    p = _run(["finish.py", str(v), "--template", str(tdir), "--plan", str(fx / "plan.json"), "--plan-only"])
    used = json.loads((v / "plan_used.json").read_text(encoding="utf-8")) if p.returncode == 0 else {"x": 1}
    check("早送り: テンプレートの enabled=false で自動の早送りをしない", "speedups" not in used, str(used.keys()))


# ---------------------------------------------------------------- 偽の Claude
def _sse(obj: dict) -> bytes:
    text = json.dumps(obj, ensure_ascii=False)
    ev = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text[:20]}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text[20:]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 300}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for n, d in ev).encode("utf-8")


def t_fake_claude(check, cd: Path, fx: Path):
    import anthropic
    import httpx2

    import finish
    import rc_llm
    a = fx / "speedup" / "ai"
    if a.exists():
        shutil.rmtree(a)
    shutil.copytree(cd, a, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "plan_*.json"))
    reqs = []
    answer = {"telops": [{"start_id": 9, "end_id": 9, "text": "開いた！", "style": "funny", "se": "none"}],
              "zooms": [], "bgm": [{"start_id": 1, "mood": "calm"}],
              "speedups": [{"work": 1, "start_id": 0, "end_id": 0, "speed": 6, "style": "fast"},
                           {"work": 0, "start_id": 11, "end_id": 11, "speed": 3, "style": "fast"}]}

    def handler(request):
        reqs.append(json.loads(request.content.decode("utf-8")))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(answer))

    orig = rc_llm._client
    rc_llm._client = lambda: (anthropic, anthropic.Anthropic(
        api_key="sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    try:
        finish.run(a, fx / "tpl", llm="api", plan_only=True)
        err = ""
    except BaseException as e:  # noqa: BLE001
        err = repr(e)
    finally:
        rc_llm._client = orig
    if err or not reqs:
        check("早送り: AIの演出プラン（偽のClaude）", False, err)
        return
    body = reqs[0]
    user = body["messages"][0]["content"]
    user = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    system = body["system"] if isinstance(body["system"], str) else json.dumps(body["system"], ensure_ascii=False)
    schema = body.get("output_config", {}).get("format", {}).get("schema", {})
    check("早送り: AIに作業の場面を見せる", "【作業W1 [" in user and "無言の作業 10秒" in user and "speedups" in system
          and "speedups" in schema.get("required", []), user[-400:])
    used = json.loads((a / "plan_used.json").read_text(encoding="utf-8"))
    sp = [(x["work"], x["speed"], x["start"]) for x in used["speedups"]]
    chunks = {c["id"]: c for c in json.loads((a / "chunks.json").read_text(encoding="utf-8"))}
    check("早送り: AIが倍率を変えて早送りを足す", sp == [(1, 6.0, WORK[0]), (0, 3.0, chunks[11]["start"])]
          and used["telops"][0]["text"] == "開いた！", str(sp))

    p = _run(["finish.py", str(a), "--template", str(fx / "tpl"), "--llm", "manual"])
    prompt = (a / "prompt_plan.md").read_text(encoding="utf-8") if (a / "prompt_plan.md").exists() else ""
    check("早送り: claude.ai 用のプロンプトにも作業の場面", p.returncode == 0 and "【作業W1" in prompt
          and '"speedups"' in prompt)


# ---------------------------------------------------------------- GUI
GUI_CODE = r'''
import os, sys, json, time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
QMessageBox.information = staticmethod(lambda *a, **k: None)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
out = Path(OUT)
rv = w.review
rv.load(out)
res = {"ext": "gui_ext_speedup" in w.extensions}
tab = rv.speedup_tab
res["tab_text"] = rv.plan_tabs.tabText(rv.plan_tabs.indexOf(tab))
res["rows"] = tab.table.rowCount()
res["len0"] = tab.table.item(0, 4).text()
cb = tab.table.cellWidget(0, 3)
cb.setCurrentIndex(cb.findText("×6"))
res["len1"] = tab.table.item(0, 4).text()
if rv.player:
    tab.table.cellClicked.emit(0, 1)
    res["rate_fast"] = rv.player.playbackRate()
    rv.play_range(1.0, 2.0)
    res["rate_back"] = rv.player.playbackRate()
else:
    res["rate_fast"], res["rate_back"] = 6.0, 1.0
plan = rv._collect_plan()
res["collect"] = plan["speedups"]
res["auto_flag"] = plan.get("speedups_auto")
tab.table.item(0, 0).setCheckState(Qt.Unchecked)
rv._save_plan_edits()
ed = json.loads((out / "plan_edited.json").read_text(encoding="utf-8"))
res["saved_on"] = ed["speedups"]
res["saved_off"] = ed["_off"]["speedups"]
res["telops_kept"] = len(ed["telops"])
# 開き直しても残る
w2 = gui.MainWindow(); w2.review.load(out)
t2 = w2.review.speedup_tab
res["reopen"] = [t2.table.item(0, 0).checkState() == Qt.Checked, t2.table.cellWidget(0, 3).currentText()]
w2.close()
tab.table.item(0, 0).setCheckState(Qt.Checked)
w.tabs.setCurrentWidget(rv); rv.tabs.setCurrentIndex(1); rv.plan_tabs.setCurrentWidget(tab)
for _ in range(10):
    app.processEvents(); time.sleep(0.02)
w.grab().save(str(out / "gui_speedup.png"))
calls = []
w.runner.start = lambda script, args, title, on_done=None, handle_error=None: calls.append((script, list(args))) or True
rv.speedup_xml_button.click()
res["xml_call"] = calls[-1] if calls else None
# レビューモードの結果を開くと、早送りの表とボタンは出さない
talk = Path(TALK)
if (talk / "session.json").exists():
    rv.load(talk)
    res["talk_hidden"] = (not rv.plan_tabs.isTabVisible(rv.plan_tabs.indexOf(tab))
                          and not rv.speedup_xml_button.isVisibleTo(rv))
    rv.load(out)
    res["craft_shown"] = rv.plan_tabs.isTabVisible(rv.plan_tabs.indexOf(tab)) and rv.speedup_xml_button.isVisibleTo(rv)
else:
    res["talk_hidden"] = res["craft_shown"] = True
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, cd: Path, fx: Path):
    gd = fx / "speedup" / "gui"
    if gd.exists():
        shutil.rmtree(gd)
    shutil.copytree(cd, gd, ignore=shutil.ignore_patterns("work"))
    (gd / "plan_edited.json").unlink(missing_ok=True)
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\nTALK = {str(fx / 'out')!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"))
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("早送り GUI: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("早送り GUI: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("早送り GUI: 「② 演出の確認」に早送りの表", r["ext"] and r["tab_text"] == "早送り（1）" and r["rows"] == 1,
          r["tab_text"])
    check("早送り GUI: 速さを変えると完成での長さが変わる", r["len0"] == "5.0秒" and r["len1"] == "1.7秒",
          f"{r['len0']} → {r['len1']}")
    check("早送り GUI: その速さで再生し、ほかの行はふつうの速さ", r["rate_fast"] == 6.0 and r["rate_back"] == 1.0,
          f"{r['rate_fast']} / {r['rate_back']}")
    check("早送り GUI: 表の内容をプランにする", r["auto_flag"] is False and len(r["collect"]) == 1
          and r["collect"][0]["speed"] == 6.0 and abs(r["collect"][0]["start"] - WORK[0]) < 0.01, str(r["collect"]))
    check("早送り GUI: 外した早送りを保存（ほかの演出も残る）", r["saved_on"] == [] and len(r["saved_off"]) == 1
          and r["telops_kept"] == 3, str(r["saved_off"]))
    check("早送り GUI: 開き直しても残る", r["reopen"] == [False, "×6"], str(r["reopen"]))
    xc = r["xml_call"] or ["", []]
    check("早送り GUI: 早送り入りXMLのボタン", xc[0] == "speedup.py" and "--xml" in xc[1], str(xc))
    check("早送り GUI: 早送りがない結果では表とボタンを出さない", r["talk_hidden"] and r["craft_shown"])


def t_talk_unchanged(check, out: Path):
    plan = out / "plan_used.json"
    if plan.exists():
        used = json.loads(plan.read_text(encoding="utf-8"))
        check("早送り: レビューモードのプランは今まで通り", "speedups" not in used and "speedups_auto" not in used)
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    check("早送り: レビューモードでは作業区間なし", sess.get("silent_work") == [])


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    (fx / "speedup").mkdir(exist_ok=True)
    t_units(check)
    t_long_material(check)
    if not (out / "transcript.json").exists() or not (out / "session.json").exists():
        check("早送り: テスト（荒編集の結果が必要）", False, "先に tests/smoke_test.py の荒編集が成功している必要があります")
        return
    t_talk_unchanged(check, out)
    cd = t_render(check, out, fx)
    if cd:
        t_plan_variants(check, cd, fx)
        t_fake_claude(check, cd, fx)
        t_gui(check, cd, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    if not (out / "session.json").exists():
        print("（先に tests/smoke_test.py を実行してください。荒編集の結果を使います）")
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
