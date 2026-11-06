"""2台カメラの自動切り替え（multicam）のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_multicam.py        # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py           # 通しテストの最後からも呼ばれます

確認すること:
  - 最短ショット長（カット後のタイムラインで3秒）を守る・2台目の映像がない所は使わない
  - 自動の選び方（長い無言の作業・「この部品」などの発言）
  - 書き出しで、選んだ区間の映像だけ2台目に差し替わる（音声は本編のまま）・縦長はぼかし背景
  - Premiere用XMLで、選んだ区間だけ V2 のクリップが有効になる（カットを変えても保たれる）
  - Claude に静止画と発言を見せて選ぶ（偽の通信）
  - 画面の「カメラ」の表で確認・変更でき、書き出すプランに入る
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=900)


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _fake_sources(durations, fps=Fraction(30)):
    from rc_media import MediaInfo, SourceSet
    return SourceSet([MediaInfo(Path(f"/x/{i}.mp4"), d, fps, 1920, 1080, 2, 48000) for i, d in enumerate(durations)])


def _segments(tl, shots):
    """カット後のタイムラインを (開始, 終了, 手元か) の並びにする"""
    import rc_multicam
    ed = rc_multicam.to_edit(tl, shots)
    out, t = [], 0.0
    for s, e in ed:
        if s > t + 1e-3:
            out.append((t, s, False))
        out.append((s, e, True))
        t = e
    if tl.duration > t + 1e-3:
        out.append((t, tl.duration, False))
    return out


# ---------------------------------------------------------------- 区間の計算
def t_units(check):
    import rc_multicam
    from rc_timeline import Timeline
    src = _fake_sources([60.0])
    cam2 = rc_multicam.Cam2(_fake_sources([50.0]), -5.0)   # 本編 5〜55秒に2台目の映像がある
    tl = Timeline(src, [(0, 20), (30, 60)], Fraction(30))   # 20〜30秒をカット → 50秒
    ms = 3.0

    # 1秒だけの指定 → 3秒に延ばす
    sh = rc_multicam.compute_shots(tl, [{"start": 10, "end": 11}], cam2, ms)
    ed = rc_multicam.to_edit(tl, sh)
    check("multicam: 短い指定は最短ショット長に延ばす", len(ed) == 1 and abs(ed[0][1] - ed[0][0] - 3.0) < 0.01, str(ed))

    # 間の正面が1秒しかない → 1つにまとめる
    sh = rc_multicam.compute_shots(tl, [{"start": 8, "end": 12}, {"start": 13, "end": 17}], cam2, ms)
    ed = rc_multicam.to_edit(tl, sh)
    check("multicam: 短い正面のはさまりはまとめる", ed == [(8.0, 17.0)], str(ed))

    # カットをまたぐ: 元の時刻では 18〜31秒（13秒）でも、カット後は 3秒
    sh = rc_multicam.compute_shots(tl, [{"start": 19, "end": 31}], cam2, ms)
    ed = rc_multicam.to_edit(tl, sh)
    check("multicam: 長さはカット後で測る", len(ed) == 1 and abs((ed[0][1] - ed[0][0]) - 3.0) < 0.01
          and all(not (20 < s < 30 or 20 < e < 30) for s, e in sh), f"{ed} {sh}")

    # 2台目の映像がない所（0〜5秒）は使わない
    sh = rc_multicam.compute_shots(tl, [{"start": 0, "end": 9}], cam2, ms)
    check("multicam: 2台目の映像がない所は使わない", sh and sh[0][0] >= 5.0 - 1e-6, str(sh))

    # 正面のまま（cam=main）の行や、逆向きの区間は無視
    sh = rc_multicam.compute_shots(tl, [{"start": 12, "end": 18, "cam": "main"}, {"start": 40, "end": 35}], cam2, ms)
    check("multicam: 正面の行・逆向きの区間は無視", sh == [], str(sh))

    # ランダムな指定でも、どのショットも3秒以上（全体が3秒未満の場合を除く）
    rng = random.Random(7)
    bad = []
    for k in range(300):
        keep, t = [], 0.0
        while t < 120:
            a = t + rng.uniform(0, 3)
            b = a + rng.uniform(0.3, 15)
            keep.append((a, min(b, 120)))
            t = b
        tl2 = Timeline(_fake_sources([120.0]), keep, Fraction(30000, 1001))
        c2 = rc_multicam.Cam2(_fake_sources([rng.uniform(40, 120)]), -rng.uniform(0, 40))
        ang = []
        for _ in range(rng.randint(1, 12)):
            s = rng.uniform(0, 120)
            ang.append({"start": s, "end": s + rng.uniform(0.2, 12)})
        sh = rc_multicam.compute_shots(tl2, ang, c2, ms)
        lo, hi = c2.cover
        if any(s < lo - 1e-6 or e > hi + 1e-6 for s, e in sh):
            bad.append((k, "範囲外"))
            continue
        segs = _segments(tl2, sh)
        if len(segs) > 1:
            short = [(s, e, c) for s, e, c in segs if e - s < ms - 0.02]
            # 2台目の映像がない所に挟まれた短い正面だけは、どうしても残ることがある
            short = [x for x in short if x[2] or rc_multicam._intersect([(x[0], x[1])],
                                                                          rc_multicam.to_edit(tl2, [c2.cover]))]
            if short:
                bad.append((k, short[:2]))
    check("multicam: ランダムな指定でも最短ショット長を守る", not bad, str(bad[:3]))


# ---------------------------------------------------------------- 書き出しの部品
def t_render_cmd(check):
    import rc_render
    a = rc_render.Seg(Path("/m/A.mp4"), 1.0, 30)
    b = rc_render.Seg(Path("/m/A.mp4"), 2.0, 30, video=Path("/m/phone.mp4"), video_start=5.0, fit="blur")
    c = rc_render.Seg(Path("/m/B.mp4"), 0.0, 30, 1.2)
    cmd = rc_render._batch_cmd([a, b, c], 1920, 1080, Fraction(30), Path("o.mkv"), [])
    fc = cmd[cmd.index("-filter_complex") + 1]
    inputs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
    A, PH, B = (str(Path(p)) for p in ("/m/A.mp4", "/m/phone.mp4", "/m/B.mp4"))   # Windows では「\m\A.mp4」になる
    ok = (inputs == [A, A, PH, B]
          and "[1:a]" in fc and "[2:v]" in fc and "[1:v]" not in fc and "[2:a]" not in fc
          and "[3:v]crop=" in fc and "[3:a]" in fc and "boxblur" in fc)
    check("multicam: 書き出しは映像だけ2台目・音声は本編", ok, fc[:300] if not ok else "")
    i = cmd.index(PH)
    check("multicam: 2台目の読み始めの位置", cmd[i - 5:i - 3] == ["-ss", "5.0000"], str(cmd[i - 5:i]))


# ---------------------------------------------------------------- 素材
def make_cam2_tc(fx: Path) -> Path:
    """時刻の数字が映る縦長の2台目（同期の確認用）"""
    p = fx / "multicam" / "phone_tc.mp4"
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=540x960:rate=30",
                    "-i", str(fx / "cam2.wav"), "-t", "50", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


def _frame(video: Path, t: float, path: Path) -> bool:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                        str(path)], capture_output=True)
    return p.returncode == 0 and path.exists()


def t_pipeline(check, fx: Path) -> Path | None:
    import numpy as np
    from PIL import Image
    import rc_multicam
    work = fx / "multicam" / "craft"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copy(fx / "transcript.json", work / "transcript.json")
    phone = make_cam2_tc(fx)
    p = _run(["roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--mode", "craft", "--llm", "none",
              "--decisions", str(fx / "claude_cuts.json"), "--cam2", str(phone), "--out", str(work)])
    if p.returncode != 0:
        check("multicam: 荒編集（工作・2台）", False, _tail(p))
        return None
    xml0 = (work / "C0001_荒編集.xml").read_text(encoding="utf-8")
    check("multicam: プランがない時のXMLは今まで通り（V2は非表示）",
          xml0.count("<enabled>FALSE</enabled>") == 1 and "Basic Motion" not in xml0)

    # 演出プラン → 自動で手元カメラの場面が入る
    # 工作の作業シーンの早送り（rc_speedup）は止めて、カメラの切り替えだけを確かめる（組み合わせは test_integration）
    plan_in = work / "plan_in.json"
    plan_in.write_text(json.dumps({**json.loads((fx / "plan.json").read_text(encoding="utf-8")),
                                   "speedups": [], "speedups_auto": False}, ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(work), "--template", str(fx / "tpl"), "--plan", str(plan_in),
              "--llm", "none", "--plan-only"])
    if p.returncode != 0:
        check("multicam: 演出プラン（2台）", False, _tail(p))
        return None
    plan = json.loads((work / "plan_used.json").read_text(encoding="utf-8"))
    ang = plan.get("angles", [])
    check("multicam: 長い無言の作業を手元カメラに", len(ang) == 1 and abs(ang[0]["start"] - 29.5) < 0.05
          and abs(ang[0]["end"] - 40.0) < 0.05 and "無言" in ang[0]["why"], str(ang))
    check("multicam: テロップなどはそのまま", len(plan["telops"]) == 3 and len(plan["bgm"]) == 2)

    # XML: 選んだ所だけ V2 が有効
    root = ET.parse(work / "C0001_荒編集.xml").getroot()
    tracks = root.findall("./sequence/media/video/track")
    v2 = tracks[1].findall("clipitem") if len(tracks) > 1 else []
    on = [(int(c.find("start").text), int(c.find("end").text)) for c in v2 if c.find("enabled").text == "TRUE"]
    fps = 30000 / 1001
    total_on = sum(e - s for s, e in on) / fps
    first_on = on[0][0] / fps if on else -1
    check("multicam: XMLで選んだ区間だけV2が有効", len(tracks) == 2 and tracks[1].find("enabled").text == "TRUE"
          and abs(total_on - 10.5) < 0.1 and any(c.find("enabled").text == "FALSE" for c in v2),
          f"有効 {total_on:.2f}秒 / 先頭 {first_on:.2f}秒")
    scale = [c.find(".//parameter/value").text for c in v2 if c.find(".//parameter") is not None]
    check("multicam: XMLで縦長の2台目を画面に収める", scale and all(abs(float(s) - 112.5) < 0.01 for s in scale),
          str(scale[:2]))

    # 書き出し
    p = _run(["finish.py", str(work), "--template", str(fx / "tpl"), "--plan", str(work / "plan_used.json"),
              "--llm", "none", "--no-thumbs", "--keep-work"])
    if p.returncode != 0:
        check("multicam: 書き出し（2台）", False, _tail(p))
        return None
    mp4 = work / "C0001_完成.mp4"
    # 手元カメラの区間（カット後 約13.8〜24.3秒）の真ん中と、その前の正面
    ok1 = _frame(mp4, 18.0, work / "frame_cam2.png")
    ok2 = _frame(mp4, 11.0, work / "frame_main.png")
    if ok1 and ok2:
        a = np.asarray(Image.open(work / "frame_cam2.png").convert("L"), dtype=np.float32)
        b = np.asarray(Image.open(work / "frame_main.png").convert("L"), dtype=np.float32)
        # 左右の端：ぼかし背景はなめらか（となりの画素との差が小さい）で、黒で埋めたのでもない
        edge = a[:, :300]
        smooth = float(np.abs(np.diff(edge, axis=1)).mean())
        check("multicam: 縦長の2台目はぼかし背景で埋める", edge.mean() > 15 and smooth < 2.0,
              f"明るさ {edge.mean():.1f} / ざらつき {smooth:.2f}")
        check("multicam: 区間の外は正面のまま", float(np.abs(a - b).mean()) > 20)
    else:
        check("multicam: 完成動画から静止画を取り出す", False)

    # 音声は本編のまま：作業フォルダの body の音声と、本編の音声がそろっている
    body = work / "work" / "body.mkv"

    def env(path: Path, ss: float, dur: float):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                              "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        n = len(x) // 80
        return np.sqrt((x[: n * 80].reshape(n, 80) ** 2).mean(axis=1) + 1)
    # カット後 15〜23秒 は、元の時刻 30.69〜38.69秒（工作モードの残す区間から）
    sess = json.loads((work / "session.json").read_text(encoding="utf-8"))
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    src = SourceSet([probe(Path(x)) for x in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    g = rc_multicam.to_source(tl, [(15.0, 23.0)])
    if body.exists() and g:
        fi = 1 if g[0][0] >= src.offsets[1] else 0
        x = env(body, 15.0, g[0][1] - g[0][0])
        y = env(src.files[fi].path, g[0][0] - src.offsets[fi], g[0][1] - g[0][0])
        n = min(len(x), len(y))
        cc = float(np.corrcoef(np.log(x[:n]), np.log(y[:n]))[0, 1]) if n > 10 else 0.0
        check("multicam: 手元カメラの所も音声は本編のまま", cc > 0.95, f"相関 {cc:.3f}")
    else:
        check("multicam: 作業フォルダの本編", False, str(body))

    # 試し書き出し（--preview）でも差し替わる
    p = _run(["finish.py", str(work), "--template", str(fx / "tpl"), "--plan", str(work / "plan_used.json"),
              "--llm", "none", "--no-thumbs", "--preview", "20"])
    pv = work / "C0001_プレビュー.mp4"
    ok = p.returncode == 0 and _frame(pv, 18.0, work / "frame_preview.png")
    if ok:
        a = np.asarray(Image.open(work / "frame_preview.png").convert("L"), dtype=np.float32)
        ok = float(np.abs(np.diff(a[:, :300], axis=1)).mean()) < 2.0
    check("multicam: 試し書き出しでも手元カメラに切り替わる", ok, "" if p.returncode == 0 else _tail(p))
    shutil.rmtree(work / "work", ignore_errors=True)

    # カットの変更を反映（roughcut を作り直す）しても、XMLの V2 の選択が残る
    (work / "plan_edited.json").write_text(json.dumps(dict(plan, angles=ang), ensure_ascii=False), encoding="utf-8")
    p = _run(["roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--mode", "craft", "--llm", "none",
              "--decisions", str(fx / "claude_cuts.json"), "--cam2", str(phone), "--out", str(work)])
    xml1 = (work / "C0001_荒編集.xml").read_text(encoding="utf-8") if p.returncode == 0 else ""
    root = ET.fromstring(xml1.split("\n", 2)[2]) if xml1 else None
    on2 = [c for c in root.findall("./sequence/media/video/track")[1].findall("clipitem")
           if c.find("enabled").text == "TRUE"] if root is not None else []
    check("multicam: カットを変えて作り直しても V2 の選択が残る", len(on2) >= 1, _tail(p) if p.returncode else "")
    (work / "plan_edited.json").unlink(missing_ok=True)
    return work


def t_rules(check, work: Path):
    """「この部品」などの発言・同期の信頼度・自動をやめる設定"""
    import rc_multicam
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    from rc_transcribe import Chunk
    sess = json.loads((work / "session.json").read_text(encoding="utf-8"))
    chunks = [Chunk(**c) for c in json.loads((work / "chunks.json").read_text(encoding="utf-8"))]
    for c in chunks:
        if c.id == 2:
            c.text = "今日はこの部品を付けます。"   # 3.4〜6.0秒（2.6秒）
    src = SourceSet([probe(Path(x)) for x in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    cam2 = rc_multicam.load_cam2(work, sess)
    cfg = rc_multicam.settings()
    ang = rc_multicam.rule_angles(tl, chunks, sess, cam2, cfg)
    hit = [a for a in ang if "この部品" in a["why"]]
    ed = rc_multicam.to_edit(tl, rc_multicam.compute_shots(tl, hit, cam2, 3.0))
    check("multicam: 「この部品」の発言を手元カメラに（3秒以上）", len(hit) == 1 and hit[0]["start"] >= 3.5 - 1e-6
          and ed and ed[0][1] - ed[0][0] >= 3.0 - 0.02, f"{hit} {ed}")

    plan = {"telops": [], "zooms": [], "bgm": []}

    class Tpl:
        def __init__(self, d):
            self.d = d

        def get(self, k, default=None):
            return self.d.get(k, default)
    sync = json.loads((work / "sync.json").read_text(encoding="utf-8"))
    (work / "sync.json").write_text(json.dumps(dict(sync, confidence=3.0)), encoding="utf-8")
    try:
        mc = rc_multicam.prepare(work, sess, tl, chunks, plan, Tpl({}), "none", "low", {"in": 0, "out": 0})
        check("multicam: 同期の信頼度が低い時は自動で切り替えない", plan["angles"] == [] and mc.shots == [])
    finally:
        (work / "sync.json").write_text(json.dumps(sync), encoding="utf-8")
    plan = {"telops": [], "zooms": [], "bgm": []}
    rc_multicam.prepare(work, sess, tl, chunks, plan, Tpl({"multicam": {"auto": False}}), "none", "low",
                        {"in": 0, "out": 0})
    check("multicam: auto = false なら自動で切り替えない", plan["angles"] == [])
    plan = {"telops": [], "zooms": [], "bgm": [], "angles": [{"start": 1, "end": 2, "cam": "cam2", "why": "手動"}]}
    rc_multicam.prepare(work, sess, tl, chunks, plan, Tpl({}), "none", "low", {"in": 0, "out": 0})
    check("multicam: 手で選んだ区間はそのまま使う", plan["angles"] == [{"start": 1, "end": 2, "cam": "cam2",
                                                                    "why": "手動"}])
    import rc_plan
    p2 = rc_plan.sanitize({"telops": [], "zooms": [], "bgm": [], "angles": [{"start": 1, "end": 5}],
                           "_off": {"angles": []}}, {1}, [])
    check("multicam: 演出プランの整理で angles を消さない", p2.get("angles") == [{"start": 1, "end": 5}]
          and "_off" not in p2)
    # 2台目がない素材では何もしない
    plan = {"telops": []}
    mc = rc_multicam.prepare(work, dict(sess, args={}), tl, chunks, plan, Tpl({}), "none", "low", {"in": 0, "out": 0})
    check("multicam: 2台目がなければ何もしない", mc is None and "angles" not in plan)
    # 書き出しの素材リストで、2台目のファイル内の位置が合っている（2台目の時刻 = 本編 - 3.5秒）
    import rc_render
    segs = [rc_render.Seg(src.files[0].path, 25.0, 300), rc_render.Seg(src.files[1].path, 5.0, 300)]
    mc = rc_multicam.Multicam(cam2, [(29.5, 40.0)])
    out = rc_multicam.apply_segs(segs, src, Fraction(30), mc, 1920, 1080)
    alt = [s for s in out if s.video]
    ok = (len(out) == 4 and len(alt) == 2 and abs(alt[0].video_start - 26.0) < 0.05
          and abs(alt[1].video_start - (35.0 - 3.5)) < 0.05 and alt[0].fit == "blur"
          and alt[0].path == src.files[0].path and sum(s.frames for s in out) == 600)
    check("multicam: 2台目の映像の位置（同期のずれ）", ok, str([(s.start, s.frames, s.video_start) for s in out]))


# ---------------------------------------------------------------- Claude（偽の通信）
def _sse(obj: dict) -> bytes:
    text = json.dumps(obj, ensure_ascii=False)
    ev = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 80}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for n, d in ev).encode("utf-8")


AI_CODE = r'''
import json, os, sys
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ["ANTHROPIC_API_KEY"] = "test-key"
import anthropic, httpx2, rc_llm
from test_multicam import _sse
seen = []
def handler(req):
    body = json.loads(req.content)
    content = body["messages"][0]["content"]
    texts = [c["text"] for c in content if c["type"] == "text"]
    imgs = [c for c in content if c["type"] == "image"]
    scenes = [t for t in texts if t.startswith("場面") and "発言" in t]
    seen.append({"n_img": len(imgs), "scenes": scenes, "system": body.get("system", ""),
                 "schema": "json_schema" in json.dumps(body.get("output_config", {}))})
    # 「実際に穴を開けて」の場面を手元カメラにする
    nos = [int(t[2:t.index(" ")]) for t in scenes if "穴を開けて" in t]
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           content=_sse({"scenes": [{"no": n, "cam": "cam2", "why": "穴あけの手元"} for n in nos]}))
rc_llm._client = lambda: (anthropic, anthropic.Anthropic(api_key="test-key", http_client=httpx2.Client(
    transport=httpx2.MockTransport(handler))))
import multicam
sys.argv = ["multicam.py", OUT, "--llm", "api", "--template", TPL]
multicam.main()
print("RESULT " + json.dumps(seen, ensure_ascii=False))
'''


def t_ai(check, work: Path, fx: Path):
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(work)!r}\nTPL = {str(fx / 'tpl')!r}\n" + AI_CODE
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=300)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if p.returncode != 0 or not line:
        check("multicam: AIでカメラを選ぶ（偽の通信）", False, _tail(p))
        return
    seen = json.loads(line[7:])
    req = seen[0] if seen else {}
    check("multicam: AIに両カメラの静止画と発言を送る", len(seen) == 1 and req["n_img"] == 2 * len(req["scenes"])
          and req["n_img"] >= 4 and req["schema"] and "手元" in req["system"],
          f"画像{req.get('n_img')}枚 / 場面{len(req.get('scenes', []))}")
    plan = json.loads((work / "plan_used.json").read_text(encoding="utf-8"))
    ai = [a for a in plan.get("angles", []) if a["why"].startswith("AI")]
    check("multicam: AIの答えを演出プランに入れる", len(ai) == 1 and ai[0]["start"] <= 26.0 + 0.5
          and "穴あけ" in ai[0]["why"], str(plan.get("angles")))
    check("multicam: AIの料金を表示", "Claude API" in p.stdout)


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import os, sys, json, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
app = QApplication([])
w = gui.MainWindow(); w.resize(1300, 820); w.show()
rv = w.review
res = {"ext": "gui_ext_multicam" in w.extensions}
rv.load(Path(OUT))
p = rv.angle_panel
idx = rv.plan_tabs.indexOf(p)
res["visible"] = rv.plan_tabs.isTabVisible(idx)
res["tab"] = rv.plan_tabs.tabText(idx)
res["rows"] = p.table.rowCount()
w.tabs.setCurrentWidget(rv); rv.tabs.setCurrentIndex(1); rv.plan_tabs.setCurrentWidget(p)
p.table.selectRow(0)
p._show_row(0, play=False)
res["img"] = p.img_cam2.pixmap() is not None and not p.img_cam2.pixmap().isNull()
for _ in range(10):
    app.processEvents()
w.grab().save(PNG)
# 時刻を直す・行を足す・チェックを外す
p.table.item(0, 2).setText("0:41.5")
res["len_text"] = p.table.item(0, 3).text()
p.table.item(0, 1).setText("あいう")   # 読めない書き方は元に戻す
res["bad_text"] = p.table.item(0, 1).text()
p.table.clearSelection()
p.add_row()
p.table.item(1, 1).setText("0:05")
p.table.item(1, 2).setText("0:09")
p.add_row()
p.table.item(2, 0).setCheckState(Qt.Unchecked)
calls = []
w.runner.start = lambda script, args, title, on_done=None, handle_error=None: calls.append((script, list(args))) or True
rv.render(preview=True)
ed = json.loads((Path(OUT) / "plan_edited.json").read_text(encoding="utf-8"))
res["angles"] = ed.get("angles")
res["off"] = ed.get("_off", {}).get("angles")
res["render"] = [s for s, a in calls]
# 開き直しても残る
w2 = gui.MainWindow(); w2.review.load(Path(OUT))
res["reopen_rows"] = w2.review.angle_panel.table.rowCount()
res["reopen_checked"] = [w2.review.angle_panel.table.item(r, 0).checkState() == Qt.Checked
                         for r in range(w2.review.angle_panel.table.rowCount())]
# 自動で選び直す → multicam.py を呼ぶ
calls.clear()
p.redo(False)
res["redo"] = calls[-1] if calls else None
# 2台目がない結果では「カメラ」タブを出さない
rv.load(Path(OUT2))
res["hidden_without_cam2"] = not rv.plan_tabs.isTabVisible(rv.plan_tabs.indexOf(p))
res["no_key_without_cam2"] = "angles" not in rv._collect_plan()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, work: Path, fx: Path, out: Path | None):
    gd = fx / "multicam" / "gui"
    if gd.exists():
        shutil.rmtree(gd)
    shutil.copytree(work, gd, ignore=shutil.ignore_patterns("work", "*.mp4", "*.png"))
    (gd / "plan_edited.json").unlink(missing_ok=True)
    # 2台目がない結果（比較用）
    nd = fx / "multicam" / "gui_nocam2"
    if nd.exists():
        shutil.rmtree(nd)
    shutil.copytree(gd, nd)
    s = json.loads((nd / "session.json").read_text(encoding="utf-8"))
    s["args"]["cam2"] = []
    (nd / "session.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    p = json.loads((nd / "plan_used.json").read_text(encoding="utf-8"))
    p.pop("angles", None)
    (nd / "plan_used.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    png = (out or gd) / "gui_multicam.png"
    code = (f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\nOUT2 = {str(nd)!r}\nPNG = {str(png)!r}\n" + GUI_CODE)
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("multicam: 画面", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("multicam: 画面", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("multicam: 画面に「カメラ」の表", r["ext"] and r["visible"] and r["rows"] == 1 and r["tab"] == "カメラ（1）",
          f"{r['tab']} {r['rows']}行")
    check("multicam: 画面に両カメラの静止画", r["img"])
    check("multicam: 画面で時刻を直せる", r["len_text"] == "12.0秒" and r["bad_text"] == "00:29.5",
          f"{r['len_text']} {r['bad_text']}")
    ang = r["angles"] or []
    check("multicam: 画面の変更が書き出すプランに入る",
          [(a["start"], a["end"]) for a in ang] == [(5.0, 9.0), (29.5, 41.5)] and len(r["off"] or []) == 1
          and r["render"] == ["finish.py"], f"{ang} / off {r['off']}")
    check("multicam: 開き直しても画面の変更が残る", r["reopen_rows"] == 3 and r["reopen_checked"].count(False) == 1)
    redo = r["redo"] or ["", []]
    check("multicam: 「自動で選び直す」で multicam.py", redo[0] == "multicam.py" and "none" in redo[1], str(redo))
    check("multicam: 2台目がない時は「カメラ」の表を出さない", r["hidden_without_cam2"] and r["no_key_without_cam2"])


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path | None, fx: Path):
    (fx / "multicam").mkdir(exist_ok=True)
    t_units(check)
    t_render_cmd(check)
    work = t_pipeline(check, fx)
    if not work:
        return
    t_rules(check, work)
    t_gui(check, work, fx, out)
    t_ai(check, work, fx)   # plan_used.json の angles を書き換えるので最後に


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "multicam", fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
