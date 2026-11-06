"""Bロール（差し込み素材）の自動配置のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_broll.py            # 単独で実行（先に素材と荒編集の結果を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 素材フォルダ（本編と同じ場所の broll）を見つけ、画像・動画（音なし・縦向き・EXIFの向き付き）を読める
  - Claude の呼び出し（偽の通信）: 画像を送って説明をもらい、発言と照らし合わせて場所を決める
  - AIなしでは、ファイル名が発言に出てくる所に入れる
  - 演出プランの "broll" が手直し（plan_edited.json）でも残る・存在しない発言IDは除く
  - 書き出し: 全画面・ワイプ・写真のゆっくりズーム（Ken Burns）が動画に入る（静止画を取り出して確認）
  - Premiere 用 XML に Bロールのトラックが入る
  - 画面（gui_ext_broll）: Bロールの表が出て、手直しが書き出しのプランに入る
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable


def _run(args: list[str], env: dict | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=900)


def _tail(p: subprocess.CompletedProcess, n: int = 15) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


# ---------------------------------------------------------------- テスト素材
def make_assets(folder: Path) -> Path:
    """Bロールの素材: 横の写真・縦の写真（EXIFで回転）・透過PNG・音なし動画・縦の動画"""
    from PIL import Image, ImageDraw, ImageFont

    import make_fixtures
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    font = ImageFont.truetype(str(make_fixtures.find_jp_font()), 120)

    def card(size, color, text):
        im = Image.new("RGB", size, color)
        d = ImageDraw.Draw(im)
        for k in range(0, size[0], 100):   # ズームが分かるように格子を描く
            d.line([(k, 0), (k, size[1])], fill=(255, 255, 255), width=3)
        for k in range(0, size[1], 100):
            d.line([(0, k), (size[0], k)], fill=(255, 255, 255), width=3)
        d.text((size[0] // 2, size[1] // 2), text, font=font, fill=(255, 255, 0), anchor="mm",
               stroke_width=6, stroke_fill=(0, 0, 0))
        return im

    card((2400, 1600), (40, 90, 160), "ドリル写真").save(folder / "マキタ_ドリル.jpg", quality=90)
    # 縦の写真を、横のまま保存して EXIF で「90度回して表示」と書く（スマホと同じ）
    tall = card((1200, 1800), (150, 60, 60), "縦の写真")
    exif = Image.Exif()
    exif[0x0112] = 6
    tall.rotate(90, expand=True).save(folder / "スペック表.jpg", quality=90, exif=exif)
    logo = Image.new("RGBA", (800, 800), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse((50, 50, 750, 750), fill=(30, 160, 80, 255))
    logo.save(folder / "完成品.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(folder / "穴あけ 作業.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=360x640:rate=30",
                    "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(folder / "IMG_0001.mp4")], check=True)
    (folder / "メモ.txt").write_text("素材ではないファイル", encoding="utf-8")
    return folder


def prepare_case(fx: Path) -> Path:
    """本編（テスト素材のコピー）と同じ場所に broll フォルダを置いた撮影フォルダを作り、荒編集までする"""
    from rc_media import SourceSet, probe
    from rc_transcribe import _cache_key
    case = fx / "broll_case" / "撮影 フォルダ"
    if case.parent.exists():
        shutil.rmtree(case.parent)
    case.mkdir(parents=True)
    for n in ("C0001.MP4", "C0002.MP4"):
        shutil.copy2(fx / n, case / n)
    make_assets(case / "broll")
    out = case / "C0001_roughcut"
    out.mkdir()
    tr = json.loads((fx / "transcript.json").read_text(encoding="utf-8"))
    src = SourceSet([probe(case / "C0001.MP4"), probe(case / "C0002.MP4")])
    tr["key"] = _cache_key(src, "large-v3-turbo", "")
    (out / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
    p = _run(["roughcut.py", str(case / "C0001.MP4"), str(case / "C0002.MP4"), "--mode", "talk", "--llm", "none",
              "--decisions", str(fx / "claude_cuts.json"), "--meta", str(fx / "claude_meta.json"),
              "--out", str(out)])
    if p.returncode != 0:
        raise RuntimeError("荒編集に失敗" + _tail(p))
    return out


# ---------------------------------------------------------------- Claude の偽の通信
FAKE_CLAUDE = r'''
import json, re, runpy, sys
sys.path.insert(0, ROOT)
import anthropic, httpx2, rc_llm
LOG = []

def sse(obj):
    text = json.dumps(obj, ensure_ascii=False)
    ev = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
          "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
          "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
          "usage": {"output_tokens": 80}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {k}\ndata: {json.dumps(v, ensure_ascii=False)}\n\n" for k, v in ev).encode("utf-8")

PICK = {"マキタ_ドリル.jpg": (2, 2, "full"), "スペック表.jpg": (6, 6, "full"), "穴あけ 作業.mp4": (8, 8, "wipe"),
        "完成品.png": (9, 10, "wipe"), "IMG_0001.mp4": (11, 11, "full")}

def handler(req):
    body = json.loads(req.content.decode("utf-8"))
    system = body["system"] if isinstance(body["system"], str) else json.dumps(body["system"], ensure_ascii=False)
    content = body["messages"][0]["content"]
    if "差し込み素材（Bロール）として用意された" in system:
        texts = [b["text"] for b in content if b["type"] == "text"]
        images = [b for b in content if b["type"] == "image"]
        LOG.append({"kind": "describe", "texts": texts, "images": len(images),
                    "image_ok": all(b["source"]["media_type"] == "image/jpeg" and len(b["source"]["data"]) > 1000
                                    for b in images)})
        out = []
        for t in texts:
            m = re.match(r"\[(A\d+)\] ファイル名: (.+?)（", t)
            if m:
                out.append({"key": m.group(1), "description": f"{m.group(2)} の様子", "keywords": [m.group(2)[:2]]})
        res = {"assets": out}
    else:
        text = content if isinstance(content, str) else content[0]["text"]
        LOG.append({"kind": "match", "has_lines": "#2 [" in text, "has_desc": "の様子" in text})
        keys = dict((n, k) for k, n in re.findall(r"\[(B\d+)\] \S+ (.+?): ", text))
        res = {"broll": [{"asset": keys[n], "start_id": s, "end_id": e, "mode": m, "reason": "テスト"}
                         for n, (s, e, m) in PICK.items() if n in keys]
               + [{"asset": "B1", "start_id": 999, "end_id": 999, "mode": "full", "reason": "存在しない発言"}]}
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(res))

def fake_client():
    return anthropic, anthropic.Anthropic(api_key="test-key",
                                          http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
rc_llm._client = fake_client
sys.argv = [SCRIPT] + ARGS
try:
    runpy.run_path(SCRIPT, run_name="__main__")
finally:
    open(LOGFILE, "w", encoding="utf-8").write(json.dumps(LOG, ensure_ascii=False))
'''


def run_with_fake_claude(script: str, args: list[str], logfile: Path) -> tuple[subprocess.CompletedProcess, list]:
    code = (f"ROOT = {str(ROOT)!r}\nSCRIPT = {str(ROOT / script)!r}\nARGS = {args!r}\nLOGFILE = {str(logfile)!r}\n"
            + FAKE_CLAUDE)
    p = _run(["-c", code], env={"ANTHROPIC_API_KEY": ""})
    try:
        log = json.loads(logfile.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log = []
    return p, log


# ---------------------------------------------------------------- 単体
def t_units(check, out: Path):
    import rc_broll
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    c = rc_broll.config(None)
    folder = rc_broll.find_folder(out, sess, c)
    check("Bロール: 本編と同じ場所の broll フォルダを見つける", folder is not None and folder.name == "broll", str(folder))
    files = rc_broll.scan(folder) if folder else []
    names = sorted(f.name for f in files)
    check("Bロール: 画像・動画だけを読む", names == sorted(["IMG_0001.mp4", "スペック表.jpg", "マキタ_ドリル.jpg",
                                                     "完成品.png", "穴あけ 作業.mp4"]), str(names))
    a = {f.name: rc_broll.probe_asset(f) for f in files}
    check("Bロール: 縦の写真の向き（EXIF）を直して読む", a["スペック表.jpg"].height > a["スペック表.jpg"].width,
          f"{a['スペック表.jpg'].width}x{a['スペック表.jpg'].height}")
    check("Bロール: 音のない動画も読める", a["穴あけ 作業.mp4"].kind == "video" and abs(a["穴あけ 作業.mp4"].duration - 6) < 0.2)

    _tl, _by_id, items = rc_broll.load_items(out, sess)
    span = {i: (s, e) for i, s, e, _ in items}
    res = rc_broll.match_by_name(items, [a[n] for n in sorted(a)], c)
    got = [(Path(r["file"]).name, r["start_id"]) for r in res]
    check("Bロール: AIなしはファイル名で合わせる", got == [("マキタ_ドリル.jpg", 2)], str(got))
    clean = rc_broll.sanitize([{"file": "x.jpg", "start_id": 999}, {"file": "x.jpg", "start_id": 2, "end_id": 1,
                                                                    "mode": "zzz"}], set(span))
    check("Bロール: プランの不正な値を直す", clean == [{"file": "x.jpg", "start_id": 2, "end_id": 2, "mode": "full",
                                                "reason": ""}], str(clean))
    metas = {"a": a["マキタ_ドリル.jpg"], "v": a["IMG_0001.mp4"]}
    # #2: 発言の長さ(2.6秒) / #6〜#12: 長すぎるので7秒まで → 次の #8 の所で切る / #8: 3秒の指定 / #11: 動画は4秒まで
    pl = rc_broll.resolve([{"file": "a", "start_id": 2, "end_id": 2}, {"file": "a", "start_id": 6, "end_id": 12},
                           {"file": "a", "start_id": 8, "end_id": 8, "sec": 3},
                           {"file": "v", "start_id": 11, "end_id": 12}], span, 29.9, c, metas)
    ok = (len(pl) == 4 and abs(pl[0].t1 - pl[0].t0 - 2.6) < 0.01 and pl[0].t1 <= pl[1].t0
          and abs(pl[1].t1 - span[8][0]) < 1e-6 and abs(pl[2].t1 - pl[2].t0 - 3) < 1e-6
          and abs(pl[3].t1 - pl[3].t0 - 4.0) < 0.1)
    check("Bロール: 長さ（動画の長さまで・重なりは前を短く・秒数の指定）", ok,
          str([(round(p.t0, 2), round(p.t1, 2)) for p in pl]))
    import rc_plan
    sp = rc_plan.sanitize({"telops": [], "zooms": [], "bgm": [], "broll": [{"file": "x", "start_id": 2}]}, set(span), [])
    sp0 = rc_plan.sanitize({"telops": [], "zooms": [], "bgm": []}, set(span), [])
    check("Bロール: 演出プランの整理で broll が残る（無いプランは今まで通り）", len(sp.get("broll", [])) == 1
          and "broll" not in sp0)


# ---------------------------------------------------------------- AI（偽の通信）→ プラン → 書き出し
def _frame(video: Path, t: float, dst: Path):
    from PIL import Image
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(dst)],
                   check=True)
    return Image.open(dst).convert("RGB")


def _mean(im, box):
    import numpy as np
    return np.asarray(im.crop(box), dtype=float).reshape(-1, 3).mean(0)


def t_ai_and_render(check, out: Path, fx: Path):
    import numpy as np
    tpl = str(fx / "tpl")
    p, log = run_with_fake_claude("broll.py", [str(out), "--template", tpl, "--llm", "api"], out / "fake_log.json")
    check("Bロール: broll.py（Claudeは偽の通信）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    desc = [x for x in log if x["kind"] == "describe"]
    check("Bロール: 素材の画像をClaudeに送る（動画は代表コマ）", bool(desc) and sum(d["images"] for d in desc) == 5
          and all(d["image_ok"] for d in desc), str(desc)[:300])
    match = [x for x in log if x["kind"] == "match"]
    check("Bロール: 発言と素材の説明を照らし合わせる", bool(match) and match[0]["has_lines"] and match[0]["has_desc"],
          str(match))
    plan = json.loads((out / "plan_used.json").read_text(encoding="utf-8")) if (out / "plan_used.json").exists() else {}
    got = sorted((Path(b["file"]).name, b["start_id"], b["mode"]) for b in plan.get("broll", []))
    check("Bロール: 演出プランに入る（存在しない発言は除く）", len(got) == 5 and all(g[1] != 999 for g in got), str(got))
    xml = out / "C0001_Bロール入り.xml"
    try:
        root = ET.parse(xml).getroot()
        tracks = root.findall("./sequence/media/video/track")
        names = [[c.findtext("name") for c in t.findall("clipitem")] for t in tracks]
        ok = len(tracks) == 3 and sorted(names[1]) == ["IMG_0001.mp4", "スペック表.jpg", "マキタ_ドリル.jpg"] \
            and sorted(names[2]) == ["完成品.png", "穴あけ 作業.mp4"]
        motion = tracks[1].find("clipitem/filter/effect/parameter/keyframe") is not None
        check("Bロール: Premiere用XMLに V2（全画面）/ V3（ワイプ）", ok and motion, str(names))
    except Exception as e:  # noqa: BLE001
        check("Bロール: Premiere用XMLに V2（全画面）/ V3（ワイプ）", False, str(e))

    # 2回目は素材の説明を使い回す（AIに画像を送り直さない）
    p, log = run_with_fake_claude("broll.py", [str(out), "--template", tpl, "--llm", "api"], out / "fake_log2.json")
    check("Bロール: 素材の説明を使い回す", p.returncode == 0 and not [x for x in log if x["kind"] == "describe"]
          and bool([x for x in log if x["kind"] == "match"]), str(log)[:200])
    if not plan.get("broll"):
        return

    # 手直しを想定: テロップなどは fixtures の plan.json、Bロールは今のプラン
    base = json.loads((fx / "plan.json").read_text(encoding="utf-8"))
    base["broll"] = plan["broll"]
    (out / "plan_edited.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(out), "--template", tpl, "--plan", str(out / "plan_edited.json"), "--llm", "none",
              "--no-thumbs"])
    check("Bロール: 書き出し（finish.py）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    mp4 = out / "C0001_完成.mp4"
    if p.returncode != 0 or not mp4.exists():
        return
    used = json.loads((out / "plan_used.json").read_text(encoding="utf-8"))
    check("Bロール: 書き出したプランにも残る", len(used.get("broll", [])) == 5)
    shots = out / "broll_shots"
    shots.mkdir(exist_ok=True)
    # 時刻（編集後）: #2 2.57〜 / #6 5.55〜 / #8 9.95〜 / #9 13.87〜 / #11 19.27〜
    f_a = _frame(mp4, 3.0, shots / "1_drill_a.png")
    f_b = _frame(mp4, 4.8, shots / "2_drill_b.png")
    f_tall = _frame(mp4, 7.5, shots / "3_tall_blur.png")
    f_wipe = _frame(mp4, 11.5, shots / "4_wipe_video.png")
    f_png = _frame(mp4, 16.0, shots / "5_wipe_png.png")
    f_vert = _frame(mp4, 21.5, shots / "6_vertical_video.png")
    f_none = _frame(mp4, 1.0, shots / "0_none.png")
    blue = _mean(f_a, (200, 200, 1720, 880))
    check("Bロール: 全画面で写真が出る", blue[2] > blue[0] + 40, str(blue.round()))
    diff = np.abs(np.asarray(f_a, dtype=float) - np.asarray(f_b, dtype=float)).mean()
    check("Bロール: 写真がゆっくり動く（Ken Burns）", diff > 3, f"差 {diff:.1f}")
    # 中央は写真そのもの（格子がくっきり）、左右はぼかした背景（となりの点との差が小さい）
    arr = np.asarray(f_tall, dtype=float)
    side, mid = (np.abs(np.diff(arr[300:780, x0:x1], axis=1)).mean() for x0, x1 in ((20, 400), (700, 1100)))
    red = _mean(f_tall, (700, 300, 1100, 780))
    check("Bロール: 縦の写真は、ぼかした背景の上に全体を出す", red[0] > red[2] + 40 and mid > side * 3,
          f"くっきり度 横 {side:.2f} / 中 {mid:.2f}")
    border = _mean(f_wipe, (1228, 40, 1234, 400))
    check("Bロール: ワイプ（右上・白いふち）", border.min() > 220, str(border.round()))
    a0, a1 = _mean(f_none, (1300, 60, 1800, 380)), _mean(f_wipe, (1300, 60, 1800, 380))
    check("Bロール: ワイプの中に素材の動画", abs(a0 - a1).sum() > 30, f"{a0.round()} → {a1.round()}")
    g = _mean(f_png, (1300, 60, 1800, 380))
    check("Bロール: 透過PNGもワイプで出る", g[1] > g[0] + 40, str(g.round()))
    d = _mean(f_vert, (860, 200, 1060, 880)) - _mean(f_none, (860, 200, 1060, 880))
    check("Bロール: 縦の動画も全画面で出る", abs(d).sum() > 30, str(d.round()))
    tail = _mean(_frame(mp4, 27.5, shots / "7_after.png"), (200, 200, 1720, 880))
    check("Bロール: 出し終わったら本編に戻る", abs(tail - _mean(f_none, (200, 200, 1720, 880))).sum() < 60,
          str(tail.round()))

    # 試し書き出しでも Bロールが入る
    p = _run(["finish.py", str(out), "--template", tpl, "--plan", str(out / "plan_edited.json"), "--preview", "6",
              "--no-thumbs"])
    pv = out / "C0001_プレビュー.mp4"
    ok = p.returncode == 0 and pv.exists()
    if ok:
        b = _mean(_frame(pv, 3.0, shots / "8_preview.png"), (200, 200, 1720, 880))
        ok = b[2] > b[0] + 40
    check("Bロール: 試し書き出し（--preview）にも入る", ok, "" if p.returncode == 0 else _tail(p))


def t_auto_in_finish(check, out: Path, fx: Path):
    """finish.py で新しく演出プランを作るとき（AIなし）、素材フォルダがあれば Bロールも入る"""
    import rc_broll
    for n in ("plan_used.json", "plan_edited.json", rc_broll.CATALOG):   # AIが付けた説明も使わない
        (out / n).unlink(missing_ok=True)
    p = _run(["finish.py", str(out), "--template", str(fx / "tpl"), "--llm", "none", "--plan-only"])
    plan = json.loads((out / "plan_used.json").read_text(encoding="utf-8")) if (out / "plan_used.json").exists() else {}
    got = [(Path(b["file"]).name, b["start_id"]) for b in plan.get("broll", [])]
    check("Bロール: 演出プランを作るとき自動で入る（AIなし＝ファイル名）", p.returncode == 0
          and got == [("マキタ_ドリル.jpg", 2)], str(got) + ("" if p.returncode == 0 else _tail(p)))
    # 素材フォルダが無い撮影では、今まで通り（broll を作らない）
    empty = fx / "broll_case" / "空"
    empty.mkdir(exist_ok=True)
    plan2 = {"telops": []}
    rc_broll.auto_place(empty, {"inputs": [str(fx / "C0001.MP4")], "mode": "talk"}, None, [], plan2, "none", "low",
                        {"in": 0, "out": 0})
    check("Bロール: 素材フォルダが無ければ何もしない", "broll" not in plan2, str(plan2))


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import json, os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
QMessageBox.information = staticmethod(lambda *a, **k: None)
out = Path(OUT)
w = gui.MainWindow(); w.resize(1500, 900); w.show()
res = {"loaded": "gui_ext_broll" in getattr(w, "extensions", [])}
rv = w.review
rv.load(out)
panel = rv.broll_panel
res["tab"] = rv.plan_tabs.tabText(rv.plan_tabs.indexOf(panel))
res["rows"] = panel.table.rowCount()
res["icons"] = sum(1 for r in range(panel.table.rowCount()) if not panel.table.item(r, 2).icon().isNull())
res["folder"] = panel.folder.text()
w.tabs.setCurrentWidget(rv)
rv.tabs.setCurrentIndex(1)
rv.plan_tabs.setCurrentWidget(panel)
for _ in range(5):
    app.processEvents()
w.grab().save(str(out / "gui_broll.png"))
# 1行目をワイプに、2行目を使わないに、3行目を4秒にする
panel.table.cellWidget(0, 3).setCurrentIndex(panel.table.cellWidget(0, 3).findData("wipe"))
panel.table.item(1, 0).setCheckState(Qt.Unchecked)
panel.table.cellWidget(2, 4).setValue(4)
plan = rv._collect_plan()
res["modes"] = [b["mode"] for b in plan["broll"]]
res["off"] = [Path(b["file"]).name for b in plan["_off"]["broll"]]
res["sec"] = [b.get("sec") for b in plan["broll"]]
res["telops_kept"] = len(plan["telops"])
calls = []
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    if on_done:
        QTimer.singleShot(0, lambda: on_done(True))
    return True
w.runner.start = fake_start
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
rv.render(preview=True)
for _ in range(10):
    app.processEvents(); time.sleep(0.02)
ed = json.loads((out / "plan_edited.json").read_text(encoding="utf-8"))
res["edited"] = [(Path(b["file"]).name, b["mode"], b.get("sec")) for b in ed["broll"]]
res["edited_off"] = [Path(b["file"]).name for b in ed["_off"]["broll"]]
# 開き直しても手直しが残る
w2 = gui.MainWindow(); w2.review.load(out)
p2 = w2.review.broll_panel
res["reopen"] = [(p2.table.item(r, 0).checkState() == Qt.Checked, p2.table.cellWidget(r, 3).currentData(),
                  p2.table.cellWidget(r, 4).value()) for r in range(p2.table.rowCount())]
w2.close()
# 「素材を発言に割り当てる」→ broll.py を呼ぶ
calls.clear()
panel.assign()
for _ in range(10):
    app.processEvents(); time.sleep(0.02)
res["assign"] = [(s, a) for s, a in calls]
w.grab().save(str(out / "gui_broll_edited.png"))
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, fx: Path):
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(out)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("Bロール画面: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("Bロール画面: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("Bロール画面: 拡張が読み込まれる", r["loaded"])
    check("Bロール画面: 表に素材が並ぶ（小さな画像つき）", r["rows"] == 5 and r["icons"] == 5 and "（5）" in r["tab"],
          f"{r['tab']} {r['rows']}行 画像{r['icons']}")
    check("Bロール画面: 素材フォルダを表示", "broll" in r["folder"], r["folder"])
    check("Bロール画面: 出し方・使う・長さを変えられる", r["modes"][:1] == ["wipe"] and len(r["off"]) == 1
          and r["sec"][1:2] == [4] and r["telops_kept"] > 0, f"{r['modes']} {r['off']} {r['sec']}")
    check("Bロール画面: 書き出すプランに手直しが入る", len(r["edited"]) == 4 and r["edited"][0][1] == "wipe"
          and len(r["edited_off"]) == 1, str(r["edited"]))
    check("Bロール画面: 開き直しても手直しが残る", len(r["reopen"]) == 5
          and [x[0] for x in r["reopen"]].count(False) == 1 and r["reopen"][0][1] == "wipe", str(r["reopen"]))
    ok = r["assign"] and r["assign"][0][0] == "broll.py" and "--llm" in r["assign"][0][1]
    check("Bロール画面: 「素材を発言に割り当てる」で broll.py を実行", bool(ok), str(r["assign"]))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    try:
        case = prepare_case(fx)
    except Exception as e:  # noqa: BLE001
        check("Bロール: テストの準備", False, str(e))
        return
    t_units(check, case)
    t_ai_and_render(check, case, fx)
    t_gui(check, case, fx)
    t_auto_in_finish(check, case, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
