"""人物の切り抜きサムネイルと、顔を見た候補選びのテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_thumbspro.py        # 単独で実行（先に tests/smoke_test.py を実行しておくと全部できます）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 顔の検出（顔の大きさ・目が開いているか）、顔のない画像では何も見つけない
  - 目を閉じた画像より、目が開いた画像を選ぶ / 顔のある候補・表情の違う候補を優先する
  - 人物の切り抜き（顔の位置から推定）と、白いフチ付きの人物デザイン（背景5種類）
  - 切り抜き用データの自動ダウンロード（偽のサーバー）と、失敗したときに簡易な方法へ切り替える
  - 動画からのサムネイル案に person のデザインが加わる（AIなし・偽のClaude）
  - OpenCV が使えないときは、ほかのデザインだけになる
  - GUI の「ツール」メニューからお試しができる（画面なしモードで画像に保存）

テスト用の画像 tests/data/person_sample.jpg は NASA の宇宙飛行士の写真（パブリックドメイン。
scikit-image の astronaut と同じもの）。tests/data/fake_humanseg.onnx は、本物の切り抜きデータと同じ形の
入出力をする小さな偽物（赤みの強い所を人物とみなすだけ）。
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

PY = sys.executable
DATA = ROOT / "tests" / "data"


def _person_frame(size=(1280, 720), mouth: bool = False, closed: bool = False) -> Image.Image:
    """人物が右寄りに大きく写った、動画の1コマのような画像"""
    W, H = size
    src = Image.open(DATA / "person_sample.jpg").convert("RGB")
    bg = src.crop((380, 0, 512, 200)).resize((W, H)).filter(ImageFilter.GaussianBlur(6))
    p = src.resize((H, H))
    bg.paste(p, (W - H - W // 16, 0))
    if mouth or closed:
        import rc_thumbspro as P
        f = P.detect_faces(bg)[0]
        x, y, w, h = f["box"]
        d = ImageDraw.Draw(bg)
        if mouth:   # 口を大きく開けた表情の代わり
            d.ellipse((x + w * 0.38, y + h * 0.74, x + w * 0.62, y + h * 0.9), fill=(90, 30, 35))
        if closed:  # 目を閉じた代わり（目の帯を肌の色で塗って線を引く）
            skin = bg.getpixel((int(x + w * 0.5), int(y + h * 0.62)))
            band = (int(x + w * 0.15), int(y + h * 0.3), int(x + w * 0.85), int(y + h * 0.5))
            d.rectangle(band, fill=skin)
            for x0 in (0.22, 0.58):
                d.line((x + w * x0, y + h * 0.42, x + w * (x0 + 0.2), y + h * 0.42), fill=(60, 40, 30), width=3)
    return bg


def _testsrc(path: Path) -> Image.Image:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=1", "-frames:v",
                    "1", str(path)], check=True)
    return Image.open(path).convert("RGB")


def _tpl(fx: Path):
    from rc_template import Template
    return Template(fx / "tpl")


def _font(tpl):
    import rc_thumb
    return rc_thumb.find_font(tpl)


# ---------------------------------------------------------------- 顔の検出・点数
def t_faces(check, work: Path, fx: Path):
    import rc_thumbspro as P
    if not P.available():
        check("人物: OpenCV が使える", False, "opencv-python-headless が入っていません")
        return False
    fr = _person_frame()
    faces = P.detect_faces(fr)
    f = faces[0] if faces else {}
    check("人物: 顔を見つける（目も）", len(faces) == 1 and f.get("eyes") == 2 and f.get("size", 0) > 0.15,
          str([{k: v for k, v in x.items() if k != "crop"} for x in faces]))
    ts = _testsrc(work / "testsrc.png")
    check("人物: 顔のない画像では見つけない", P.detect_faces(ts) == [])
    closed = _person_frame(closed=True)
    k_open, k_closed = P.frame_key(fr, 100.0), P.frame_key(closed, 100.0)
    check("人物: 目を閉じた画像より、目が開いた画像を選ぶ", k_open > k_closed * 1.5, f"{k_open:.0f} > {k_closed:.0f}")

    # 候補の点数: 顔あり > 顔なし、表情の違う候補が「表情の変化が大きい」
    paths = []
    for name, img in (("none", ts), ("neutral1", fr), ("neutral2", fr.copy()), ("mouth", _person_frame(mouth=True))):
        p = work / f"cand_{name}.jpg"
        img.save(p, quality=92)
        paths.append(p)
    import rc_thumb
    cands = [{"no": i + 1, "t": float(i), "why": "全体から", "sharp": rc_thumb.sharpness(Image.open(p)), "path": p}
             for i, p in enumerate(paths)]
    d = P.Designer(_tpl(fx))
    cands = d.annotate(cands)
    by = {c["path"].stem.split("_", 1)[1]: c for c in cands}
    check("人物: 顔のある候補を優先", by["none"]["score"] < min(by["neutral1"]["score"], by["mouth"]["score"]),
          str({k: c["score"] for k, c in by.items()}))
    ex = {k: c["face"].get("expression") for k, c in by.items() if c.get("face")}
    check("人物: 表情の変化を見つける", by["mouth"]["face"] and ex.get("mouth") == max(ex.values())
          and "表情の変化が大きい" in by["mouth"]["note"], str(ex) + " " + by["mouth"].get("note", ""))
    check("人物: AIへの補足（顔の様子）", "目が開いている" in by["neutral1"]["note"] and by["none"]["note"] == "顔: 見つからない",
          by["neutral1"]["note"])
    picks = rc_thumb.pick_simple(cands, ["テスト"], "")
    check("人物: AIなしでも顔のある候補から選ぶ", cands[picks[0]["frame"] - 1]["path"].stem != "cand_none",
          str([p["frame"] for p in picks]))
    return True


# ---------------------------------------------------------------- 切り抜きとデザイン
def t_compose(check, work: Path, fx: Path):
    import rc_thumbspro as P
    tpl = _tpl(fx)
    cfg = dict(P.settings(tpl), model="off")
    fr = _person_frame()
    face = P.detect_faces(fr)[0]
    a = P.person_mask(fr, face, cfg, None)
    ok = a is not None
    x, y, w, h = face["box"]
    detail = ""
    if ok:
        area = float((a > 0.5).mean())
        inside = a[int(y + h / 2), int(x + w / 2)] > 0.9
        corner = a[5, 5] < 0.1 and a[5, -5] < 0.1
        ok = 0.15 < area < 0.7 and inside and corner
        detail = f"面積{area:.2f} 顔の中{inside} 角は背景{corner}"
    check("人物: 顔の位置から人物を切り抜く", ok, detail)
    check("人物: 顔がなければ切り抜かない", P.person_mask(_testsrc(work / "ts.png"), None, cfg, None) is None)
    if a is None:
        return
    font = _font(tpl)
    tiles = []
    for style in P.BACKGROUNDS:
        img = P.compose_person(fr, a, "宇宙服\\nレビュー", style, tpl, font, cfg)
        img.save(work / f"person_{style}.jpg", quality=90)
        tiles.append(img)
    arr = np.asarray(tiles[2])
    # 白いフチ: 人物の右上の輪郭の外側に白っぽい画素がある
    white = (arr.min(axis=2) > 235).mean()
    check("人物: 5種類の背景で 1280x720 のデザインができる", all(t.size == (1280, 720) for t in tiles))
    check("人物: 白いフチが付いている", white > 0.01, f"白い画素 {white:.3f}")
    # 文字は人物の反対側（左）: 左半分は背景だけのときより変化が多い
    left = np.asarray(tiles[4])[:, :500].std()
    check("人物: 人物の反対側に文字が入る", left > 30, f"左側のばらつき {left:.1f}")
    grid = Image.new("RGB", (1280, 1080), "white")
    for i, t in enumerate(tiles + [P.cutout(fr, a).convert("RGB")]):
        t2 = t.copy()
        t2.thumbnail((640, 360))
        grid.paste(t2, ((i % 2) * 640, (i // 2) * 360))
    grid.save(work / "person_designs.jpg", quality=88)
    side = P.compose_person(fr.transpose(Image.FLIP_LEFT_RIGHT), a[:, ::-1].copy(), "左右", "blur", tpl, font, cfg)
    side.save(work / "person_left.jpg", quality=88)
    check("人物: 左にいる人物は左に置き、文字は右", np.asarray(side)[:, 800:].std() > 30)
    # ほかのデザイン用: 人物はそのままで背景だけぼかす
    pt = P.portrait(fr, a, "blur")
    diff_person = np.abs(np.asarray(pt, np.float32) - np.asarray(fr, np.float32))[a > 0.99].mean()
    diff_bg = np.abs(np.asarray(pt, np.float32) - np.asarray(fr, np.float32))[a < 0.01].mean()
    check("人物: 背景だけぼかす（ほかのデザイン用）", diff_person < 1.0 and diff_bg > 3, f"{diff_person:.2f} / {diff_bg:.2f}")


# ---------------------------------------------------------------- 切り抜き用データ（ダウンロード）
class _Server:
    def __init__(self, files: dict[str, bytes]):
        self.hits: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                body = files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def t_model(check, work: Path, fx: Path):
    import rc_thumbspro as P
    md = work / "models"
    shutil.rmtree(md, ignore_errors=True)
    old = (os.environ.get("AVE_MODEL_DIR"), list(P.MODEL_URLS), P.MIN_MODEL_BYTES, os.environ.get("NO_PROXY"))
    os.environ["AVE_MODEL_DIR"] = str(md)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    P.MIN_MODEL_BYTES = 50
    srv = _Server({"/ok/" + P.MODEL_NAME: (DATA / "fake_humanseg.onnx").read_bytes()})
    try:
        # 1つ目の場所が壊れていても、2つ目から取れる
        P.MODEL_URLS[:] = [srv.url + "/missing/" + P.MODEL_NAME, srv.url + "/ok/" + P.MODEL_NAME]
        m = P.find_model({"model": "auto"})
        check("人物: 切り抜き用データを初回に自動ダウンロード", m is not None and m.exists() and len(srv.hits) == 2,
              str(srv.hits))
        n = len(srv.hits)
        m2 = P.find_model({"model": "auto"})
        check("人物: 2回目はダウンロードしない", m2 == m and len(srv.hits) == n)
        fr = _person_frame()
        prob = P._model_prob(np.asarray(fr), m)
        check("人物: 切り抜き用データで人物らしさを計算（OpenCVだけで動く）",
              prob is not None and prob.shape == (720, 1280) and 0 <= prob.min() and prob.max() <= 1)
        a = P.person_mask(fr, P.detect_faces(fr)[0], P.settings(_tpl(fx)), m)
        check("人物: データ＋顔の位置で切り抜く", a is not None and 0.05 < (a > 0.5).mean() < 0.85)
        # ダウンロードに失敗したら、簡易な方法に切り替え、すぐには再試行しない
        shutil.rmtree(md)
        P.MODEL_URLS[:] = [srv.url + "/missing/" + P.MODEL_NAME]
        srv.hits.clear()
        r1 = P.find_model({"model": "auto"})
        r2 = P.find_model({"model": "auto"})
        check("人物: ダウンロードできなければ簡易な方法（1日は再試行しない）",
              r1 is None and r2 is None and len(srv.hits) == 1 and (md / "download_failed.txt").exists(),
              str(srv.hits))
        check("人物: model = \"off\" ならダウンロードしない", P.find_model({"model": "off"}) is None and len(srv.hits) == 1)
    finally:
        srv.close()
        P.MODEL_URLS[:] = old[1]
        P.MIN_MODEL_BYTES = old[2]
        for k, v in (("AVE_MODEL_DIR", old[0]), ("NO_PROXY", old[3])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- 動画からのサムネイル案
def _ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, a)], check=True)


def _person_session(out: Path, fx: Path, work: Path) -> Path:
    """荒編集の結果をコピーし、素材を「人物が写った動画」に差し替える"""
    sd = work / "sess"
    if sd.exists():
        shutil.rmtree(sd)
    shutil.copytree(out, sd, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "*.png"))
    _person_frame((1920, 1080)).save(work / "p1.png")
    _person_frame((1920, 1080), mouth=True).save(work / "p2.png")
    vids = []
    for name, img, rng in (("C0001.MP4", "p1.png", "between(t,10,30)"), ("C0002.MP4", "p2.png", "between(t,0,16)")):
        dst = work / f"人物 {name}"     # 日本語とスペースを含むファイル名
        _ff("-i", fx / name, "-loop", "1", "-i", work / img, "-filter_complex",
            f"[0:v][1:v]overlay=enable='{rng}':shortest=1[v]", "-map", "[v]", "-map", "0:a", "-c:v", "libx264",
            "-preset", "ultrafast", "-c:a", "copy", dst)
        vids.append(str(dst))
    s = json.loads((sd / "session.json").read_text(encoding="utf-8"))
    s["inputs"] = vids
    (sd / "session.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    return sd


def t_generate(check, out: Path, fx: Path, work: Path):
    sd = _person_session(out, fx, work)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([PY, "thumbs.py", str(sd), "--template", str(fx / "tpl"), "--llm", "none", "--text", "人物\\nテスト"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       timeout=600)
    td = sd / "thumbnails"
    jpgs = sorted(td.glob("thumb_*.jpg"))
    persons = sorted(td.glob("thumb_*_person.jpg"))
    check("人物: サムネイル案に人物デザインが加わる（thumbs.py）", p.returncode == 0 and len(persons) >= 2
          and len(jpgs) == 9 + len(persons), "\n".join((p.stdout + p.stderr).strip().splitlines()[-12:]))
    info = json.loads((td / "thumbnails.json").read_text(encoding="utf-8")) if (td / "thumbnails.json").exists() else []
    t0 = info[0]["source_time"] if info else -1
    check("人物: 顔の写った場面を優先して選ぶ", 10 <= t0 <= 46, f"1枚目の場面 {t0}秒")
    cut = td / "cutout_1.png"
    if cut.exists():
        im = Image.open(cut)
        al = np.asarray(im.getchannel("A")) if im.mode == "RGBA" else np.full((1, 1), 255)
        check("人物: 切り抜いた人物を透過PNGでも保存", im.mode == "RGBA" and al.min() == 0 and al.max() == 255)
    else:
        check("人物: 切り抜いた人物を透過PNGでも保存", False, "cutout_1.png がありません")
    if persons:
        Image.open(persons[0]).save(work / "generated_person.jpg")
    return sd


def _sse(obj: dict) -> bytes:
    text = json.dumps(obj, ensure_ascii=False)
    ev = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 80}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for e, d in ev).encode("utf-8")


def t_generate_ai(check, sd: Path, fx: Path, work: Path):
    """偽の通信で Claude に選ばせる。候補の説明に顔の様子が入り、選ばれた場面で人物デザインを作る"""
    import anthropic
    import httpx2
    import rc_llm
    import rc_thumb
    seen = {}

    def handler(req):
        body = json.loads(req.content.decode("utf-8"))
        blocks = body["messages"][0]["content"]
        labels = [b["text"] for b in blocks if b.get("type") == "text" and b["text"].startswith("候補")]
        seen["labels"] = labels
        face_nos = [int(lb[2:lb.index("（")]) for lb in labels if "顔: 大きく" in lb or "顔: 写って" in lb]
        nos = face_nos[:2] + [int(labels[0][2:labels[0].index("（")])]
        picks = [{"frame": n, "text": f"AI案{i + 1}", "reason": "テスト"} for i, n in enumerate(nos)]
        seen["picks"] = nos
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse({"picks": picks}))

    client = anthropic.Anthropic(api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    orig = rc_llm._client
    rc_llm._client = lambda: (anthropic, client)
    try:
        outs = rc_thumb.generate(sd, _tpl(fx), "api", "low")
    except BaseException as e:  # noqa: BLE001
        check("人物: Claude が選んだ場面で人物デザイン（偽の通信）", False, repr(e))
        return
    finally:
        rc_llm._client = orig
    labels = seen.get("labels", [])
    check("人物: Claude に顔の様子も伝える", any("目が開いている" in lb for lb in labels)
          and any("顔: 見つからない" in lb for lb in labels), " / ".join(labels[:3]))
    info = json.loads((sd / "thumbnails" / "thumbnails.json").read_text(encoding="utf-8"))
    names = {p.name for p in outs}
    check("人物: Claude が選んだ場面で人物デザイン（偽の通信）",
          [i["text"] for i in info] == ["AI案1", "AI案2", "AI案3"] and "thumb_1_person.jpg" in names
          and "thumb_2_person.jpg" in names, str(sorted(names)))


def t_no_cv2(check, sd: Path, fx: Path):
    """OpenCV が使えないPCでは、今までどおり3種類のデザインだけ"""
    import rc_thumb
    import rc_thumbspro as P
    orig = P.cv2
    P.cv2 = None
    P._NOTICE.clear()
    try:
        outs = rc_thumb.generate(sd, _tpl(fx), "none", "low")
    except BaseException as e:  # noqa: BLE001
        outs = []
        check("人物: OpenCV が使えないときは、ほかのデザインだけ", False, repr(e))
        return
    finally:
        P.cv2 = orig
    names = sorted(p.name for p in outs)
    check("人物: OpenCV が使えないときは、ほかのデザインだけ", len(outs) == 9 and not any("person" in n for n in names)
          and not list((sd / "thumbnails").glob("cutout_*.png")), str(names))


def t_layout_list(check, sd: Path, fx: Path, work: Path):
    """person = false でも layouts に "person" を書けば作る。person を書かず false なら作らない"""
    import rc_thumb
    from rc_template import Template
    tdir = work / "tpl_off"
    if tdir.exists():
        shutil.rmtree(tdir)
    shutil.copytree(fx / "tpl", tdir, ignore=shutil.ignore_patterns("bgm"))
    s = (tdir / "template.toml").read_text(encoding="utf-8")
    s = s.replace("\nperson = true", "\nperson = false").replace('backgrounds = ["blur", "vivid", "burst"]',
                                                                 'backgrounds = ["dark"]')
    (tdir / "template.toml").write_text(s, encoding="utf-8")
    outs = rc_thumb.generate(sd, Template(tdir), "none", "low", "オフ")
    check("人物: person = false なら作らない", not any("person" in p.name for p in outs), str([p.name for p in outs]))
    s = s.replace('layouts = ["bottom", "band", "side"]', 'layouts = ["person", "bottom"]')
    (tdir / "template.toml").write_text(s, encoding="utf-8")
    outs = rc_thumb.generate(sd, Template(tdir), "none", "low", "指定")
    names = sorted(p.name for p in outs)
    check("人物: layouts に \"person\" と書いても作れる", any("person" in n for n in names)
          and not any("band" in n for n in names), str(names))


# ---------------------------------------------------------------- GUI
GUI_CODE = r'''
import os, sys, json, time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox
import gui, gui_ext_thumbspro as X
app = QApplication([])
res = {}
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
acts = [a.text() for a in w.tools_menu.actions()]
res["menu"] = acts
res["menu_visible"] = w.tools_menu.menuAction().isVisible()
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (IMG, ""))
QInputDialog.getText = staticmethod(lambda *a, **k: ("お試し\\n人物", True))
shown = []
def fake_exec(self):
    self.resize(1100, 560); self.show()
    for _ in range(10):
        app.processEvents()
    self.grab().save(str(Path(WORK) / "gui_try.png"))
    shown.append(len(self.findChildren(gui.ClickLabel)))
    self.close()
X.TryResult.exec = fake_exec
w.thumbspro_actions[1].trigger()
t0 = time.time()
while (w.runner.busy or not shown) and time.time() - t0 < 180:
    app.processEvents(); time.sleep(0.05)
res["try_tiles"] = shown[0] if shown else 0
res["log"] = w.runner.log.toPlainText()[-600:]
# サムネイル案の画面（人物デザインの列が増える）
w.review.load(Path(SESS))
td = gui.ThumbDialog(w.review, Path(SESS) / "thumbnails")
td.show()
for _ in range(10):
    app.processEvents()
td.grab().save(str(Path(WORK) / "gui_thumbs.png"))
res["thumb_tiles"] = len(td.findChildren(gui.ClickLabel))
td.reject()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, fx: Path, work: Path, sd: Path | None):
    img = work / "写真 テスト.jpg"
    _person_frame().save(img, quality=92)
    shutil.rmtree(work / "写真 テスト_人物デザイン", ignore_errors=True)
    code = (f"ROOT = {str(ROOT)!r}\nIMG = {str(img)!r}\nWORK = {str(work)!r}\nSESS = {str(sd or work)!r}\n"
            + GUI_CODE)
    md = work / "gui_models"
    md.mkdir(exist_ok=True)
    (md / "download_failed.txt").write_text("test", encoding="utf-8")
    env = dict(os.environ, APPDATA=str(fx / "appdata"), XDG_CONFIG_HOME=str(fx / "appdata"),
               PYTHONIOENCODING="utf-8", AVE_MODEL_DIR=str(md))
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("人物: GUI", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("人物: GUI", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("人物: GUIの「ツール」メニューに追加", r["menu_visible"] and any("人物の切り抜き" in a for a in r["menu"]),
          str(r["menu"]))
    check("人物: GUIから画像で試して、5種類を並べて表示", r["try_tiles"] == 5, f"{r['try_tiles']}枚 {r['log'][-300:]}")
    if sd:
        n = len(list((sd / "thumbnails").glob("thumb_*.jpg")))
        check("人物: サムネイル案の画面に人物デザインも並ぶ", r["thumb_tiles"] == n and n > 9, f"{r['thumb_tiles']}/{n}")


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    work = fx / "thumbspro"
    work.mkdir(parents=True, exist_ok=True)
    # テスト中は切り抜き用データをダウンロードしない（「失敗した直後」の状態にしておく）
    nomodel = work / "nomodel"
    nomodel.mkdir(exist_ok=True)
    (nomodel / "download_failed.txt").write_text("test", encoding="utf-8")
    old = os.environ.get("AVE_MODEL_DIR")
    os.environ["AVE_MODEL_DIR"] = str(nomodel)
    try:
        _run_all(check, out, fx, work)
    finally:
        if old is None:
            os.environ.pop("AVE_MODEL_DIR", None)
        else:
            os.environ["AVE_MODEL_DIR"] = old


def _run_all(check, out: Path, fx: Path, work: Path):
    if not t_faces(check, work, fx):
        return
    t_compose(check, work, fx)
    t_model(check, work, fx)
    sd = None
    if (out / "session.json").exists():
        sd = t_generate(check, out, fx, work)
        t_generate_ai(check, sd, fx, work)
        t_layout_list(check, sd, fx, work)
        t_no_cv2(check, sd, fx)
        # GUI の画面で見るために、ふつうの設定で作り直しておく
        import rc_thumb
        rc_thumb.generate(sd, _tpl(fx), "none", "low", "人物\\nテスト")
    else:
        check("人物: 動画からのサムネイル案（荒編集の結果が必要）", False, "先に tests/smoke_test.py を実行してください")
    t_gui(check, fx, work, sd)


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
