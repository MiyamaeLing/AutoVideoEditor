"""声と映像の補正（rc_enhance / enhance.py / gui_ext_enhance.py）のテスト。GPU・ネット・APIキーなしで動く。

    python tests/test_enhance.py          # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - テンプレート・撮影ジャンル・切り替え（GUI/コマンド）から使う補正が決まる（屋外は風切り音対策と手ブレ補正）
  - .cube の並び（R が一番速く変わる）が FFmpeg の lut3d と一致する・自動補正で色かぶりが減る
  - 声の処理で音が遅れない（ノイズ除去の遅れを取り除く）・オープニング/エンディングの音はそのまま
  - 色・手ブレ補正・声の処理をすべて使っても、完成動画で映像と音がずれない
  - vidstab が無い FFmpeg では deshake を使う
  - finish.py で屋外モード・LUT・自動補正を使って書き出せる。--audio-clean off などで切れる
  - 2台目カメラの色合わせ LUT（enhance.py cam2）
  - 長い音声（10分）でもメモリが増えない
  - 画面: 詳細設定の「声と映像の補正」・「2台目の色合わせ」ボタン
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import rc_enhance  # noqa: E402

PY = sys.executable
SR = 48000


def _run(args, cwd=ROOT, env=None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.pop(rc_enhance.ENV_NAME, None)
    e.update(env or {})
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=1200)


def _tail(p, n=12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _ff(*args, cwd=None):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, cwd=cwd)


class FakeTpl:
    def __init__(self, cfg: dict, d: Path = ROOT):
        self.cfg, self.dir = cfg, d

    def get(self, k, default=None):
        return self.cfg.get(k, default)


# ---------------------------------------------------------------- 設定の決まり方
def t_resolve(check):
    os.environ.pop(rc_enhance.ENV_NAME, None)
    tpl = FakeTpl({})
    talk = rc_enhance.resolve(tpl, "talk")
    out = rc_enhance.resolve(tpl, "outdoor")
    check("補正: 解説モードの初期値（声あり・色なし・手ブレ補正なし）",
          bool(talk.audio) and not talk.audio["wind"] and talk.color is None and talk.stab is None, talk.describe())
    check("補正: 屋外モードは風切り音対策を強め・手ブレ補正あり",
          out.audio["wind"] and out.audio["lowcut"] > talk.audio["lowcut"] and out.audio["nr"] > talk.audio["nr"]
          and out.stab and out.stab["method"] in ("vidstab", "deshake"), out.describe())
    chain = rc_enhance.voice_chain(out.audio)
    check("補正: 声のフィルター（低音カット・ノイズ除去・歯擦音・コンプレッサー）",
          all(k in chain for k in ("highpass", "afftdn", "deesser", "acompressor")) and chain.count("highpass") == 2,
          chain)
    off = rc_enhance.resolve(FakeTpl({"audio_clean": {"enabled": False}, "stabilize": {"enabled": False}}), "outdoor")
    check("補正: テンプレートで無効にできる", off.audio is None and off.stab is None)
    weak = rc_enhance.resolve(FakeTpl({"audio_clean": {"strength": 0.5, "deesser": 0}}), "talk")
    check("補正: 強さの調整", abs(weak.audio["nr"] - talk.audio["nr"] / 2) < 1e-6
          and "deesser" not in rc_enhance.voice_chain(weak.audio))
    ov = rc_enhance.resolve(tpl, "outdoor", {"audio_clean": False, "color": True, "stabilize": False})
    check("補正: 切り替えがテンプレートより優先", ov.audio is None and ov.color and ov.color["auto"] and ov.stab is None,
          ov.describe())
    os.environ[rc_enhance.ENV_NAME] = json.dumps({"stabilize": True, "color": "bad"})
    try:
        envd = rc_enhance.resolve(tpl, "talk")
    finally:
        os.environ.pop(rc_enhance.ENV_NAME, None)
    check("補正: 画面からの切り替え（環境変数）を読む", envd.stab is not None and envd.color is None, envd.describe())
    lut = rc_enhance.resolve(FakeTpl({"color": {"lut": "no_such.cube"}}), "talk")
    check("補正: LUT が無いときは注意を出して続ける", lut.color is None and any("LUT" in n for n in lut.notes))
    # vidstab が入っていない FFmpeg のふり
    real = rc_enhance._FILTERS
    rc_enhance._FILTERS = {"deshake", "afftdn", "lut3d"}
    try:
        fb = rc_enhance.resolve(FakeTpl({"stabilize": {"method": "vidstab"}}), "outdoor")
    finally:
        rc_enhance._FILTERS = real
    check("補正: vidstab が無ければ deshake を使う", fb.stab and fb.stab["method"] == "deshake"
          and any("deshake" in n for n in fb.notes), str(fb.stab))


# ---------------------------------------------------------------- LUT
def _frame_rgb(path: Path, cwd: Path, vf: str, size="64x64") -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", f"{vf},format=rgb24",
                        "-f", "rawvideo", "-"], capture_output=True, cwd=cwd, check=True)
    return np.frombuffer(p.stdout, np.uint8).reshape(-1, 3).astype(float) / 255


def t_cube(check, d: Path):
    g = rc_enhance.identity_grid(17)
    rc_enhance.write_cube(d / "swap.cube", g[:, [2, 1, 0]], "swap", 17)
    rc_enhance.write_cube(d / "same.cube", g, "same", 17)
    kind, data, lo, hi = rc_enhance.read_cube(d / "swap.cube")
    px = np.array([[0.9, 0.2, 0.1], [0.3, 0.6, 0.8]])
    check("LUT: 読み書きと補間", kind == "3D" and np.allclose(rc_enhance.apply_cube(px, (kind, data, lo, hi)),
                                                         px[:, [2, 1, 0]], atol=1e-6))
    _ff("-f", "lavfi", "-i", "color=c=0xE03010:s=64x64:d=0.1", "-frames:v", "1", str(d / "red.png"))
    a = _frame_rgb(d / "red.png", d, "null").mean(axis=0)
    b = _frame_rgb(d / "red.png", d, "lut3d=file=swap.cube").mean(axis=0)
    c = _frame_rgb(d / "red.png", d, "lut3d=file=same.cube").mean(axis=0)
    check("LUT: 並びが FFmpeg の lut3d と一致", np.allclose(b, a[[2, 1, 0]], atol=0.03) and np.allclose(c, a, atol=0.03),
          f"{a.round(2)} → {b.round(2)}")
    # 自動補正: 暗く青っぽい画面 → 少し明るく、色かぶりが減る
    rng = np.random.default_rng(3)
    gray = rng.uniform(0.15, 0.7, (4000, 1)) * np.ones((1, 3)) + rng.normal(0, 0.01, (4000, 3))
    cast = np.clip(gray * np.array([0.85, 1.0, 1.15]) * 0.75, 0, 1)
    p = rc_enhance.auto_params(cast, 0.5)
    fixed = rc_enhance.color_transform(cast, {"manual": None}, p)
    spread = lambda x: float(np.abs(x.mean(axis=0) - x.mean()).max())   # noqa: E731
    check("色: 自動補正で色かぶりが減り、少し明るくなる（控えめ）",
          spread(fixed) < spread(cast) * 0.9 and fixed.mean() > cast.mean() + 0.01
          and all(0.88 <= x <= 1.12 for x in p["gains"]), f"{spread(cast):.3f}→{spread(fixed):.3f} {p}")
    p0 = rc_enhance.auto_params(cast, 0.0)
    check("色: 強さ0なら何もしない", np.allclose(rc_enhance.color_transform(cast, {"manual": None}, p0), cast, atol=1e-6))
    # 2台目の色合わせ
    ref = rng.uniform(0, 1, (5000, 3))
    src = np.clip(ref * np.array([1.1, 0.95, 0.8]) + 0.03, 0, 1)
    cube = rc_enhance.match_cube(src, ref, 1.0)
    rc_enhance.write_cube(d / "m.cube", cube, "m")
    moved = rc_enhance.apply_cube(src, rc_enhance.read_cube(d / "m.cube"))
    check("色: 2台目の色を本編に寄せる", np.abs(moved.mean(0) - ref.mean(0)).max()
          < np.abs(src.mean(0) - ref.mean(0)).max() * 0.3)


# ---------------------------------------------------------------- 声の処理で音が遅れない
def _wav_f32(path: Path, x: np.ndarray):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-ac", "2", "-c:a", "pcm_f32le", str(path)], input=x.astype(np.float32).tobytes(), check=True)


def _read_f32(path: Path, cwd=None) -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True, check=True, cwd=cwd)
    return np.frombuffer(p.stdout, np.float32)


def _onsets(y: np.ndarray, times, win=0.3) -> list[float]:
    """各予定時刻の近くで、音が立ち上がった時刻（秒）"""
    out = []
    for t in times:
        a = max(0, int((t - win) * SR))
        seg = np.abs(y[a:int((t + win) * SR)])
        out.append((a + int(np.argmax(seg > 0.3 * seg.max()))) / SR)
    return out


def t_voice_sync(check, d: Path):
    rng = np.random.default_rng(5)
    n = SR * 12
    x = rng.normal(0, 0.01, n)
    beeps = [1.0, 4.0, 7.5, 10.5]
    for t in beeps:
        s = int(t * SR)
        x[s:s + int(0.12 * SR)] += 0.5 * np.sin(2 * np.pi * 900 * np.arange(int(0.12 * SR)) / SR)
    _wav_f32(d / "v_in.wav", x)
    st = rc_enhance.resolve(FakeTpl({}), "outdoor")
    vf = rc_enhance.voice_fx(st, 2.0, 9.0, 12.0, [(d / "v_in.wav", 0.0, 12.0)])
    nf = re.search(r"nf=(-?\d+)", vf.chain)
    check("声: 素材の雑音の大きさに合わせてノイズ除去する", nf is not None and -42 <= int(nf.group(1)) <= -32,
          vf.chain)
    g = ";".join(vf.graph("[0:a]anull", "[o]"))
    _ff("-i", "v_in.wav", "-filter_complex", g, "-map", "[o]", "-c:a", "pcm_f32le", "v_out.wav", cwd=d)
    y = _read_f32(d / "v_out.wav")
    x2 = _read_f32(d / "v_in.wav")
    on_in, on_out = _onsets(x2, beeps), _onsets(y, beeps)
    err = max(abs(a - b) for a, b in zip(on_in, on_out)) * 1000
    check("声: ノイズ除去の遅れを取り除く（ずれ 2ms 以内・長さ同じ）",
          vf.delay > 0 and err <= 2.0 and len(y) == len(x2), f"遅れ{vf.delay}サンプル→ずれ{err:.2f}ms 長さ{len(y)}/{len(x2)}")
    same = np.allclose(y[:2 * SR - 10], x2[:2 * SR - 10], atol=1e-6) and np.allclose(y[9 * SR + 10:], x2[9 * SR + 10:],
                                                                                    atol=1e-6)
    check("声: オープニング・エンディングの音はそのまま", same)

    # ノイズ除去の効き目（コンプレッサーは声とノイズの差を縮める処理なので、ここでは外して測る）
    st2 = rc_enhance.resolve(FakeTpl({"audio_clean": {"compressor": 0}}), "outdoor")
    vf2 = rc_enhance.voice_fx(st2, 0.0, None, 12.0, [(d / "v_in.wav", 0.0, 12.0)])
    _ff("-i", "v_in.wav", "-filter_complex", ";".join(vf2.graph("[0:a]anull", "[o]")), "-map", "[o]",
        "-c:a", "pcm_f32le", "v_out2.wav", cwd=d)
    y2 = _read_f32(d / "v_out2.wav")

    def snr(z):   # 声（ピッ）とノイズの大きさの差（dB）
        rms = lambda a, b: float(np.sqrt(np.mean(z[int(a * SR):int(b * SR)] ** 2)))   # noqa: E731
        return 20 * np.log10(rms(4.02, 4.1) / rms(5.0, 7.0))
    check("声: ノイズが減る（声とノイズの差が 6dB 以上広がる）", snr(y2) - snr(x2) >= 6,
          f"{snr(x2):.1f}dB → {snr(y2):.1f}dB")


# ---------------------------------------------------------------- 完成動画で映像と音がずれない
def _make_flash_clip(path: Path, dur: float, flashes, seed: int, fps=30):
    """フラッシュ（真っ白の1フレーム）と同時に「ピッ」と鳴る動画。手ブレ補正が効くように少し揺らす"""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 0.01, int(dur * SR))
    for t in flashes:
        s = int(t * SR)
        x[s:s + int(0.1 * SR)] += 0.5 * np.sin(2 * np.pi * 1000 * np.arange(int(0.1 * SR)) / SR)
    wav = path.with_suffix(".wav")
    _wav_f32(wav, x)
    en = "+".join(f"eq(n,{round(t * fps)})" for t in flashes)
    _ff("-f", "lavfi", "-i", f"testsrc2=s=1440x810:r={fps}", "-i", str(wav), "-t", str(dur),
        "-vf", f"crop=1280:720:80+40*sin(n*1.3):45+30*cos(n*1.7),drawbox=c=white:t=fill:enable='{en}'",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-c:a", "aac", "-shortest", str(path))


def _flash_times(path: Path, fps: float) -> list[float]:
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=32:18,format=gray", "-f", "rawvideo",
                        "-"], capture_output=True, check=True)
    y = np.frombuffer(p.stdout, np.uint8).reshape(-1, 32 * 18).mean(axis=1)
    idx = [i for i in range(len(y)) if y[i] > 200 and (i == 0 or y[i - 1] <= 200)]
    return [i / fps for i in idx]


def t_av_sync(check, d: Path):
    import rc_render
    from rc_ass import build_ass
    from rc_template import Template
    tpl = Template(ROOT / "templates" / "default")
    fps = Fraction(30)
    main, intro = d / "揺れ 素材.mp4", d / "intro.mp4"   # 日本語・スペースを含む名前
    _make_flash_clip(main, 12.0, [2.0, 5.0, 9.0], 1)
    _make_flash_clip(intro, 2.0, [1.0], 2)
    work = d / "作業 フォルダ"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    rc_enhance.write_cube(work / "warm.cube", np.clip(rc_enhance.identity_grid() * [1.05, 1.0, 0.92], 0, 1), "warm")
    st = rc_enhance.resolve(FakeTpl({"color": {"auto": True, "lut": "warm.cube"}}, work), "outdoor")
    segs = [rc_render.Seg(main.resolve(), 1.0, 150), rc_render.Seg(main.resolve(), 6.5, 150, 1.12)]
    rc_enhance.apply_video(st, segs, work, fps)
    all_segs = [rc_render.Seg(intro.resolve(), 0.0, 60)] + segs
    final, inter, _ = rc_render.pick_encoder("libx264", 20)
    t0 = time.time()
    body = rc_render.render_body(all_segs, 1280, 720, fps, work, inter)
    total = 12.0
    vf = rc_enhance.voice_fx(st, 2.0, None, total)
    loud = rc_render.mix_audio(body, total, [], [], tpl, work, voice_fx=vf)
    (work / "subs.ass").write_text(build_ass(tpl, 1280, 720, [], [], []), encoding="utf-8")
    out = d / "同期 テスト.mp4"
    rc_render.render_final(body, work / "mix.wav", loud, "subs.ass", None, None, tpl, 1280, 720, final, out, work)
    took = time.time() - t0
    expect = [1.0, 3.0, 6.0, 9.5]       # オープニング 2秒 + 本編（1.0〜6.0秒、6.5〜11.5秒）
    vt = _flash_times(out, 30.0)
    at = _onsets(_read_f32(out), expect, 0.25)
    ok_v = len(vt) == 4 and all(abs(a - b) < 0.02 for a, b in zip(vt, expect))
    diff = [abs(a - v) * 1000 for a, v in zip(at, vt)] if ok_v else [999]
    check("同期: 補正を全部使っても、映像のフラッシュが予定の位置", ok_v, f"{[round(t, 3) for t in vt]}")
    check("同期: 補正を全部使っても、映像と音がずれない（20ms 以内）", ok_v and max(diff) < 20,
          f"ずれ {[round(x, 1) for x in diff]} ms / {took:.0f}秒")
    trf = sorted(work.glob("*.trf"))
    check("同期: 手ブレ補正（vidstab）と色の LUT を使った", st.stab["method"] != "vidstab" or len(trf) == 2,
          f"{[t.name for t in trf]} {sorted(p.name for p in work.glob('color*.cube'))}")
    # 見た目の確認用に、補正の前と後の静止画を残す
    _ff("-ss", "4.0", "-i", str(main), "-frames:v", "1", "-vf", "scale=640:-2", str(d / "enhance_before.png"))
    _ff("-ss", "5.0", "-i", str(out), "-frames:v", "1", "-vf", "scale=640:-2", str(d / "enhance_after.png"))


def t_deshake(check, d: Path):
    import rc_render
    fps = Fraction(30)
    src = d / "揺れ 素材.mp4"
    work = d / "deshake"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    st = rc_enhance.resolve(FakeTpl({"stabilize": {"enabled": True, "method": "deshake"}}), "talk")
    segs = [rc_render.Seg(src.resolve(), 1.0, 90)]
    rc_enhance.apply_video(st, segs, work, fps)
    _, inter, _ = rc_render.pick_encoder("libx264", 23)
    body = rc_render.render_body(segs, 640, 360, fps, work, inter)
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(body)], capture_output=True, text=True)
    check("手ブレ補正: deshake でも書き出せる（フレーム数が同じ）", st.stab["method"] == "deshake"
          and n.stdout.strip() == "90", n.stdout.strip())


# ---------------------------------------------------------------- finish.py で使う
def t_finish(check, out: Path, fx: Path, d: Path):
    od = d / "outdoor"
    if od.exists():
        shutil.rmtree(od)
    shutil.copytree(out, od, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4"))
    sp = od / "session.json"
    sess = json.loads(sp.read_text(encoding="utf-8"))
    sess["mode"] = "outdoor"
    sp.write_text(json.dumps(sess, ensure_ascii=False), encoding="utf-8")
    tpl = d / "tpl_enh"
    if tpl.exists():
        shutil.rmtree(tpl)
    shutil.copytree(fx / "tpl", tpl)
    (tpl / "luts").mkdir()
    rc_enhance.write_cube(tpl / "luts" / "look.cube",
                          np.clip((rc_enhance.identity_grid() - 0.5) * 1.15 + 0.5, 0, 1) * [1.04, 1.0, 0.94], "look")
    s = (tpl / "template.toml").read_text(encoding="utf-8")
    if "[color]" not in s:   # 古い素材（このテストより前に作ったテンプレート）にも足す
        s += (ROOT / "templates" / "default" / "template.toml").read_text(encoding="utf-8").split(
            "# ---- 声を聞き取りやすくする")[1].join(["\n# ---- 声を聞き取りやすくする", ""])
    s = re.sub(r'(?m)^auto = false', "auto = true", s)
    s = re.sub(r'(?m)^lut = ""', 'lut = "luts/look.cube"', s)
    (tpl / "template.toml").write_text(s, encoding="utf-8")
    plan = fx / "plan.json"
    p = _run(["finish.py", str(od), "--template", str(tpl), "--plan", str(plan), "--llm", "none", "--preview", "8",
              "--no-thumbs"])
    log = p.stdout
    line = next((ln for ln in log.splitlines() if "補正:" in ln), "")
    ok = p.returncode == 0 and "風切り音" in line and "LUT look.cube" in line and "自動補正" in line and "手ブレ補正" in line
    check("仕上げ: 屋外モードで声・色（自動+LUT）・手ブレ補正を使って書き出せる", ok, line if ok else _tail(p))
    mp4 = od / "C0001_プレビュー.mp4"
    if mp4.exists():
        q = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
                            str(mp4)], capture_output=True, text=True)
        ds = {s["codec_type"]: float(s["duration"]) for s in json.loads(q.stdout)["streams"]}
        check("仕上げ: 映像と音声の長さがそろう", abs(ds.get("video", 0) - ds.get("audio", -1)) < 0.06
              and abs(ds["video"] - 8) < 0.1, str(ds))
        _ff("-ss", "3", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=640:-2", str(d / "finish_outdoor.png"))
    p2 = _run(["finish.py", str(od), "--template", str(tpl), "--plan", str(plan), "--llm", "none", "--preview", "4",
               "--no-thumbs", "--audio-clean", "off", "--color", "off", "--stabilize", "off"])
    check("仕上げ: --audio-clean off などで補正を切れる", p2.returncode == 0 and "補正: なし" in p2.stdout,
          "" if p2.returncode == 0 else _tail(p2))
    p3 = _run(["finish.py", str(od), "--template", str(tpl), "--plan", str(plan), "--llm", "none", "--preview", "4",
               "--no-thumbs", "--color", "off"], env={rc_enhance.ENV_NAME: json.dumps({"stabilize": False})})
    l3 = next((ln for ln in p3.stdout.splitlines() if "補正:" in ln), "")
    check("仕上げ: 画面の切り替え（環境変数）が効く", p3.returncode == 0 and "手ブレ" not in l3 and "声" in l3, l3)
    p4 = _run(["finish.py", "--template-preview", "--template", str(tpl)])
    check("仕上げ: テンプレートの見本にも色の設定を使う", p4.returncode == 0 and (tpl / "preview_1.png").exists(),
          "" if p4.returncode == 0 else _tail(p4))


def t_cam2_cli(check, out: Path, d: Path):
    od = d / "cam2"
    if od.exists():
        shutil.rmtree(od)
    shutil.copytree(out, od, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4"))
    p = _run(["enhance.py", "cam2", str(od)])
    cube = od / "cam2_色合わせ.cube"
    ok = p.returncode == 0 and cube.exists()
    n = 0
    if ok:
        kind, data, _, _ = rc_enhance.read_cube(cube)
        n = data.shape[0]
    check("2台目: 色合わせ LUT を作る（enhance.py cam2）", ok and n == rc_enhance.CUBE_SIZE, _tail(p, 4))
    sp = od / "session.json"
    sess = json.loads(sp.read_text(encoding="utf-8"))
    sess["args"]["cam2"] = []
    sp.write_text(json.dumps(sess, ensure_ascii=False), encoding="utf-8")
    p2 = _run(["enhance.py", "cam2", str(od)])
    check("2台目: 2台目が無いときは分かりやすく伝える", p2.returncode != 0 and "2台目" in p2.stderr, p2.stderr[-200:])
    p3 = _run(["enhance.py", "check", "--mode", "outdoor"])
    check("補正: enhance.py check", p3.returncode == 0 and "手ブレ補正" in p3.stdout, _tail(p3, 6))


def t_long_audio(check):
    """10分の音声を、オープニング・エンディング付きで声の処理に通す（メモリが増えないこと）"""
    st = rc_enhance.resolve(FakeTpl({}), "outdoor")
    vf = rc_enhance.voice_fx(st, 5.0, 595.0, 600.0)
    g = ";".join(vf.graph("[0:a]anull", "[o]"))
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"anoisesrc=d=600:r={SR}:a=0.05,aformat=channel_layouts=stereo",
           "-filter_complex", g, "-map", "[o]", "-f", "null", "-"]
    # 別のプロセスの中で FFmpeg を動かして、その FFmpeg だけの最大メモリを測る（Windows では測れないので 0）
    code = ("import subprocess,sys,json\n"
            "p=subprocess.run(json.loads(sys.argv[1]),capture_output=True)\n"
            "try:\n import resource; m=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024\n"
            "except ImportError:\n m=0\n"
            "print(p.returncode, m)\n")
    t0 = time.time()
    p = subprocess.run([PY, "-c", code, json.dumps(cmd)], capture_output=True, text=True)
    rc, mb = (p.stdout.split() + ["1", "0"])[:2]
    mb = float(mb)
    took = time.time() - t0
    check("長尺: 10分の声の処理でメモリが増えない（300MB 未満）", rc == "0" and mb < 300,
          f"{mb:.0f}MB / {took:.1f}秒（3時間なら約{took * 18 / 60:.0f}分）")


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import os, sys, json, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
QMessageBox.information = staticmethod(lambda *a, **k: None)
w = gui.MainWindow(); w.resize(1200, 820); w.show()
res = {"ext": "gui_ext_enhance" in w.extensions}
form = w.create.adv.parentWidget().layout()
row = w.enhance_row
res["hidden_first"] = not row.isVisible()
w.create.adv.setChecked(True)
for _ in range(5):
    app.processEvents()
res["shown"] = row.isVisible()
row.combos["stabilize"].setCurrentIndex(row.combos["stabilize"].findData("on"))
row.combos["audio_clean"].setCurrentIndex(row.combos["audio_clean"].findData("off"))
res["env"] = json.loads(w.runner.env().value("AVE_ENHANCE"))
res["saved"] = w.settings.value("enhance/stabilize")
w.grab().save(str(Path(OUT) / "gui_enhance_create.png"))
calls = []
w.runner.start = lambda script, args, title, on_done=None, handle_error=None: calls.append((script, list(args))) or True
w.review.load(Path(OUT))
btn = [w.review.ext_bar.itemAt(i).widget() for i in range(w.review.ext_bar.count())]
btn = [b for b in btn if b is not None and b.text() == "2台目の色合わせ"]
res["button"] = bool(btn)
if btn:
    btn[0].click()
res["calls"] = calls
w.tabs.setCurrentWidget(w.review)
for _ in range(5):
    app.processEvents()
w.grab().save(str(Path(OUT) / "gui_enhance_review.png"))
w.create.files.add([str(Path(OUT).parent / "揺れ 素材.mp4")])
row.show_sample()
res["sample_call"] = calls[-1] if calls else None
w2 = gui.MainWindow()
res["persist"] = w2.enhance_row.combos["stabilize"].currentData()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, d: Path):
    gd = d / "gui"
    if gd.exists():
        shutil.rmtree(gd)
    shutil.copytree(out, gd, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4"))
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\n" + GUI_CODE
    cfg = d / "appdata"
    env = dict(os.environ, APPDATA=str(cfg), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(cfg))
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("画面: 補正の切り替え", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("画面: 補正の切り替え", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("画面: 拡張（gui_ext_enhance）を読み込む", r["ext"])
    check("画面: 詳細設定を開くと「声と映像の補正」が出る", r["hidden_first"] and r["shown"])
    check("画面: 選んだ補正を書き出しに渡す", r["env"] == {"audio_clean": False, "stabilize": True}, str(r["env"]))
    check("画面: 選んだ補正を覚えておく", r["saved"] == "on" and r["persist"] == "on")
    check("画面: 「2台目の色合わせ」ボタン", r["button"] and r["calls"] and r["calls"][0][0] == "enhance.py"
          and r["calls"][0][1][0] == "cam2", str(r["calls"]))
    sc = r["sample_call"] or ["", []]
    check("画面: 「色の見本…」で比較画像を作る", sc[0] == "enhance.py" and sc[1][0] == "compare", str(sc))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    d = fx / "enhance"
    d.mkdir(exist_ok=True)
    t_resolve(check)
    t_cube(check, d)
    t_voice_sync(check, d)
    t_av_sync(check, d)
    t_deshake(check, d)
    t_long_audio(check)
    if not (out / "session.json").exists():
        check("補正のテスト（荒編集の結果が必要）", False, "先に荒編集が成功している必要があります")
        return
    t_finish(check, out, fx, d)
    t_cam2_cli(check, out, d)
    t_gui(check, out, d)


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
