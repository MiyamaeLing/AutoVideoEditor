"""機能どうしの組み合わせのテスト（統合ブランチ用）。

- 書き出し: 早送り（speedup）× 2台カメラ（multicam）× Bロール（broll）× 声と映像の補正（enhance）
- テロップの新しい種類（graphics）が、AIの指示・GUIの選択肢・早送り用の指示にも入っているか
- 見た目テンプレート: 全機能のセクションがそろい、テンプレート編集画面（design）で保存し直しても消えないか
- 演出プラン: 各機能が足した項目（angles / broll / speedups）が正しく引き継がれるか

単体: python tests/test_integration.py（tests/smoke_test.py からも呼ばれます）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PY = sys.executable


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
    return subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=1200)


def _tail(p: subprocess.CompletedProcess, n: int = 15) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def _gray(path: Path, t: float, w: int = 192, h: int = 108, crop: str = ""):
    import numpy as np
    vf = (crop + "," if crop else "") + f"scale={w}:{h}"
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.uint8).astype(np.float32)


def _has_filter(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return f" {name} " in out


# ---------------------------------------------------------------- 単体
def t_units(check):
    import rc_broll
    import rc_plan
    import rc_render
    import rc_speedup
    from rc_graphics import CARD_STYLES

    # 区切りを分けるとき、早送りの倍率と2台目の開始位置も一緒にずらす
    s = rc_render.Seg(Path("/m/A.mp4"), 10.0, 60, speed=4.0, video=Path("/m/phone.mp4"), video_start=3.0)
    a, b = rc_render.sub_seg(s, 0, 30, Fraction(30)), rc_render.sub_seg(s, 30, 60, Fraction(30))
    check("組み合わせ: 早送り×2台目の区切りを分ける", (a.start, a.video_start, a.frames) == (10.0, 3.0, 30)
          and abs(b.start - 14.0) < 1e-9 and abs(b.video_start - 7.0) < 1e-9 and b.speed == 4.0,
          f"{a.start},{a.video_start} / {b.start},{b.video_start}")

    # 入力の番号: 本編 → 2台目 → （最後に）Bロール
    ov = rc_broll.Overlay(src=Path("/m/photo.jpg"), kind="image", mode="full", item_frames=30, tw=640, th=360)
    segs = [rc_render.Seg(Path("/m/A.mp4"), 0.0, 30),
            rc_render.Seg(Path("/m/A.mp4"), 1.0, 30, speed=2.0, video=Path("/m/phone.mp4"), video_start=5.0,
                          fit="blur", broll=ov)]
    cmd = rc_render._batch_cmd(segs, 640, 360, Fraction(30), Path("o.mkv"), [])
    inputs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-i"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    ok = inputs == ["/m/A.mp4", "/m/A.mp4", "/m/phone.mp4", "/m/photo.jpg"] and "[3:v]" in fc \
        and "[2:v]setpts=(PTS-STARTPTS)/2," in fc and "[1:a]atempo=2," in fc and "[m1]" in fc
    check("組み合わせ: 2台目とBロールの入力の番号がずれない", ok, f"{inputs} / {fc[:240]}")
    i = cmd.index("/m/phone.mp4")
    check("組み合わせ: 早送りの2台目は倍率ぶん長く読む", cmd[i - 3:i] == ["-t", f"{2 + 0.1:.4f}", "-i"],
          str(cmd[i - 5:i]))

    # テロップの新しい種類（graphics）が、指示と選択肢のどこにも入っている
    enum = rc_plan.PLAN_SCHEMA["properties"]["telops"]["items"]["properties"]["style"]["enum"]
    sp_enum = rc_speedup.plan_schema()["properties"]["telops"]["items"]["properties"]["style"]["enum"]
    import importlib
    gui = importlib.import_module("gui")
    check("組み合わせ: カード型テロップがAIの選択肢・早送り用の指示・GUIに入る",
          all(c in enum and c in sp_enum and c in gui.STYLE_JA for c in CARD_STYLES), str(enum))

    # 演出プラン: 各機能の項目を引き継ぐ（Bロールは中身も確かめる）
    plan = {"telops": [], "zooms": [], "bgm": [], "angles": [{"start": 1, "end": 2}],
            "broll": [{"file": "/x.jpg", "start_id": 2}, {"file": "/y.jpg", "start_id": 999}], "_off": {}}
    out = rc_plan.sanitize(plan, {1, 2, 3}, [])
    check("組み合わせ: プランの angles / broll を引き継ぐ（_off は落とす）",
          out.get("angles") == plan["angles"] and [b["file"] for b in out.get("broll", [])] == ["/x.jpg"]
          and "_off" not in out, str(out))


def t_template(check):
    """全機能のテンプレートのセクションがそろい、テンプレート編集画面で保存し直しても消えない"""
    import tomllib

    import rc_design
    cfg = rc_design.load_cfg(ROOT / "templates" / "default")
    need = ["audio_clean", "color", "stabilize", "speedup", "shorts", "multicam", "broll", "translate", "speakers",
            "mv", "progress", "opening", "ending"]
    missing = [k for k in need if k not in cfg]
    check("組み合わせ: テンプレートに全機能の設定がある", not missing, f"足りない: {missing}")
    try:
        text = rc_design.dump_toml(cfg)
        ok = tomllib.loads(text) == cfg
    except Exception as e:  # noqa: BLE001
        ok, text = False, str(e)
    check("組み合わせ: テンプレート編集画面で保存しても全機能の設定が残る", ok, text[:200] if not ok else "")


# ---------------------------------------------------------------- フィルターの組み立て（FFmpeg で実際に書き出す）
def t_render_graph(check, fx: Path):
    """早送り＋2台目（縦長・ぼかし背景）＋色と手ブレの補正＋Bロールを1つの区切りに重ねても書き出せる"""
    import numpy as np
    from PIL import Image

    import rc_broll
    import rc_enhance
    import rc_render
    from rc_media import probe
    import test_multicam
    work = fx / "integration" / "graph"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    phone = test_multicam.make_cam2_tc(fx)
    main = probe(fx / "C0001.MP4")
    fps = Fraction(30)
    rc_enhance.write_cube(work / "warm.cube", rc_enhance.identity_grid() * np.array([1.0, 0.95, 0.85]), "warm")
    Image.new("RGB", (800, 450), (40, 90, 200)).save(work.parent / "写真 1.jpg")   # 作業フォルダの外
    vfx = rc_enhance.VideoFx(cube="warm.cube", stab="deshake", strength=0.5, fps=30.0)
    ov = rc_broll.Overlay(src=(work.parent / "写真 1.jpg").resolve(), kind="image", mode="wipe", item_frames=30,
                          tw=320, th=180, x=300, y=20, border=4)
    # 手ブレ補正は vidstab（解析ファイル .trf を作る）を、2台目＋早送りの区切りに使う
    vfx2 = rc_enhance.VideoFx(cube="warm.cube", stab="vidstab" if _has_filter("vidstabdetect") else "deshake",
                              strength=0.5, fps=30.0)
    segs = [rc_render.Seg(main.path, 1.0, 30, fx=vfx),
            rc_render.Seg(main.path, 20.0, 30, speed=4.0, video=phone, video_start=2.0, fit="blur", fx=vfx2,
                          broll=ov),
            rc_render.Seg(main.path, 5.0, 20, zoom=1.2)]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    try:
        body = rc_render.render_body(segs, 640, 360, fps, work, enc)
        ok, detail = body.exists(), ""
    except (Exception, SystemExit) as e:  # noqa: BLE001
        ok, detail, body = False, str(e)[-400:], None
    check("組み合わせ: 早送り＋2台目＋補正＋Bロールの区切りを書き出せる", ok, detail)
    if not ok:
        return
    d = _duration(body)
    check("組み合わせ: 書き出した長さがフレーム数どおり", abs(d - 80 / 30) < 0.08, f"{d:.3f}秒")
    # 2つ目の区切りの真ん中：右上にBロール（青）、中央は2台目、左右はぼかし背景
    t = 45 / 30
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(body), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    im = np.frombuffer(raw, np.uint8).reshape(360, 640, 3).astype(float)
    wipe = im[60:160, 360:580].mean(axis=(0, 1))
    check("組み合わせ: 2台目の区切りにもBロールが重なる", wipe[2] > wipe[0] + 60, str(wipe.round()))

    # 途中からの再開（rc_ops）: 同じ内容でもう一度書き出すと、できているパーツを使い回す（補正があっても）
    import contextlib
    import io
    import rc_ops
    rc_ops.clean_work(work)
    # finish.py と同じく、色の補正（.cube）は作業フォルダを片付けた後に作り直す（中身が同じなら使い回せる）
    rc_enhance.write_cube(work / "warm.cube", rc_enhance.identity_grid() * np.array([1.0, 0.95, 0.85]), "warm")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc_render.render_body(segs, 640, 360, fps, work, enc)
        ok = "前回書き出した分を使います" in buf.getvalue()
    except (Exception, SystemExit) as e:  # noqa: BLE001
        ok = False
        buf.write(str(e))
    check("組み合わせ: 補正つきでも、書き出し済みのパーツを使い回す", ok, buf.getvalue()[-300:])


# ---------------------------------------------------------------- 仕上げ（finish.py）の通し
def t_craft_all(check, fx: Path):
    """工作モード・2台目あり・Bロールあり：早送りの作業シーンが手元カメラに切り替わり、字幕などの時刻もそろう"""
    import numpy as np

    import rc_multicam
    import rc_speedup
    import test_broll
    import test_multicam
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    case = fx / "integration" / "工作 2台"
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    out = case / "C0001_roughcut"
    out.mkdir()
    shutil.copy(fx / "transcript.json", out / "transcript.json")
    assets = test_broll.make_assets(case / "broll")
    phone = test_multicam.make_cam2_tc(fx)
    p = _run(["roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--mode", "craft", "--llm", "none",
              "--decisions", str(fx / "claude_cuts.json"), "--cam2", str(phone), "--out", str(out)])
    check("組み合わせ: 荒編集（工作・2台）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    if p.returncode != 0:
        return
    plan = json.loads((fx / "plan.json").read_text(encoding="utf-8"))
    plan["broll"] = [{"file": str(assets / "マキタ_ドリル.jpg"), "start_id": 2, "end_id": 2, "mode": "full"}]
    (out / "plan_in.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    p = _run(["finish.py", str(out), "--template", str(fx / "tpl"), "--plan", str(out / "plan_in.json"),
              "--llm", "none", "--no-thumbs", "--keep-work", "--color", "off", "--stabilize", "off"])
    check("組み合わせ: 書き出し（早送り＋2台＋Bロール＋声の処理）", p.returncode == 0, "" if p.returncode == 0 else _tail(p))
    if p.returncode != 0:
        return
    used = json.loads((out / "plan_used.json").read_text(encoding="utf-8"))
    sp, ang = used.get("speedups", []), used.get("angles", [])
    check("組み合わせ: プランに早送り・カメラ・Bロールが全部入る",
          len(sp) == 1 and len(ang) == 1 and len(used.get("broll", [])) == 1
          and ang[0]["start"] < sp[0]["end"] and sp[0]["start"] < ang[0]["end"], f"{sp} / {ang}")

    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    src = SourceSet([probe(Path(x)) for x in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    stl = rc_speedup.SpeedTimeline(tl, sp)
    body = out / "work" / "body.mkv"
    d = _duration(body)
    check("組み合わせ: 本編の長さは早送りの分だけ短い", abs(d - stl.duration) < 0.15 and d < tl.duration - 3,
          f"{d:.2f}秒 / 計算 {stl.duration:.2f}秒 / 早送りなし {tl.duration:.2f}秒")

    # 早送りの真ん中の絵 = 2台目の、計算した時刻の絵（縦長なので中央を比べる）
    fast = [pt for pt in stl.parts if pt.sid is not None]
    cam2 = rc_multicam.load_cam2(out, sess)
    if fast and cam2:
        g = fast[-1]
        t_out = (g.o0 + g.of * 0.5) / float(stl.fps)
        t_src = g.g0 + (t_out - g.o0 / float(stl.fps)) * g.speed
        loc = cam2.locate(t_src)
        crop = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"
        a = _gray(body, t_out, 54, 96, crop)
        b = _gray(loc[0], loc[1], 54, 96)
        wrong = _gray(loc[0], max(0.0, loc[1] - 2.0), 54, 96)
        diff, diff_wrong = float(np.abs(a - b).mean()), float(np.abs(a - wrong).mean())
        check("組み合わせ: 早送りの区間は2台目の映像で、時刻も合う", a.size and diff < diff_wrong / 2,
              f"差 {diff:.1f}（2秒ずれた絵との差 {diff_wrong:.1f}）")
    else:
        check("組み合わせ: 早送りの区間は2台目の映像で、時刻も合う", False, f"早送り {len(fast)} / 2台目 {cam2}")

    # Bロール（発言2の間）が、早送りより前の時刻にそのまま出る
    chunks = {c["id"]: c for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))}
    t2 = stl.map_range(chunks[2]["start"], chunks[2]["end"])[0]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{(t2[0] + t2[1]) / 2:.3f}", "-i", str(body),
                          "-frames:v", "1", "-vf", "scale=192:108", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    im = np.frombuffer(raw, np.uint8).reshape(108, 192, 3).astype(float).mean(axis=(0, 1))
    check("組み合わせ: Bロールも正しい時刻に出る", im[2] > im[0] + 40, str(im.round()))

    # 字幕（完成の srt）も早送りの後の時刻
    srt = (out / "C0001_完成.srt").read_text(encoding="utf-8-sig")
    import re
    m = re.search(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> .*\nはい、きれいに開きました", srt)
    got = int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000 if m else -1
    want = stl.map_range(chunks[9]["start"], chunks[9]["end"])[0][0]
    check("組み合わせ: 早送りのあとの字幕の時刻", abs(got - want) < 0.02, f"{got} / 期待 {want:.3f}")


def run_all(check, out: Path | None, fx: Path):
    t_units(check)
    t_template(check)
    t_render_graph(check, fx)
    t_craft_all(check, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, None, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
