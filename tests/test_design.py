"""テンプレートの編集画面とデザイン案のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_design.py           # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - template.toml の書き出し（読み直すと同じ値・コメント付き・変な文字も壊れない）
  - フォントの一覧（ファイルの中の名前を読む。日本語名でも探せる）
  - 見本画像（字幕・テロップ4種・章カード・サムネイル）。色や位置を変えると、その所だけ変わる
  - 日本語・空白を含むフォルダ、自分の動画の1コマを背景にする
  - AIのデザイン案（偽の通信で Claude の返事を返す）。おかしな値は直す・フォントは一覧の中だけ
  - design.py（AIなし・claude.ai 貼り付け・返答の読み込み・見本）
  - 別名で保存（BGM などを複製し、そのまま finish.py の見本に使える）
  - 画面（編集・上書き保存・別名で保存・3案の比較と保存）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable


def _run(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=600)


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _diff(a: Path, b: Path) -> float:
    import numpy as np
    from PIL import Image
    with Image.open(a) as x, Image.open(b) as y:
        return float(np.abs(np.asarray(x.convert("RGB"), dtype=np.int16)
                            - np.asarray(y.convert("RGB"), dtype=np.int16)).mean())


# ---------------------------------------------------------------- TOML
def t_toml(check, fx: Path):
    import tomllib

    import rc_design
    from rc_template import Template

    ok = True
    for d in (ROOT / "templates" / "default", fx / "tpl"):
        cfg = rc_design.load_cfg(d)
        s = rc_design.dump_toml(cfg)
        ok = ok and tomllib.loads(s) == cfg
    check("デザイン: template.toml の書き出し（読み直すと同じ値）", ok)
    check("デザイン: 値の意味をコメントで残す", "# 縁取りの色" in s and "# ---- 字幕（画面下） ----" in s)
    tricky = {"caption": {"font": 'A "q" \\ b\tc', "size": 60, "bold": False, "x": 1.0, "y": 2.5e-3,
                          "list": ["a", 1, True]},
              "telop": {"emphasis": {"font": "日本語 フォント", "color": "#FFFFFF"}},
              "空白 キー": {"k": "改行\nあり"}}
    check("デザイン: 特殊な文字も壊れない", tomllib.loads(rc_design.dump_toml(tricky)) == tricky)
    d = fx / "design" / "toml"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(fx / "tpl", d, ignore=shutil.ignore_patterns(rc_design.WORK_NAME))
    cfg = rc_design.load_cfg(d)
    cfg["telop"]["emphasis"]["color"] = "#00FF00"
    rc_design.save_template(cfg, d, d)
    check("デザイン: 上書き保存（前の設定は .bak に残す）",
          Template(d)["telop"]["emphasis"]["color"] == "#00FF00" and (d / "template.toml.bak").exists())


# ---------------------------------------------------------------- フォント
def t_fonts(check, fx: Path):
    import rc_design
    import make_fixtures

    jp = make_fixtures.find_jp_font()
    faces = rc_design.read_font_file(jp)
    check("デザイン: フォントの名前を読む", bool(faces) and faces[0].japanese, str(faces[:1]))
    all_faces = rc_design.scan_fonts([fx / "tpl" / "fonts"])
    fams = rc_design.font_families(all_faces)
    names = [f["name"] for f in fams]
    check("デザイン: 日本語フォントの一覧", faces[0].family in names, f"{len(names)}種類")
    other = next((n for fc in faces for n in fc.names if n != fc.family), None)
    if other:   # 日本語名（例: IPAゴシック）でも同じフォントが見つかる
        hit = rc_design.find_face(all_faces, other)
        check("デザイン: 日本語の名前でもフォントを探せる", hit is not None and hit.family == faces[0].family, other)
    cfg = rc_design.load_cfg(fx / "tpl")
    check("デザイン: サムネイルのフォント名を読む", rc_design.thumbnail_family(cfg, fx / "tpl") == faces[0].family)
    cfg["thumbnail"]["font_file"] = "C:/Windows/Fonts/ない.ttc"
    check("デザイン: サムネイルのフォントがないときは None", rc_design.thumbnail_family(cfg, fx / "tpl") is None)


# ---------------------------------------------------------------- 見本画像
def t_preview(check, fx: Path) -> Path | None:
    from PIL import Image

    import rc_design

    # 日本語と空白を含むフォルダ（Windows の「ドキュメント」の下などを想定）
    tdir = fx / "design" / "テンプレ 日本語"
    if tdir.exists():
        shutil.rmtree(tdir)
    shutil.copytree(fx / "tpl", tdir, ignore=shutil.ignore_patterns(rc_design.WORK_NAME))
    faces = rc_design.scan_fonts([tdir / "fonts"])
    cfg = rc_design.load_cfg(tdir)
    out = fx / "design" / "preview"
    if out.exists():
        shutil.rmtree(out)
    try:
        a = rc_design.render_preview(cfg, tdir, out_dir=out / "a", faces=faces)
    except Exception as e:  # noqa: BLE001
        check("デザイン: 見本画像を作る", False, str(e))
        return None
    sizes = {k: Image.open(v).size for k, v in a.items() if k != "grid"}
    check("デザイン: 見本画像を作る（字幕・テロップ4種・章カード・サムネイル）",
          len(sizes) == len(rc_design.PANELS) and set(sizes.values()) == {(960, 540)}, str(sizes))

    cfg2 = json.loads(json.dumps(cfg))
    cfg2["telop"]["emphasis"]["color"] = "#00FF00"
    cfg2["telop"]["warning"]["margin_v"] = 300
    b = rc_design.render_preview(cfg2, tdir, out_dir=out / "b", faces=faces)
    d_emph, d_info = _diff(a["emphasis"], b["emphasis"]), _diff(a["info"], b["info"])
    check("デザイン: 色を変えると、その見本だけ変わる", d_emph > 0.05 and d_info < 0.01,
          f"強調 {d_emph:.3f} / 補足 {d_info:.3f}")
    check("デザイン: 位置（端からの距離）が見本に反映される", _diff(a["warning"], b["warning"]) > 0.05)

    import numpy as np
    with Image.open(a["emphasis"]) as im:
        top = np.asarray(im.convert("RGB"))[20:110, 300:660].astype(int)
    yellow = ((top[..., 0] > 200) & (top[..., 1] > 190) & (top[..., 2] < 120)).mean()
    check("デザイン: 強調テロップが本番と同じ大きさ・色で出る（動きの途中で撮っていない）", yellow > 0.03,
          f"黄色の割合 {yellow:.3f}")

    # 自分の素材（日本語・空白入りの名前の動画）の1コマを背景に
    vid = fx / "design" / "素材 テスト.MP4"
    shutil.copy(fx / "C0001.MP4", vid)
    try:
        frame = rc_design.extract_frame(vid, 12.0, tdir / rc_design.WORK_NAME / "bg_frame.png")
        c = rc_design.render_preview(cfg, tdir, frame, out_dir=out / "c", faces=faces)
        check("デザイン: 自分の動画の1コマを背景にする", _diff(a["info"], c["info"]) > 5)
    except Exception as e:  # noqa: BLE001
        check("デザイン: 自分の動画の1コマを背景にする", False, str(e))
    try:
        rc_design.extract_frame(vid, 9999, tdir / rc_design.WORK_NAME / "bg_far.png")
        check("デザイン: 動画より後ろの時間は分かりやすく止める", False)
    except RuntimeError as e:
        check("デザイン: 動画より後ろの時間は分かりやすく止める", "取り出せません" in str(e), str(e))
    return a["grid"]


# ---------------------------------------------------------------- AI（偽の通信）
def _sse(text: str) -> bytes:
    msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}
    ev = [("message_start", {"type": "message_start", "message": msg}),
          ("content_block_start", {"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "text", "text": ""}})]
    for i in range(0, len(text), 400):
        ev.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": text[i:i + 400]}}))
    ev += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
           ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                              "usage": {"output_tokens": 900}}),
           ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for e, d in ev).encode("utf-8")


def fake_answer(font: str) -> dict:
    def tel(color, look, accent, pos="top", anim="pop", size=96):
        return {"font": font, "size": size, "color": color, "look": look, "accent_color": accent, "outline": 9,
                "position": pos, "animation": anim}
    good = {"name": "ガジェット", "concept": "黒い帯と水色。", "caption": {"font": font, "size": 64, "color": "#FFFFFF",
                                                                   "outline_color": "#101010", "outline": 6},
            "telops": {"emphasis": tel("#5CE1FF", "outline", "#0B0F17"), "info": tel("#FFFFFF", "box", "#11161F",
                                                                                     "top-left", "slide", 52),
                       "warning": tel("#FFD23F", "box", "#1A1A1A"), "funny": tel("#FFFFFF", "outline", "#7B3FE4")},
            "chapter_card": {"font": font, "size": 90, "color": "#5CE1FF", "box_color": "#0B0F17"},
            "thumbnail": {"font": font, "text_color": "#5CE1FF", "outline_color": "#000000", "outer_color": "#FFFFFF",
                          "accent_color": "#11161F"}}
    bad = json.loads(json.dumps(good))
    bad.update(name="おかしな値", concept="直されるはず")
    bad["caption"].update(font="存在しないフォント", size=999, color="red", outline=-3)
    bad["telops"]["emphasis"].update(look="box", accent_color="12AB34", position="右下", animation="spin")
    third = json.loads(json.dumps(good))
    third.update(name="ポップ", concept="黄色とピンク")
    third["telops"]["emphasis"].update(color="#FFE14D", accent_color="#E0457B")
    return {"proposals": [good, bad, third]}


def t_ai(check, fx: Path):
    import anthropic
    import httpx2

    import rc_design
    import rc_llm
    import make_fixtures

    faces = rc_design.scan_fonts([fx / "tpl" / "fonts"])
    font = rc_design.read_font_file(make_fixtures.find_jp_font())[0].family
    names = [f["name"] for f in rc_design.font_families(faces)]
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content.decode("utf-8"))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=_sse(json.dumps(fake_answer(font), ensure_ascii=False)))

    orig = rc_llm._client
    rc_llm._client = lambda: (anthropic, anthropic.Anthropic(
        api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    try:
        base = rc_design.load_cfg(fx / "tpl")
        props, usage = rc_design.propose_api("落ち着いた・ガジェット系", base, names, "low")
    except Exception as e:  # noqa: BLE001
        check("デザイン: AIのデザイン案（偽の通信）", False, repr(e))
        return
    finally:
        rc_llm._client = orig
    body = seen.get("body", {})
    schema = body.get("output_config", {}).get("format", {}).get("schema", {})
    font_enum = schema["properties"]["proposals"]["items"]["properties"]["caption"]["properties"]["font"].get("enum")
    user = body.get("messages", [{}])[0].get("content", "")
    check("デザイン: AIのデザイン案（偽の通信）", len(props) == 3 and usage["in"] == 1200, f"{len(props)}案")
    check("デザイン: Claude への依頼（モデル・構造化出力・フォント一覧・雰囲気）",
          body.get("model") == rc_llm.MODEL and font_enum == names and "落ち着いた・ガジェット系" in user
          and body.get("thinking", {}).get("type") == "adaptive", str(font_enum)[:120])

    cfgs = [rc_design.apply_proposal(base, p, names, faces, fx / "tpl") for p in props]
    g, b = cfgs[0], cfgs[1]
    check("デザイン: 案をテンプレートに当てはめる",
          g["telop"]["info"].get("box_color") == "#11161F" and "box_color" not in g["telop"]["emphasis"]
          and g["telop"]["emphasis"]["outline_color"] == "#0B0F17" and g["chapter_card"]["color"] == "#5CE1FF"
          and g["video"] == base["video"] and g["bgm"] == base["bgm"])
    check("デザイン: おかしな値は直す（フォントは一覧の中だけ）",
          b["caption"]["font"] == base["caption"]["font"] and b["caption"]["size"] == 110
          and b["caption"]["color"] == base["caption"]["color"] and b["caption"]["outline"] == 0
          and b["telop"]["emphasis"]["box_color"] == "#12AB34"
          and b["telop"]["emphasis"]["position"] == base["telop"]["emphasis"].get("position", "top")
          and b["telop"]["emphasis"]["animation"] == base["telop"]["emphasis"].get("animation", "pop"),
          json.dumps(b["caption"], ensure_ascii=False))
    th = g["thumbnail"]
    check("デザイン: サムネイルのフォントをファイルに直す", th.get("font_file", "").startswith("fonts/")
          or Path(th.get("font_file", "")).exists(), th.get("font_file", ""))
    check("デザイン: 書き出しても読み直せる", __import__("tomllib").loads(rc_design.dump_toml(g)) == g)
    (fx / "design").mkdir(exist_ok=True)
    (fx / "design" / "claude_design.json").write_text(
        "了解です。\n```json\n" + json.dumps(fake_answer(font), ensure_ascii=False) + "\n```", encoding="utf-8")


# ---------------------------------------------------------------- design.py
def t_cli(check, fx: Path):
    import rc_design
    from rc_template import Template

    tdir = fx / "design" / "テンプレ 日本語"
    if not tdir.exists():
        shutil.copytree(fx / "tpl", tdir, ignore=shutil.ignore_patterns(rc_design.WORK_NAME))
    out = fx / "design" / "cli"
    if out.exists():
        shutil.rmtree(out)
    p = _run(["design.py", "propose", "--template", str(tdir), "--mood", "ポップ", "--llm", "none", "--out", str(out)])
    data = json.loads((out / "proposals.json").read_text(encoding="utf-8")) if p.returncode == 0 else {}
    props = data.get("proposals", [])
    check("デザイン: design.py propose（AIなし）", len(props) == 3 and all(
        (out / x["images"]["grid"]).exists() for x in props) and props[0]["name"] == "ポップなビタミン",
        _tail(p) if p.returncode else "")
    p = _run(["design.py", "propose", "--template", str(tdir), "--mood", "落ち着いた", "--llm", "manual",
              "--out", str(out)])
    prompt = out / "prompt_design.md"
    check("デザイン: claude.ai に貼るプロンプト", p.returncode == 0 and prompt.exists() and not (out / "proposals.json").exists()
          and "使えるフォント" in prompt.read_text(encoding="utf-8"), _tail(p) if p.returncode else "")
    ans = fx / "design" / "claude_design.json"
    if ans.exists():
        p = _run(["design.py", "propose", "--template", str(tdir), "--answer", str(ans), "--out", str(out),
                  "--bg", str(fx / "C0002.MP4"), "--at", "4"])
        data = json.loads((out / "proposals.json").read_text(encoding="utf-8")) if p.returncode == 0 else {}
        check("デザイン: claude.ai の返答を読み込む（```json 付きでも）",
              [x["name"] for x in data.get("proposals", [])] == ["ガジェット", "おかしな値", "ポップ"],
              _tail(p) if p.returncode else "")
    bad = fx / "design" / "bad_answer.txt"
    bad.write_text("ごめんなさい、できません", encoding="utf-8")
    p = _run(["design.py", "propose", "--template", str(tdir), "--answer", str(bad), "--out", str(out)])
    check("デザイン: 読めない返答は分かりやすく止める", p.returncode != 0 and "[エラー]" in p.stdout + p.stderr)
    png = fx / "design" / "見本 一覧.png"
    p = _run(["design.py", "preview", "--template", str(tdir), "--sample", "dark", "--out", str(png)])
    check("デザイン: design.py preview", p.returncode == 0 and png.exists(), _tail(p) if p.returncode else "")

    # 別名で保存 → finish.py の見本にそのまま使える
    check("デザイン: 使えない名前を断る", rc_design.check_name("a/b") and rc_design.check_name("CON")
          and rc_design.check_name("  ") and rc_design.check_name("_x") and not rc_design.check_name("新しい見た目 2"))
    if props:
        dest = fx / "design" / "templates" / "ポップ 案"
        if dest.exists():
            shutil.rmtree(dest)
        faces = rc_design.scan_fonts([tdir / "fonts"])
        rc_design.save_template(props[0]["cfg"], tdir, dest, faces)
        t = Template(dest)
        bgm = t.bgm_files()
        check("デザイン: 別名で保存（BGM・フォントも複製、作業ファイルは入れない）",
              t["telop"]["emphasis"]["color"] == "#FFE14D" and bgm["upbeat"] and (dest / "fonts").is_dir()
              and not (dest / rc_design.WORK_NAME).exists())
        p = _run(["finish.py", "--template-preview", "--template", str(dest)])
        check("デザイン: 保存したテンプレートで見本動画を作れる", p.returncode == 0 and (dest / "preview_2.png").exists(),
              _tail(p) if p.returncode else "")


# ---------------------------------------------------------------- 「このユーザーだけ」のフォント（Windows を想定）
def t_user_font(check, fx: Path):
    import rc_design
    import make_fixtures

    d = fx / "design" / "userfont"
    if d.exists():
        shutil.rmtree(d)
    (d / "LocalFonts").mkdir(parents=True)
    src = make_fixtures.find_jp_font()
    uf = d / "LocalFonts" / ("my_" + src.name)
    shutil.copy(src, uf)
    faces = rc_design.read_font_file(uf)
    for fc in faces:
        fc.user_font = True     # %LOCALAPPDATA%\Microsoft\Windows\Fonts に入っていることにする
    base = d / "base"
    shutil.copytree(fx / "tpl", base, ignore=shutil.ignore_patterns(rc_design.WORK_NAME, "fonts"))
    (base / "fonts").mkdir()
    cfg = rc_design.load_cfg(base)
    cfg["caption"]["font"] = faces[0].family
    cfg["thumbnail"]["font_file"] = uf.as_posix()
    rc_design.render_preview(cfg, base, faces=faces)
    in_work = (base / rc_design.WORK_NAME / "fonts" / uf.name).exists()
    dest = d / "保存先"
    rc_design.save_template(cfg, base, dest, faces)
    import tomllib
    saved = tomllib.loads((dest / "template.toml").read_text(encoding="utf-8"))
    check("デザイン: ユーザー用フォントを見本と保存先の fonts にコピーする",
          in_work and (dest / "fonts" / uf.name).exists() and saved["thumbnail"]["font_file"] == f"fonts/{uf.name}",
          saved["thumbnail"]["font_file"])


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import os, sys, json, time, shutil
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog, QPushButton
import gui, rc_design
rc_design.TEMPLATES_DIR = Path(TPLROOT)
app = QApplication([])
res = {}
shown = []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warning", a[2] if len(a) > 2 else "")))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[2] if len(a) > 2 else "")))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
names = []
QInputDialog.getText = staticmethod(lambda *a, **k: (names.pop(0), True) if names else ("", False))
out = Path(OUT)
w = gui.MainWindow(); w.resize(1200, 820); w.show()
import gui_ext_design as g
res["ext"] = "gui_ext_design" in w.extensions
row = [b.text() for b in w.create.findChildren(QPushButton)]
res["buttons"] = "編集…" in row and "AIでデザイン案…" in row

def wait(cond, sec=60):
    t = time.time()
    while not cond() and time.time() - t < sec:
        app.processEvents(); time.sleep(0.03)
    for _ in range(5):
        app.processEvents()

ed = g.TemplateEditor(w, Path(TDIR)); ed.show()
idle = lambda: not ed.renderer.running and not ed.timer.isActive() and bool(ed.images)
wait(idle)
res["first"] = bool(ed.images) and "作れません" not in ed.status.text()
# 強調の色を変える → 見本が作り直され、まだ保存していない状態になる
ed.tabs.setCurrentIndex(1)
f = ed.forms[1]
before = ed.images["emphasis"].stat().st_mtime_ns
f.w["color"].set_value("#33DD66"); f.w["color"].changed.emit("#33DD66")
f.w["size"].setValue(120)
f.r_box.setChecked(True)
wait(lambda: idle() and ed.images["emphasis"].stat().st_mtime_ns != before)
res["dirty"] = ed.dirty() and "●" in ed.windowTitle()
res["cfg"] = ed.cfg["telop"]["emphasis"]
res["box_enabled"] = f.w["box_color"].isEnabled() and not f.w["outline"].isEnabled()
res["status"] = ed.status.text()
ed.grab().save(str(out / "gui_design_editor.png"))
# サムネイルのタブ
ed.tabs.setCurrentIndex(len(ed.forms) - 1)
wait(idle)
res["thumb_panel"] = ed.panel == "thumb"
ed.grab().save(str(out / "gui_design_editor_thumb.png"))
# 上書き保存
ok = ed.save()
import tomllib
saved = tomllib.loads((Path(TDIR) / "template.toml").read_text(encoding="utf-8"))
res["saved"] = ok and saved["telop"]["emphasis"]["color"] == "#33DD66" and saved["telop"]["emphasis"].get("box_color") \
    and not ed.dirty()
# 別名で保存（使えない名前 → 正しい名前）
names[:] = ["だめ/名前", "保存テスト"]
ed.forms[0].w["size"].setValue(70)
ok = ed.save_as()
res["saveas"] = ok and (Path(TPLROOT) / "保存テスト" / "template.toml").exists() and ed.tdir.name == "保存テスト" \
    and any("使えません" in s or "使えない" in s or "/" in s for _k, s in shown if _k == "warning")
ed.close()

# AIにデザイン案（作成タブで「AIを使わない」→ 用意した案）
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
dlg = g.ProposeDialog(w, Path(TDIR), rc_design.load_cfg(Path(TDIR))); dlg.resize(1400, 860); dlg.show()
dlg.mood.setText("ガジェット系")
dlg.run()
wait(lambda: not w.runner.busy, 120)
wait(lambda: False, 0.3)
res["cards"] = len(dlg.proposals)
res["names"] = [p["name"] for p in dlg.proposals]
dlg._show("info")
for _ in range(10):
    app.processEvents()
dlg.grab().save(str(out / "gui_design_propose.png"))
names[:] = ["ガジェット案"]
dlg.save(0)
d = Path(TPLROOT) / "ガジェット案"
res["propose_saved"] = (d / "template.toml").exists() and \
    tomllib.loads((d / "template.toml").read_text(encoding="utf-8"))["chapter_card"]["box_color"] == "#0B0F17"
dlg.tune(1)
ed2 = w._design_windows[-1]
wait(lambda: not ed2.renderer.running and not ed2.timer.isActive() and bool(ed2.images))
res["tune_new"] = ed2.is_new and ed2.dirty() and not ed2.b_save.isEnabled()
ed2.grab().save(str(out / "gui_design_tune.png"))
ed2.saved = ed2.cfg
ed2.close()
dlg.close()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, fx: Path):
    import rc_design
    base = fx / "design" / "gui"
    if base.exists():
        shutil.rmtree(base)
    tplroot = base / "templates"
    tdir = tplroot / "マイ テンプレ"
    shutil.copytree(fx / "tpl", tdir, ignore=shutil.ignore_patterns(rc_design.WORK_NAME))
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(base)!r}\nTDIR = {str(tdir)!r}\nTPLROOT = {str(tplroot)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=600)
    except subprocess.TimeoutExpired:
        check("デザイン画面: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("デザイン画面: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("デザイン画面: 拡張が読み込まれ、作成タブにボタンが出る", r["ext"] and r["buttons"])
    check("デザイン画面: 開くと見本ができる", r["first"])
    c = r["cfg"]
    check("デザイン画面: 色・大きさ・帯を変えると設定と見本が変わる",
          r["dirty"] and c["color"] == "#33DD66" and c["size"] == 120 and "box_color" in c and r["box_enabled"],
          json.dumps(c, ensure_ascii=False) + " " + r["status"])
    check("デザイン画面: サムネイルのタブで見本も切り替わる", r["thumb_panel"])
    check("デザイン画面: 上書き保存", r["saved"])
    check("デザイン画面: 別名で保存（使えない名前は聞き直す）", r["saveas"])
    check("デザイン画面: 3つの案を並べて比べる", r["cards"] == 3, str(r["names"]))
    check("デザイン画面: 選んだ案を新しいテンプレートとして保存", r["propose_saved"])
    check("デザイン画面: 案を細かく調整（保存までは元のテンプレートを書き換えない）", r["tune_new"])


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    (fx / "design").mkdir(exist_ok=True)
    t_toml(check, fx)
    t_fonts(check, fx)
    t_preview(check, fx)
    t_ai(check, fx)
    t_cli(check, fx)
    t_user_font(check, fx)
    t_gui(check, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
