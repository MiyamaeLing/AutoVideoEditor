"""オープニング・エンディング・テロップの種類・章の進み具合のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_graphics.py         # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - カード型テロップ（吹き出し・スペック表・比較表・STEP・場所）の文字の読み取りと ASS への出力
  - 章の進み具合のバー（既定では出ない・有効にすると出る・3時間でも軽い）
  - 焼き込んだ画面の色（バーの塗り・カードの帯）を、FFmpeg で取り出した静止画で確かめる
  - オープニング（3秒）・エンディング（YouTube の終了画面の枠つき）の生成と、設定が同じなら作り直さないこと
  - finish.py で [opening] / [ending] / [progress] を有効にすると、完成動画に付く
  - 演出プランのスキーマと AI への説明に新しい種類が入り、偽の Claude の返答をそのまま使える
  - GUI: 表の種類の選択肢・ツールメニュー・見本のダイアログ
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable


def _run(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=900)


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _set(text: str, section: str, key: str, value: str) -> str:
    """template.toml の [section] の key の行を書き換える"""
    m = re.search(rf"^\[{re.escape(section)}\][^\n]*\n", text, re.M)
    assert m, section
    end = text.find("\n[", m.end())
    end = len(text) if end < 0 else end
    body = text[m.end():end]
    body, n = re.subn(rf"^{re.escape(key)} = .*$", f"{key} = {value}", body, count=1, flags=re.M)
    if not n:
        body = f"{key} = {value}\n" + body
    return text[:m.end()] + body + text[end:]


def _frame(video: Path, t: float, out: Path):
    from PIL import Image
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(out)],
                   check=True)
    return Image.open(out).convert("RGB")


def _near(px, rgb, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px, rgb))


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def _has_audio(p: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout.strip()
    return bool(out)


def _make_tpl(fx: Path, dst: Path) -> Path:
    """テスト用テンプレート：ロゴ・オープニング・エンディング（短め）・章の進み具合を有効にする"""
    from PIL import Image, ImageDraw
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(fx / "tpl", dst, ignore=shutil.ignore_patterns("generated", "_preview_work", "*.mp4", "*.png"))
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((20, 20, 380, 380), fill=(229, 62, 62, 255))
    d.rectangle((140, 120, 260, 280), fill=(255, 255, 255, 255))
    im.save(dst / "ロゴ 画像.png")   # 日本語とスペースを含む名前
    s = (dst / "template.toml").read_text(encoding="utf-8")
    s = _set(s, "logo", "file", '""')
    s = _set(s, "opening", "enabled", "true")
    s = _set(s, "opening", "logo", '"ロゴ 画像.png"')
    s = _set(s, "opening", "channel_name", '"テストちゃんねる"')
    s = _set(s, "ending", "enabled", "true")
    s = _set(s, "ending", "duration", "6")
    s = _set(s, "progress", "enabled", "true")
    (dst / "template.toml").write_text(s, encoding="utf-8")
    return dst


# ---------------------------------------------------------------- 文字の読み取り・ASS
def t_units(check):
    import rc_graphics as g
    from rc_ass import build_ass
    from rc_template import Template

    check("カード: 行の区切り", g.split_lines("見出し／重さ：1.2kg\n価格：1/2") == ["見出し", "重さ：1.2kg", "価格：1/2"])
    check("カード: 列の区切り", g.split_cells("｜A社|B社｜") == ["", "A社", "B社"])
    cases = [("STEP 2 穴をあける", 1, (2, "穴をあける")), ("ステップ３：仕上げ", 1, (3, "仕上げ")),
             ("穴をあける", 4, (4, "穴をあける")), ("STEP5", 1, (5, "")), ("1.2kgの板を切る", 7, (7, "1.2kgの板を切る"))]
    bad = [(t, g.parse_step(t, n)) for t, n, exp in cases if g.parse_step(t, n) != exp]
    check("カード: STEPの番号の読み取り", not bad, str(bad))

    tpl = Template(ROOT / "templates" / "default")
    telops = [(1, 4, "重さ 1.2kg", "emphasis"), (2, 5, "マキタ／重さ：1.2kg／価格：9,800円", "spec"),
              (6, 9, "｜A｜B／重さ｜1｜2", "compare"), (10, 12, "切る", "step"), (13, 15, "STEP 5 塗る", "step"),
              (16, 18, "削る", "step"), (19, 21, "高尾山／599m", "place"), (22, 24, "えっ！？", "bubble")]
    ass = build_ass(tpl, 1920, 1080, [(0, 1, "字幕")], telops, [(5.0, "章")])
    lines = ass.splitlines()
    check("ASS: カード用のスタイル", any(ln.startswith("Style: Card,") for ln in lines))
    check("ASS: カード型は T_ のスタイルを使わない", not any(f"T_{k}" in ass for k in g.CARD_STYLES))
    check("ASS: 今までの種類はそのまま", ",T_emphasis,," in ass and "重さ 1.2kg" in ass)
    check("ASS: 図形で描く", sum("\\p1" in ln for ln in lines) >= 10)
    steps = re.findall(r"\\fs76\\b1\\1c&HFFFFFF&\\bord0\\3c&H000000&\\shad0}(\d+)$", ass, re.M)
    check("ASS: STEP は順に数える（途中で番号を書いたらそこから）", steps == ["1", "5", "6"], str(steps))
    check("ASS: 進み具合は渡さなければ出ない", "\\clip" not in ass)
    check("ASS: どの種類の文字も入る", all(w in ass for w in ("マキタ", "重さ", "切る", "高尾山", "えっ！？")))
    ass2 = build_ass(tpl, 1920, 1080, [], [], [], progress=([(0, "a"), (30, "b")], 0, 60))
    check("進み具合: テンプレートの既定は無効", "\\clip" not in ass2)

    # 3時間・章200個でも軽い（イベントの数が章の数に比例するだけ）
    t0 = time.time()
    ev = g.progress_events(tpl, 1920, 1080, [(i * 54.0, f"章{i}") for i in range(200)], 0, 3 * 3600, force=True)
    check("進み具合: 3時間・200章でも軽い", len(ev) <= 200 * 4 + 1 and time.time() - t0 < 5,
          f"{len(ev)}件 {time.time() - t0:.2f}秒")
    # 塗りの右端は、章の始まり → 終わりの位置へ動く
    ev = g.progress_events(tpl, 1000, 500, [(10, "前半"), (60, "後半")], 10, 110, force=True)
    fills = [e for e in ev if "\\t(" in e]
    ok = len(fills) == 2 and "\\clip(0,0,0,8)\\t(0,50000,\\clip(0,0,500,8))" in fills[0] \
        and "\\clip(0,0,500,8)\\t(0,50000,\\clip(0,0,1000,8))" in fills[1]
    check("進み具合: 章ごとに塗りが進む", ok, "\n".join(fills)[:400])
    check("進み具合: 本編の頭から始まる", ev[0].split(",")[1] == "0:00:10.00", ev[0][:60])


# ---------------------------------------------------------------- 画面の色を確かめる
def t_render_colors(check, fx: Path, tpl_dir: Path):
    from rc_ass import build_ass
    from rc_template import Template
    tpl = Template(tpl_dir)
    work = fx / "graphics" / "colors"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copytree(tpl.fonts_dir, work / "fonts")
    telops = [(0.0, 4.0, "比較／重さ：1.2kg", "spec")]
    ass = build_ass(tpl, 1280, 720, [], telops, [], progress=([(0.0, "はじめ"), (4.0, "つぎ")], 0.0, 8.0))
    (work / "s.ass").write_text(ass, encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x404040:s=1280x720:r=25:d=8",
                    "-vf", "ass=s.ass:fontsdir=fonts", "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv444p",
                    "v.mp4"], cwd=work, check=True)
    im = _frame(work / "v.mp4", 2.0, work / "f2.png")
    yellow = (0xFF, 0xE1, 0x4D)
    # 2秒目 = 全体の 1/4 まで塗られている
    check("画面: バーの塗り（進んだ所）", _near(im.getpixel((200, 3)), yellow), str(im.getpixel((200, 3))))
    check("画面: バーの塗り（まだの所）", not _near(im.getpixel((500, 3)), yellow), str(im.getpixel((500, 3))))
    check("画面: 今の章の名前の箱", im.getpixel((20, 30))[0] < 0x40, str(im.getpixel((20, 30))))
    # スペック表は右寄せ。見出しの帯は青
    blues = [x for x in range(640, 1280) if _near(im.getpixel((x, 300)), (0x2B, 0x6C, 0xB0), 30)
             or _near(im.getpixel((x, 330)), (0x2B, 0x6C, 0xB0), 30)]
    check("画面: スペック表の見出しの帯", len(blues) > 100, f"{len(blues)}点")
    im = _frame(work / "v.mp4", 6.0, work / "f6.png")
    check("画面: 2つ目の章で塗りが進む", _near(im.getpixel((900, 3)), yellow) and
          not _near(im.getpixel((1200, 3)), yellow), f"{im.getpixel((900, 3))} {im.getpixel((1200, 3))}")


# ---------------------------------------------------------------- オープニング・エンディング
def t_clips(check, fx: Path, tpl_dir: Path):
    import rc_graphics as g
    from rc_template import Template
    p = _run(["graphics.py", "--template", str(tpl_dir)])
    gen = tpl_dir / g.GEN_DIR
    if p.returncode != 0:
        check("オープニング・エンディングの生成（graphics.py）", False, _tail(p))
        return
    intro, outro = gen / "intro.mp4", gen / "outro.mp4"
    check("オープニング: 3秒・音声つき", abs(_duration(intro) - 3.0) < 0.1 and _has_audio(intro), f"{_duration(intro):.2f}s")
    check("エンディング: 設定の長さ・音声つき", abs(_duration(outro) - 6.0) < 0.1 and _has_audio(outro),
          f"{_duration(outro):.2f}s")
    check("見本の静止画", (gen / "intro_preview.png").exists() and (gen / "outro_preview.png").exists())
    im = _frame(intro, 2.0, fx / "graphics" / "intro_2s.png")
    check("オープニング: ロゴが出る", _near(im.getpixel((960, int(1080 * 0.40) - 120)), (229, 62, 62), 50),
          str(im.getpixel((960, int(1080 * 0.40) - 120))))
    im = _frame(outro, 4.0, fx / "graphics" / "outro_4s.png")
    lay = g.ending_layout(1920, 1080, 2)
    x, y, bw, bh = lay["boxes"][0]
    check("エンディング: 終了画面の枠（左の動画）", _near(im.getpixel((int(x - 2), int(y + bh / 2))), (0xFF, 0xE1, 0x4D), 60),
          str(im.getpixel((int(x - 2), int(y + bh / 2)))))
    sx, sy, sr = lay["subscribe"]
    check("エンディング: チャンネル登録の丸", _near(im.getpixel((int(sx + sr - 4), int(sy))), (0xFF, 0xE1, 0x4D), 60),
          str(im.getpixel((int(sx + sr - 4), int(sy)))))
    guide = json.loads((gen / "outro_終了画面の枠.json").read_text(encoding="utf-8"))
    check("エンディング: 枠の位置の説明", len(guide["動画の枠"]) == 2 and guide["動画の長さ"] == 6.0)
    # 1920x1080 の枠は画面に収まり、重ならない
    (x1, y1, w1, h1), (x2, _y2, _w2, _h2) = lay["boxes"]
    check("エンディング: 枠が画面に収まり重ならない",
          x1 >= 0 and x2 + w1 <= 1920 and y1 + h1 <= 1080 and x1 + w1 < sx - sr and sx + sr < x2)

    # 設定が同じなら作り直さない・変えたら作り直す
    tpl = Template(tpl_dir)
    m0 = intro.stat().st_mtime
    g.ensure(tpl, "intro")
    check("自動生成: 設定が同じなら作り直さない", intro.stat().st_mtime == m0)
    s = (tpl_dir / "template.toml").read_text(encoding="utf-8")
    (tpl_dir / "template.toml").write_text(_set(s, "opening", "duration", "2.0"), encoding="utf-8")
    g.ensure(Template(tpl_dir), "intro")
    check("自動生成: 設定を変えたら作り直す", abs(_duration(intro) - 2.0) < 0.1, f"{_duration(intro):.2f}s")
    (tpl_dir / "template.toml").write_text(s, encoding="utf-8")

    # 既定のテンプレート（無効）では何もしない
    a, b = g.auto_intro_outro(Template(ROOT / "templates" / "default"), None, None)
    check("自動生成: 既定のテンプレートでは使わない", a is None and b is None)

    # テロップの見本
    p = _run(["graphics.py", "--template", str(tpl_dir), "--samples"])
    check("テロップの見本（graphics.py --samples）",
          p.returncode == 0 and len(list(gen.glob("telop_sample_*.png"))) == 4, _tail(p) if p.returncode else "")


# ---------------------------------------------------------------- finish.py で使う
def t_finish(check, out: Path, fx: Path, tpl_dir: Path):
    work = fx / "graphics" / "out"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(out, work, ignore=shutil.ignore_patterns("*.mp4", "work", "thumbnails", "*.png"))
    plan = {"telops": [{"start_id": 2, "end_id": 2, "text": "STEP 1 開封", "style": "step", "se": "pop"},
                       {"start_id": 6, "end_id": 6, "text": "マキタ／重さ：1.2kg／電圧：10.8V", "style": "spec",
                        "se": "none"},
                       {"start_id": 8, "end_id": 8, "text": "工房の作業台／自宅", "style": "place", "se": "none"},
                       {"start_id": 9, "end_id": 10, "text": "すごっ！", "style": "bubble", "se": "ding"},
                       {"start_id": 11, "end_id": 11, "text": "｜新｜旧／重さ｜1.2｜1.5", "style": "compare",
                        "se": "none"}],
        "zooms": [], "bgm": [{"start_id": 1, "mood": "upbeat"}]}
    (work / "plan_cards.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(work), "--template", str(tpl_dir), "--plan", str(work / "plan_cards.json"),
              "--llm", "none", "--no-thumbs", "--keep-work"])
    if p.returncode != 0:
        check("仕上げ: カード型テロップ・オープニング・エンディング", False, _tail(p))
        return
    info = json.loads((work / "final_info.json").read_text(encoding="utf-8"))
    mp4 = Path(info["video"])
    total = _duration(mp4)
    check("仕上げ: オープニングの分だけずらす", abs(info["intro_offset"] - 3.0) < 0.05, str(info["intro_offset"]))
    check("仕上げ: エンディングも付く", abs(total - info["duration"]) < 0.2 and info["duration"] > 3 + 6 + 5,
          f"{total:.2f}s / {info['duration']}")
    ass = (work / "work" / "subs.ass").read_text(encoding="utf-8")
    check("仕上げ: カード型テロップを焼き込む", "\\p1" in ass and "STEP" in ass and "マキタ" in ass and "工房の作業台" in ass)
    check("仕上げ: 章の進み具合を出す", "\\t(0," in ass and "\\clip(" in ass)
    first = min(float(re.match(r"(\d+):(\d+):(\d+\.\d+)", ln.split(",")[1]).group(3))
                + 60 * int(ln.split(",")[1].split(":")[1]) for ln in ass.splitlines()
                if ln.startswith("Dialogue: 6,"))
    check("仕上げ: 進み具合はオープニングの後から", abs(first - 3.0) < 0.05, str(first))
    im = _frame(mp4, 1.5, fx / "graphics" / "final_intro.png")
    check("仕上げ: 最初はオープニング（ロゴ）", _near(im.getpixel((960, int(1080 * 0.40) - 120)), (229, 62, 62), 60),
          str(im.getpixel((960, int(1080 * 0.40) - 120))))
    body_end = info["duration"] - 6.0
    im = _frame(mp4, 3.0 + (body_end - 3.0) * 0.6, fx / "graphics" / "final_body.png")
    check("仕上げ: 本編の上に進み具合のバー", _near(im.getpixel((200, 3)), (0xFF, 0xE1, 0x4D), 50),
          str(im.getpixel((200, 3))))
    im = _frame(mp4, info["duration"] - 2.0, fx / "graphics" / "final_outro.png")
    check("仕上げ: 最後はエンディング（バーは出ない）", not _near(im.getpixel((200, 3)), (0xFF, 0xE1, 0x4D), 50)
          and _near(im.getpixel((148, 380 + 170)), (0xFF, 0xE1, 0x4D), 70), str(im.getpixel((148, 550))))
    shutil.rmtree(work / "work", ignore_errors=True)

    # 試し書き出しではオープニングを作らない・付けない
    p = _run(["finish.py", str(work), "--template", str(tpl_dir), "--plan", str(work / "plan_cards.json"),
              "--llm", "none", "--preview", "5", "--no-thumbs"])
    prev = work / f"{json.loads((work / 'session.json').read_text(encoding='utf-8'))['stem']}_プレビュー.mp4"
    check("仕上げ: 試し書き出しはオープニングなし", p.returncode == 0 and abs(_duration(prev) - 5.0) < 0.3,
          _tail(p) if p.returncode else f"{_duration(prev):.2f}s")


# ---------------------------------------------------------------- 演出プラン（偽の Claude）
def t_plan_llm(check):
    import anthropic
    import httpx2
    import rc_llm
    import rc_plan

    enum = rc_plan.PLAN_SCHEMA["properties"]["telops"]["items"]["properties"]["style"]["enum"]
    check("プラン: スキーマに新しい種類", all(k in enum for k in ("bubble", "spec", "compare", "step", "place")))
    instr = rc_plan._instructions("craft", ["calm"])
    check("プラン: AIへの説明に書き方", all(w in instr for w in ("spec=", "compare=", "step=", "place=", "bubble=", "｜")))

    reply = {"telops": [{"start_id": 1, "end_id": 2, "text": "STEP 1 材料を切る", "style": "step", "se": "none"},
                        {"start_id": 2, "end_id": 2, "text": "ドリル／重さ：1.2kg", "style": "spec", "se": "pop"}],
             "zooms": [], "bgm": [{"start_id": 1, "mood": "calm"}]}
    seen = {}

    def sse(ev, data):
        return f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def handler(request):
        seen["body"] = json.loads(request.content.decode("utf-8"))
        text = json.dumps(reply, ensure_ascii=False)
        body = "".join([
            sse("message_start", {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": rc_llm.MODEL, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}}),
            sse("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": text}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                  "usage": {"output_tokens": 50}}),
            sse("message_stop", {"type": "message_stop"}),
        ])
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    orig = rc_llm._client
    rc_llm._client = lambda: (anthropic, client)
    try:
        plan, usage = rc_plan.make_plan_api([(1, 0.0, 2.0, "まず材料を切ります"), (2, 2.0, 4.0, "重さは1.2キロ")],
                                            "craft", ["calm"], "low")
    except Exception as e:  # noqa: BLE001
        check("プラン: 偽の Claude の返答", False, repr(e))
        return
    finally:
        rc_llm._client = orig
    plan = rc_plan.sanitize(plan, {1, 2}, ["calm"])
    sent_enum = seen["body"]["output_config"]["format"]["schema"]["properties"]["telops"]["items"]["properties"][
        "style"]["enum"]
    check("プラン: 偽の Claude の返答をそのまま使う", [t["style"] for t in plan["telops"]] == ["step", "spec"]
          and "spec" in sent_enum and usage["in"] == 100, str(plan))


# ---------------------------------------------------------------- GUI（画面なしで別プロセス）
GUI_CODE = r'''
import os, sys, json
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
res = {"ext": "gui_ext_graphics" in w.extensions,
       "menu": [a.text() for a in w.tools_menu.actions()],
       "menu_visible": w.tools_menu.menuAction().isVisible()}
w.review.load(Path(OUT))
rv = w.review
w.tabs.setCurrentWidget(rv)
rv.tabs.setCurrentIndex(1)
combo = rv.t_telop.cellWidget(0, 3)
res["styles"] = [combo.itemData(i) for i in range(combo.count())]
res["labels"] = [combo.itemText(i) for i in range(combo.count())]
combo.setCurrentIndex(combo.findData("spec"))
rv.t_telop.item(0, 2).setText("ドリル／重さ：1.2kg")
res["collected"] = rv._collect_plan()["telops"][0]
res["tooltip"] = "比較表" in rv.t_telop.toolTip()
w.grab().save(SHOT1)
import gui_ext_graphics as ge
gen = Path(TPL) / "generated"
d = ge.GraphicsDialog(w, "見本", [gen / f"telop_sample_{i}.png" for i in range(1, 5)],
                      [("動画で見る", gen / "telop_samples.mp4")], Path(TPL), ge.HINT)
d.show(); app.processEvents()
d.grab().save(SHOT2)
d2 = ge.GraphicsDialog(w, "オープニング・エンディング", [gen / "intro_preview.png", gen / "outro_preview.png"], [], Path(TPL))
d2.show(); app.processEvents()
d2.grab().save(SHOT3)
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, fx: Path, tpl_dir: Path):
    gdir = fx / "graphics"
    code = (f"ROOT = {str(ROOT)!r}\nOUT = {str(out)!r}\nTPL = {str(tpl_dir)!r}\n"
            f"SHOT1 = {str(gdir / 'gui_review.png')!r}\nSHOT2 = {str(gdir / 'gui_samples.png')!r}\n"
            f"SHOT3 = {str(gdir / 'gui_clips.png')!r}\n" + GUI_CODE)
    env = dict(os.environ, APPDATA=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"), PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("GUI(graphics): 起動", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
    if not line:
        check("GUI(graphics): 起動", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("GUI(graphics): 拡張を読み込む", r["ext"] and r["menu_visible"] and len(r["menu"]) >= 2, str(r["menu"]))
    check("GUI(graphics): テロップの種類の選択肢", all(k in r["styles"] for k in ("bubble", "spec", "compare", "step", "place"))
          and "比較表" in r["labels"], str(r["labels"]))
    check("GUI(graphics): 選んだ種類を保存", r["collected"]["style"] == "spec"
          and r["collected"]["text"] == "ドリル／重さ：1.2kg", str(r["collected"]))
    check("GUI(graphics): 書き方のヒント", r["tooltip"])
    check("GUI(graphics): 画面の画像", all((gdir / n).exists() for n in ("gui_review.png", "gui_samples.png",
                                                                       "gui_clips.png")))


def run_all(check, out: Path, fx: Path):
    (fx / "graphics").mkdir(exist_ok=True)
    t_units(check)
    t_plan_llm(check)
    tpl_dir = _make_tpl(fx, fx / "graphics" / "tpl")
    t_render_colors(check, fx, tpl_dir)
    t_clips(check, fx, tpl_dir)
    if not (out / "session.json").exists():
        check("graphics のテスト（荒編集の結果が必要）", False, "先に荒編集が成功している必要があります")
        return
    t_finish(check, out, fx, tpl_dir)
    t_gui(check, out, fx, tpl_dir)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    if not (out / "session.json").exists():
        print("（先に tests/smoke_test.py を実行すると、全部のテストができます）")
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
