"""MVモード（mv.py / rc_mv.py / gui_ext_mv.py）のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_mv.py            # 単独で実行
    python tests/smoke_test.py         # 通しテストの最後からも呼ばれます

確認すること:
  - 合成した曲（テンポ・拍・サビが分かっている）で、テンポ・拍・小節の頭・サビを当てられる（速い曲・遅い曲も）
  - カットの境目が拍の位置にある。サビは細かく、イントロはゆったり。サビ前の1小節はたたみかける
  - 日本語・スペースを含むパスで、MVラフの mp4 と Premiere用XML（拍のマーカー・素材の配置）ができる
  - 暗すぎる素材は使わない。盛り上がる所ほど動きの多い素材を使う。音声なし・縦長の素材も使える
  - 撮った順・サビだけ・テンポの手動指定
  - AIで並べる（Claudeの通信は偽物）: 代表フレームを送り、返事の順番どおりに並ぶ。失敗したら自動で並べる
  - 長い素材でもメモリをほとんど使わない（キーフレームだけ読む）
  - GUIの「MV」タブ
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mv_fixtures  # noqa: E402

PY = sys.executable
SHORT = [("intro", 4), ("verse", 4), ("chorus", 4), ("verse", 4), ("chorus", 4), ("outro", 2)]


def _run(args: list[str], env: dict | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=timeout)


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _probe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                         capture_output=True, text=True).stdout
    return json.loads(out or "{}")


def make_fixtures(fx: Path) -> dict:
    d = fx / "mv"
    stamp = d / "done"
    if stamp.exists() and stamp.read_text(encoding="utf-8") == "v3":
        return json.loads((d / "truth.json").read_text(encoding="utf-8"))
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    truth = {}
    for bpm in (128, 92, 170):
        truth[str(bpm)] = mv_fixtures.make_song(d / f"song{bpm}.wav", bpm=bpm)
    truth["short"] = mv_fixtures.make_song(d / "曲 テスト.wav", bpm=128, struct=SHORT)
    # 生演奏のような曲（テンポが少しずつ速くなる・1音ごとのずれ・雑音）
    truth["live"] = mv_fixtures.make_song(d / "live.wav", bpm=124, drift=0.04, jitter=0.012, noise=0.05, seed=5)
    vids = mv_fixtures.make_videos(d / "素材 フォルダ")
    truth["videos"] = {k: str(v) for k, v in vids.items()}
    (d / "truth.json").write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")
    stamp.write_text("v3", encoding="utf-8")
    return truth


# ---------------------------------------------------------------- 曲の解析
def _overlap(a: list, b: list) -> float:
    """区間の集まり a と b が、時間でどれだけ重なるか（IoU）"""
    t = np.arange(0, max([e for _, e in a + b] + [1]), 0.05)
    ma = np.zeros(len(t), bool)
    mb = np.zeros(len(t), bool)
    for s, e in a:
        ma |= (t >= s) & (t < e)
    for s, e in b:
        mb |= (t >= s) & (t < e)
    return float((ma & mb).sum() / max((ma | mb).sum(), 1))


def t_song(check, d: Path, truth: dict):
    import rc_mv
    for bpm in ("128", "92", "170", "live"):
        tr = truth[bpm]
        lab = f"{bpm} BPM" if bpm.isdigit() else "生演奏のゆれ・雑音あり"
        an = rc_mv.analyze_song(d / (f"song{bpm}.wav" if bpm != "live" else "live.wav"))
        tb, db = np.array(tr["beats"]), np.array(an["beats"])
        hit = float(np.mean([np.abs(db - x).min() < 0.07 for x in tb]))
        dhit = float(np.mean([np.abs(np.array(an["downbeats"]) - x).min() < 0.07 for x in tr["downbeats"]]))
        ch = [(s["start"], s["end"]) for s in an["sections"] if s["kind"] == "chorus"]
        iou = _overlap(ch, tr["choruses"])
        if bpm != "live":
            check(f"MV: テンポ（{bpm} BPM）", abs(an["bpm"] - tr["bpm"]) / tr["bpm"] < 0.015, f"{an['bpm']}")
        check(f"MV: 拍の位置（{lab}）", hit >= 0.95, f"一致 {hit:.0%}")
        check(f"MV: 小節の頭（{lab}）", dhit >= 0.9, f"一致 {dhit:.0%}")
        check(f"MV: サビの検出（{lab}）", iou >= 0.8, f"重なり {iou:.0%} {ch}")
        if bpm == "128":
            bc = an["best_chorus"]
            check("MV: 一番の盛り上がりはサビ", any(abs(bc["start"] - s) < 0.1 for s, _ in tr["choruses"]), str(bc))
            kinds = [s["kind"] for s in an["sections"]]
            check("MV: 区間の名前（イントロ・アウトロ）", kinds[0] == "intro" and kinds[-1] == "outro", str(kinds))
            an2 = rc_mv.analyze_song(d / f"song{bpm}.wav", bpm=128.0)
            check("MV: テンポの手動指定", abs(an2["bpm"] - 128) < 0.5, f"{an2['bpm']}")
    return None


def t_plan(check, d: Path):
    import rc_mv
    an = rc_mv.analyze_song(d / "song128.wav")
    fps = Fraction(30000, 1001)
    a, b = 0.0, an["duration"]
    cuts = rc_mv.plan_cuts(an, a, b, "normal", fps)
    beats = np.array(an["beats"])
    inner = [c.t0 for c in cuts[1:]]
    on_beat = all(np.abs(beats - t).min() < 1e-3 for t in inner)
    check("MV: カットの境目はすべて拍の位置", on_beat, f"{len(cuts)}カット")
    cont = all(x.f1 == y.f0 for x, y in zip(cuts, cuts[1:])) and cuts[0].f0 == 0 \
        and cuts[-1].f1 == int(round(b * fps))
    check("MV: カットがすき間なく続く（フレーム単位）", cont)
    ch = [c.beats for c in cuts if c.kind == "chorus"]
    intro = [c.beats for c in cuts if c.kind == "intro" and c.beats]
    check("MV: サビは2拍ごと・イントロは2小節ごと", ch and max(ch) <= 2 and intro and min(intro) == 8,
          f"サビ{set(ch)} イントロ{set(intro)}")
    # サビ直前の1小節は1拍ずつ
    first_ch = next(i for i, c in enumerate(cuts) if c.kind == "chorus")
    check("MV: サビ直前の1小節はたたみかける", [c.beats for c in cuts[first_ch - 4:first_ch]] == [1, 1, 1, 1],
          str([c.beats for c in cuts[first_ch - 6:first_ch]]))
    fast = rc_mv.plan_cuts(an, a, b, "fast", fps)
    slow = rc_mv.plan_cuts(an, a, b, "slow", fps)
    check("MV: カットの速さ（細かめ＞ふつう＞ゆったり）", len(fast) > len(cuts) > len(slow),
          f"{len(fast)} / {len(cuts)} / {len(slow)}")
    sa, sb = rc_mv.song_range(an, "sabi", None, None)
    check("MV: サビだけの範囲（1小節前から）", abs(sb - an["best_chorus"]["end"]) < 0.01
          and 1.5 < an["best_chorus"]["start"] - sa < 2.2, f"{sa}〜{sb}")


# ---------------------------------------------------------------- CLI（おまかせ）
def _scene_times(mp4: Path) -> list[float]:
    p = subprocess.run(["ffmpeg", "-v", "info", "-nostdin", "-i", str(mp4), "-vf", "select='gt(scene,0.25)',showinfo",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [float(x) for x in re.findall(r"pts_time:\s*([0-9.]+)", p.stderr)]


def t_cli_auto(check, d: Path, truth: dict, shots: Path):
    out = d / "出力 auto"
    shutil.rmtree(out, ignore_errors=True)
    p = _run(["mv.py", str(d / "曲 テスト.wav"), str(d / "素材 フォルダ"), "--size", "640x360", "--out", str(out)])
    check("MV: おまかせでMVラフを作る（mv.py）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    if p.returncode != 0:
        return None
    plan = json.loads((out / "mv_plan.json").read_text(encoding="utf-8"))
    an = json.loads((out / "mv_song.json").read_text(encoding="utf-8"))
    mp4, xml = Path(plan["video"]), Path(plan["xml"])
    info = _probe(mp4)
    streams = {s["codec_type"]: s for s in info.get("streams", [])}
    dur = float(info.get("format", {}).get("duration", 0))
    check("MV: mp4 の長さ＝曲の長さ・映像と音声", abs(dur - an["duration"]) < 0.15 and "audio" in streams
          and streams.get("video", {}).get("width") == 640, f"{dur:.2f}s / 曲 {an['duration']:.2f}s")
    # XML
    try:
        root = ET.parse(xml).getroot()
    except Exception as e:  # noqa: BLE001
        check("MV: Premiere用XMLが正しいXML", False, str(e))
        return None
    seq = root.find("sequence")
    clips = seq.findall("./media/video/track/clipitem")
    st = [int(c.find("start").text) for c in clips]
    en = [int(c.find("end").text) for c in clips]
    check("MV: XMLに素材が並んでいる（すき間なし）", len(clips) == len(plan["cuts"]) and st[0] == 0
          and all(e == s for e, s in zip(en, st[1:])) and en[-1] == int(seq.find("duration").text),
          f"{len(clips)}クリップ")
    marks = seq.findall("marker")
    nbeats = sum(1 for t in an["beats"] if t < an["duration"])
    names = [m.find("name").text for m in marks]
    check("MV: XMLに拍のマーカー", len(marks) >= nbeats and any("サビ" in n for n in names)
          and any(n.endswith("小節") for n in names), f"マーカー{len(marks)}個 / 拍{nbeats}個")
    fps = Fraction(plan["fps"])
    mk_frames = {int(m.find("in").text) for m in marks}
    beat_frames = {int(round(t * fps)) for t in an["beats"]}
    check("MV: マーカーの位置＝拍の位置", beat_frames <= mk_frames | {0})
    a_clips = seq.findall("./media/audio/track/clipitem")
    check("MV: XMLに曲の音声", len(a_clips) == 2 and a_clips[0].find("./file/name").text == "曲 テスト.wav"
          and "%20" in a_clips[0].find("./file/pathurl").text)
    files = {f.get("id"): f for f in root.iter("file") if f.find("name") is not None}
    phone = [f for f in files.values() if f.find("name").text == "スマホ 縦.mp4"]
    check("MV: 音声なし・縦長の素材もXMLに入る", bool(phone) and phone[0].find("./media/audio") is None
          and phone[0].find("./media/video/samplecharacteristics/height").text == "640")
    # 素材の選び方
    names_by = [Path(plan["files"][c["file"]]).name for c in plan["cuts"]]
    check("MV: 暗すぎる素材は使わない", "暗い.mp4" not in names_by, str(sorted(set(names_by))))
    ch = [Path(plan["files"][c["file"]]).name for c in plan["cuts"] if c["kind"] == "chorus"]
    quiet = [Path(plan["files"][c["file"]]).name for c in plan["cuts"] if c["kind"] in ("intro", "outro")]
    r_ch = ch.count("静か.mp4") / max(len(ch), 1)
    r_q = quiet.count("静か.mp4") / max(len(quiet), 1)
    check("MV: 静かな所ほど動きの少ない素材", r_q > r_ch, f"静かな素材の割合 イントロ等{r_q:.0%} サビ{r_ch:.0%}")
    # 動画の中の切り替わりが、拍の位置にある
    sc = _scene_times(mp4)
    bounds = [c["t0"] for c in plan["cuts"][1:]]
    near = [min(abs(t - b) for b in bounds) for t in sc]
    ok = sc and np.mean([x < 1.5 / float(fps) for x in near]) >= 0.9
    check("MV: 動画の絵の切り替わり＝拍の位置", bool(ok), f"切り替わり{len(sc)}か所 ずれ最大{max(near or [0]):.3f}s")
    # 目で確かめる用の一覧画像
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-i", str(mp4), "-vf",
                    "fps=2,scale=160:-1,tile=8x6", "-frames:v", "1", str(shots / "mv_contact.png")])
    # 2回目は解析結果を使い回す
    p2 = _run(["mv.py", str(d / "曲 テスト.wav"), str(d / "素材 フォルダ"), "--size", "640x360", "--out", str(out),
               "--no-render", "--seed", "2"])
    check("MV: 2回目は解析を使い回す", p2.returncode == 0 and "前回の解析結果を使います" in p2.stdout, _tail(p2, 5))
    return out


def t_cli_variants(check, d: Path, truth: dict):
    out = d / "出力 files"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    if (d / "出力 auto" / "mv_clips.json").exists():   # 素材の解析結果を使い回して速くする
        shutil.copy(d / "出力 auto" / "mv_clips.json", out / "mv_clips.json")
    p = _run(["mv.py", str(d / "曲 テスト.wav"), str(d / "素材 フォルダ" / "静か.mp4"),
              str(d / "素材 フォルダ" / "動き 多め.mp4"), "--order", "files", "--no-render", "--out", str(out)])
    check("MV: 撮った順（--order files）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    if p.returncode == 0:
        plan = json.loads((out / "mv_plan.json").read_text(encoding="utf-8"))
        seq = [(c["file"], c["src_in"]) for c in plan["cuts"]]
        mono = all(x <= y for x, y in zip(seq, seq[1:]))
        check("MV: 撮った順に、素材の頭から終わりへ進む", mono and seq[0][0] != seq[-1][0], str(seq[:3]) + "…")
        check("MV: --no-render ではXMLだけ", Path(plan["xml"]).exists() and not Path(plan["video"]).exists())
    p = _run(["mv.py", str(d / "song128.wav"), str(d / "素材 フォルダ"), "--part", "sabi", "--no-render",
              "--markers", "bars", "--fps", "24", "--out", str(d / "出力 sabi")])
    check("MV: サビだけ（--part sabi）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    if p.returncode == 0:
        plan = json.loads((d / "出力 sabi" / "mv_plan.json").read_text(encoding="utf-8"))
        an = json.loads((d / "出力 sabi" / "mv_song.json").read_text(encoding="utf-8"))
        a, b = plan["range"]
        root = ET.parse(plan["xml"]).getroot()
        marks = root.findall("./sequence/marker")
        check("MV: サビだけの長さと、小節ごとのマーカー", abs((b - a) - (plan["total_frames"] / 24)) < 0.05
              and abs(b - an["best_chorus"]["end"]) < 0.01 and 5 <= len(marks) <= 12 and plan["fps"] == "24/1",
              f"{a}〜{b} マーカー{len(marks)}")
    # 曲がない・素材がない
    p = _run(["mv.py", str(d / "ない曲.wav"), str(d / "素材 フォルダ")])
    check("MV: 曲がないときのエラー", p.returncode != 0 and "曲のファイルが見つかりません" in p.stderr, _tail(p, 3))
    empty = d / "空のフォルダ"
    empty.mkdir(exist_ok=True)
    p = _run(["mv.py", str(d / "曲 テスト.wav"), str(empty), "--out", str(d / "出力 empty")])
    check("MV: 素材がないときのエラー", p.returncode != 0 and "映像素材がありません" in p.stderr, _tail(p, 3))
    env = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}
    p = _run(["mv.py", str(d / "曲 テスト.wav"), str(d / "素材 フォルダ"), "--order", "ai", "--out",
              str(d / "出力 nokey")], env=env)
    check("MV: APIキーがないときは「おまかせ」を案内", p.returncode != 0 and "おまかせ" in p.stderr, _tail(p, 3))


# ---------------------------------------------------------------- AI（偽の通信）
FAKE = r'''
import json, re, runpy, sys
ROOT, MODE, LOG = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = [ROOT + "/mv.py"] + sys.argv[4:]
sys.path.insert(0, ROOT)
import anthropic, httpx2, rc_llm

def sse(text):
    evs = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
          "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
          "usage": {"input_tokens": 5000, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
          "usage": {"output_tokens": 300}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in evs).encode()

def handler(req):
    body = json.loads(req.content)
    json.dump(body, open(LOG, "w", encoding="utf-8"), ensure_ascii=False)
    if MODE == "fail":
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                     "message": "テスト用のエラー"}})
    content = body["messages"][0]["content"]
    n = sum(1 for b in content if b["type"] == "image")
    text = "\n".join(b["text"] for b in content if b["type"] == "text")
    secs = [int(x) for x in re.findall(r"区間(\d+):", text)]
    plan = []
    for s in secs:
        ms = [1] if s == secs[2] else [k for k in range(n, 0, -1)]
        plan.append({"section": s, "materials": ms, "reason": "テスト"})
    res = {"concept": "静かに始まり、サビで一気に広がる流れ", "sections": plan, "avoid": [2]}
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(json.dumps(res, ensure_ascii=False)))

rc_llm._client = lambda: (anthropic, anthropic.Anthropic(api_key="test", max_retries=0,
                          http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
runpy.run_path(ROOT + "/mv.py", run_name="__main__")
'''


def t_ai(check, d: Path):
    fake = d / "fake_claude.py"
    fake.write_text(FAKE, encoding="utf-8")
    out = d / "出力 ai"
    shutil.rmtree(out, ignore_errors=True)
    log = d / "claude_request.json"
    env = {"ANTHROPIC_API_KEY": ""}
    p = _run([str(fake), str(ROOT), "ok", str(log), str(d / "曲 テスト.wav"), str(d / "素材 フォルダ"),
              "--order", "ai", "--note", "夏・青春", "--no-render", "--out", str(out)], env=env)
    check("MV: AIで並べる（偽の通信）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    if p.returncode != 0:
        return
    req = json.loads(log.read_text(encoding="utf-8"))
    content = req["messages"][0]["content"]
    imgs = [b for b in content if b["type"] == "image"]
    texts = "\n".join(b["text"] for b in content if b["type"] == "text")
    frames = sorted((out / "mv_frames").glob("m*.jpg"))
    check("MV: AIに代表フレームと曲の区間を送る", len(imgs) == len(frames) >= 4 and "夏・青春" in texts
          and "サビ" in texts and req["output_config"]["format"]["type"] == "json_schema"
          and "暗い.mp4" not in texts, f"画像{len(imgs)}枚")
    plan = json.loads((out / "mv_plan.json").read_text(encoding="utf-8"))
    m1 = re.search(r"素材1: (.+?) \d+:\d\d〜", texts).group(1)
    secs = sorted({c["section"] for c in plan["cuts"]})
    target = [c for c in plan["cuts"] if c["section"] == secs[2]]
    used = {Path(plan["files"][c["file"]]).name for c in target}
    check("MV: AIの返事の順番どおりに並ぶ", used == {m1} and all(c["why"].startswith("AI") for c in target),
          f"{used} / 素材1={m1}")
    check("MV: AIの考えを保存", plan["concept"].startswith("静かに始まり") and "AIの考え" in p.stdout)
    # 失敗したら、おまかせで並べる
    p = _run([str(fake), str(ROOT), "fail", str(log), str(d / "曲 テスト.wav"), str(d / "素材 フォルダ"),
              "--order", "ai", "--no-render", "--out", str(out)], env=env)
    plan = json.loads((out / "mv_plan.json").read_text(encoding="utf-8")) if (out / "mv_plan.json").exists() else {}
    check("MV: AIが失敗したら自動で並べる", p.returncode == 0 and "[注意]" in p.stdout
          and all(c["why"].startswith("自動") for c in plan.get("cuts", [{}])), _tail(p, 4))


# ---------------------------------------------------------------- 長い素材
def t_long(check, d: Path):
    import time
    import tracemalloc

    import rc_mv
    long = d / "長い素材 30分.mp4"
    if not long.exists():
        mv_fixtures.make_long_video(long, 30)
    tracemalloc.start()
    t0 = time.time()
    c = rc_mv.analyze_clip(long)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    t = np.array(c.t)
    check("MV: 長い素材はキーフレームだけ読む", 800 <= len(t) <= 1000 and bool(np.all(np.diff(t) > 0))
          and t[-1] > 1790, f"{len(t)}点 {time.time() - t0:.1f}秒")
    check("MV: 長い素材でもメモリをほとんど使わない", peak < 40e6, f"最大 {peak / 1e6:.1f}MB")
    wins = rc_mv.build_windows([c], 4.0)
    check("MV: 長い素材を区間に分ける", 400 <= len(wins) <= 460, f"{len(wins)}区間")


# ---------------------------------------------------------------- GUI
GUI_CODE = r'''
import json, os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
shown = []
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
app = QApplication([])
w = gui.MainWindow(); w.resize(1200, 900); w.show()
res = {"ext": "gui_ext_mv" in w.extensions, "has_tab": hasattr(w, "mv_tab")}
res["tab_names"] = [w.tabs.tabText(i) for i in range(w.tabs.count())]
res["menus_hidden"] = not w.tools_menu.menuAction().isVisible()
t = w.mv_tab
calls = []
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    if on_done:
        on_done(True)
    return True
w.runner.start = fake_start
D = Path(DIR)
# 素材がないときは知らせる
t.set_song(D / "曲 テスト.wav")
t.make()
res["no_material_msg"] = any("素材" in s for s in shown) and not calls
# 曲のファイルを素材の欄に落とすと「曲」の欄へ
t.files.add([str(D / "素材 フォルダ"), str(D / "曲 テスト.wav"), str(D / "truth.json")])
res["items"] = [t.files.item(i).text() for i in range(t.files.count())]
res["song"] = t.song.text()
t.note.setText("夏・青春")
t.size.setCurrentIndex(t.size.findData("1080x1920"))
t.make(preview=True)
res["make_args"] = calls[-1][1] if calls else []
# AIは APIキーがないと止める
w.settings.setValue("api_key", "")
os.environ.pop("ANTHROPIC_API_KEY", None)
t.order_group.button(1).setChecked(True)
n = len(calls)
t.make()
res["ai_blocked"] = len(calls) == n
t.order_group.button(0).setChecked(True)
t.analyze()
res["analyze_args"] = calls[-1][1]
# 結果の表示（出力済みのフォルダを使う）
t.set_song(Path(OUTSONG))
t.refresh()
res["info"] = t.info.text()
res["play"] = t.b_play.isEnabled()
res["tip"] = t.graph.tip_at(t.graph.width() * 0.45)
w.tabs.setCurrentWidget(t)
for _ in range(10):
    app.processEvents()
w.grab().save(SHOT)
t.graph.grab().save(SHOT.replace(".png", "_graph.png"))
# 処理中はボタンを押せない
w.runner.busy_changed.emit(True)
res["busy"] = not t.b_make.isEnabled()
w.runner.busy_changed.emit(False)
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, d: Path, fx: Path, shots: Path):
    src = d / "出力 auto"
    if not (src / "mv_plan.json").exists():
        check("MV: GUI（MVの結果が必要）", False)
        return
    # GUIは「曲名_mv」フォルダを見るので、出力をそこへ写す
    gd = d / "gui"
    shutil.rmtree(gd, ignore_errors=True)
    gd.mkdir()
    song = gd / "曲 テスト.wav"
    shutil.copy(d / "曲 テスト.wav", song)
    shutil.copytree(src, gd / "曲 テスト_mv")
    an = json.loads((gd / "曲 テスト_mv" / "mv_song.json").read_text(encoding="utf-8"))
    an["song"] = str(song)
    (gd / "曲 テスト_mv" / "mv_song.json").write_text(json.dumps(an, ensure_ascii=False), encoding="utf-8")
    shot = str(shots / "gui_mv.png")
    code = (f"ROOT = {str(ROOT)!r}\nDIR = {str(d)!r}\nOUTSONG = {str(song)!r}\nSHOT = {shot!r}\n" + GUI_CODE)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata_mv"), APPDATA=str(fx / "appdata_mv"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("MV: GUI", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("MV: GUI", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("MV: GUIに「MV」タブ", r["ext"] and r["has_tab"] and "MV" in r["tab_names"], str(r["tab_names"]))
    check("MV: GUI 空のメニューは出さないまま", r["menus_hidden"])
    check("MV: GUI 素材がないときは知らせる", r["no_material_msg"])
    check("MV: GUI 曲のファイルは「曲」の欄へ・フォルダも素材にできる",
          r["items"] == ["[フォルダ] 素材 フォルダ"] and r["song"].endswith("曲 テスト.wav"), str(r["items"]))
    a = r["make_args"]
    check("MV: GUI「試しに20秒」の引数", a[:2] == [str(d / "曲 テスト.wav"), str(d / "素材 フォルダ")]
          and "--preview" in a and a[a.index("--size") + 1] == "1080x1920" and "夏・青春" in a
          and a[a.index("--order") + 1] == "auto" and "--template" in a, str(a))
    check("MV: GUI AIはAPIキーがないと止める", r["ai_blocked"])
    check("MV: GUI 曲の解析", "--analyze-only" in r["analyze_args"])
    check("MV: GUI 結果の表示", "BPM" in r["info"] and "サビ" in r["info"] and "カット" in r["info"] and r["play"],
          r["info"])
    check("MV: GUI 処理中はボタンを押せない", r["busy"])
    check("MV: GUI グラフの説明（区間と素材）", "サビ" in r["tip"] and ".mp4" in r["tip"], r["tip"].replace("\n", " / "))


# ---------------------------------------------------------------- テンプレート
def t_template(check):
    from rc_template import Template
    tpl = Template(ROOT / "templates" / "default")
    mv = tpl.get("mv", {})
    check("MV: テンプレートに [mv] の設定", mv.get("fit") == "cover" and mv.get("pace") == "normal"
          and tpl.get("video", {}).get("quality") == 19, str(mv))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path | None, fx: Path):
    d = fx / "mv"
    shots = fx / "mv" / "shots"
    try:
        truth = make_fixtures(fx)
    except Exception as e:  # noqa: BLE001
        check("MV: テスト素材の作成", False, str(e))
        return
    shots.mkdir(parents=True, exist_ok=True)
    t_template(check)
    t_song(check, d, truth)
    t_plan(check, d)
    t_cli_auto(check, d, truth, shots)
    t_cli_variants(check, d, truth)
    t_ai(check, d)
    t_long(check, d)
    t_gui(check, d, fx, shots)
    print(f"  （MVの確認用画像: {shots}）")


def main():
    import make_fixtures as mf
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
        return bool(ok)
    fx = mf.FX
    fx.mkdir(parents=True, exist_ok=True)
    run_all(check, None, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
