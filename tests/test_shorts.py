"""縦型ショート（shorts.py / rc_shorts.py / gui_ext_shorts.py）のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_shorts.py            # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py             # 通しテストの最後からも呼ばれます

確認すること:
  - 区間の選び方（AIなし：見どころの周り・30〜60秒・重ならない／AIの返答の確認と直し）
  - Claude API の呼び出し（偽の通信。モデル・構造化出力の指定）と、その結果での書き出し
  - 顔を追いかける切り抜き（顔が動いても画面の中央付近に入る・急に動かない）
  - 顔がないときは動きの多い所、それもなければ中央
  - 1080x1920 で書き出し、shorts.json に #shorts 付きの説明文
  - claude.ai に貼るプロンプトの作成
  - 画面（GUI）に「ショートを作る」ボタンと一覧
  - 素材全体を1本のショートに（shorts.py --whole）: 1080x1920・全体の長さ・BGM・タイトル（AI／タイトル案）・
    本編から切り出したショートは消さない。画面の「作るもの」と「全体を1本のショートに」
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable
FACE = ROOT / "tests" / "data" / "face.jpg"
SR = 48000
PROJ_LEN = 150.0

TEXTS = ["今日はこの道具を使ってみます", "まずは見た目をチェックしていきましょう", "持ってみると意外と軽いんですよ",
         "ここが一番のポイントです", "実際に動かしてみますね", "おお、これはすごい", "思ったよりパワーがあります",
         "ただ、ちょっと音は大きめかな", "値段を考えるとかなりお得です", "というわけで、おすすめです"]


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=900)


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _probe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(p)],
                         capture_output=True, text=True).stdout
    d = json.loads(out or "{}")
    s = (d.get("streams") or [{}])[0]
    return {"w": s.get("width", 0), "h": s.get("height", 0), "dur": float(d.get("format", {}).get("duration", 0))}


def _frame(video: Path, t: float, out: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(out)],
                   check=True)
    return out


# ---------------------------------------------------------------- 素材
def make_project(fx: Path) -> Path:
    """顔が左右に動く場面（0〜100秒）と、白い箱だけが動く場面（100〜150秒）がある、荒編集済みの想定のフォルダ。"""
    d = fx / "shorts_proj"
    stamp = f"ok3 {d.resolve()}"
    if (d / "done").exists() and (d / "done").read_text(encoding="utf-8") == stamp:
        return d
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    rng = np.random.default_rng(5)
    x = np.zeros(int(SR * PROJ_LEN), dtype=np.float32)
    starts = np.arange(0.5, PROJ_LEN - 2, 3.0)
    for s in starts:
        n0, n = int(s * SR), int(2.4 * SR)
        x[n0:n0 + n] += 0.3 * np.sin(2 * np.pi * rng.uniform(200, 600) * np.arange(n) / SR)
    with wave.open(str(d / "voice.wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    face_x = "if(lt(t,100),80+330*(1+sin(t*2*PI/36)),2000)"
    box_x = "if(gte(t,100),60+(t-100)*20,-400)"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x4A5568:s=1280x720:r=30:d={PROJ_LEN}",
        "-loop", "1", "-i", str(FACE),
        "-f", "lavfi", "-i", "color=c=white:s=140x140:r=30",
        "-i", str(d / "voice.wav"),
        "-filter_complex",
        f"[1:v]scale=380:380[f];[0:v][f]overlay=x='{face_x}':y=170:shortest=1[a];"
        f"[a][2:v]overlay=x='{box_x}':y=290:shortest=1,format=yuv420p[v]",
        "-map", "[v]", "-map", "3:a", "-t", str(PROJ_LEN), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", str(d / "顔テスト 素材.mp4")], check=True)

    chunks = [{"id": i, "start": round(float(s), 2), "end": round(float(s) + 2.4, 2), "text": TEXTS[(i - 1) % len(TEXTS)]}
              for i, s in enumerate(starts, 1)]
    (d / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    # 見どころ: 21.5秒(#8) / 75.5秒(#26) / 117.5秒(#40)
    (d / "decisions_used.json").write_text(json.dumps({
        "cuts": [], "review": [], "corrections": [],
        "highlights": [{"id": 8, "label": "軽さにびっくり"}, {"id": 26, "label": "パワーがすごい"},
                       {"id": 40, "label": "白い箱が動く"}]}, ensure_ascii=False), encoding="utf-8")
    (d / "plan_used.json").write_text(json.dumps({
        "telops": [{"start_id": 27, "end_id": 27, "text": "ここがポイント！", "style": "emphasis", "se": "none"}],
        "zooms": [], "bgm": []}, ensure_ascii=False), encoding="utf-8")
    sess = {"version": 1, "mode": "talk", "args": {}, "stem": "顔テスト 素材",
            "inputs": [str((d / "顔テスト 素材.mp4").resolve())],
            "keep": [[0.0, 49.2], [51.0, PROJ_LEN]],   # 49.2〜51秒はカット済み（つなぎ目のテスト）
            "cut_ids": [], "corrections": {}, "markers": [], "chapters": [],
            "meta": {"titles": ["道具レビュー"], "tags": ["レビュー", "工作"]}}
    (d / "session.json").write_text(json.dumps(sess, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "done").write_text(stamp, encoding="utf-8")
    return d


# ---------------------------------------------------------------- 偽の Claude
def fake_claude(answer: dict, calls: list):
    import anthropic
    import httpx2

    text = json.dumps(answer, ensure_ascii=False)
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text[:40]}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text[40:]}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 300}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {k}\ndata: {json.dumps(v, ensure_ascii=False)}\n\n" for k, v in events).encode("utf-8")

    def handler(request):
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    client = anthropic.Anthropic(api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return lambda: (anthropic, client)


# ---------------------------------------------------------------- 単体
def t_units(check, proj: Path, tpl_dir: Path):
    import rc_shorts
    from rc_template import Template

    check("ショート: 横長素材の切り抜きの大きさ", rc_shorts.crop_box(1920, 1080) == (606, 1080, 0),
          str(rc_shorts.crop_box(1920, 1080)))
    check("ショート: 縦長素材は幅いっぱい", rc_shorts.crop_box(1080, 2400) == (1080, 1920, 240),
          str(rc_shorts.crop_box(1080, 2400)))
    lines = rc_shorts.wrap_title("穴あけが一瞬で終わる最強ドリル", 11)
    check("ショート: タイトルを自然な所で2行に", len(lines) == 2 and all(len(s) <= 11 for s in lines), str(lines))

    p = rc_shorts.Pick(1, 2, "t", "説明です", ["#工作", "DIY"])
    desc = rc_shorts.description_text(p, rc_shorts.DEFAULTS)
    check("ショート: 説明文の最後に #shorts", desc.endswith("#shorts #工作 #DIY") and desc.startswith("説明です"), desc)

    # なめらかさ：0.2 → 0.8 に急に移っても、1フレームで動く量は上限まで
    cfg = dict(rc_shorts.DEFAULTS)
    samples = [(k / 5, 0.2 if k < 25 else 0.8, "face") for k in range(50)]
    c = rc_shorts.smooth_path(samples, 30.0, 300, [], cfg)
    vmax = cfg["follow_max_speed"] / 30
    check("ショート: 急に動かない（動く速さに上限）", float(np.abs(np.diff(c)).max()) <= vmax + 1e-9
          and abs(c[-1] - 0.8) < 0.05 and abs(c[0] - 0.2) < 0.05, f"{np.abs(np.diff(c)).max():.4f}")
    jitter = [(k / 5, 0.5 + (0.015 if k % 2 else -0.015), "face") for k in range(50)]
    c2 = rc_shorts.smooth_path(jitter, 30.0, 300, [], cfg)
    check("ショート: 小さなゆれは追いかけない", float(c2.max() - c2.min()) < 0.03, f"{c2.max() - c2.min():.3f}")
    c3 = rc_shorts.smooth_path(samples, 30.0, 300, [150], cfg)
    check("ショート: つなぎ目では位置を切り替えてよい", abs(c3[150] - c3[149]) > 0.3 and abs(c3[151] - c3[150]) <= vmax + 1e-9,
          f"{c3[149]:.2f}→{c3[150]:.2f}")

    # 区間の選び方（AIなし）
    pj = rc_shorts.load_project(proj)
    picks = rc_shorts.pick_auto(pj, 3, 30, 60)
    seeds = {i for i, *_ in rc_shorts.seeds(pj)}
    ok = len(picks) == 3 and all(29.5 <= p.b - p.a <= 60.01 for p in picks)
    ok = ok and all(not rc_shorts._overlap(p, [q for q in picks if q is not p]) for p in picks)
    check("ショート: AIなしで3本・30〜60秒・重ならない", ok,
          str([(round(p.a), round(p.b), p.start_id) for p in picks]))
    check("ショート: AIなしは見どころから始まる", all(p.start_id in seeds for p in picks),
          str([p.start_id for p in picks]))

    # AIの返答の確認と直し
    res = {"shorts": [
        {"start_id": 999, "end_id": 1000, "title": "ない発言", "description": "", "hashtags": [], "hook": "", "reason": ""},
        {"start_id": 5, "end_id": 45, "title": "長すぎる", "description": "d", "hashtags": ["#a"], "hook": "", "reason": ""},
        {"start_id": 6, "end_id": 15, "title": "重なる", "description": "", "hashtags": [], "hook": "", "reason": ""},
        {"start_id": 30, "end_id": 31, "title": "短すぎる", "description": "", "hashtags": [], "hook": "", "reason": ""},
    ]}
    ps = rc_shorts.picks_from_ai(pj, res, 3, 30, 60)
    check("ショート: AIの返答を確かめて直す（ない発言・長さ・重なり）",
          [p.title for p in ps] == ["長すぎる", "短すぎる"] and all(29.5 <= p.b - p.a <= 60.01 for p in ps)
          and ps[0].start_id == 5 and ps[1].start_id == 30 and ps[0].hashtags == ["a"],
          str([(p.title, round(p.b - p.a, 1), p.start_id) for p in ps]))

    # claude.ai 用のプロンプト
    mp = proj / "prompt_shorts_test.md"
    rc_shorts.write_manual_prompt(pj, 3, 30, 60, mp)
    s = mp.read_text(encoding="utf-8")
    check("ショート: claude.ai 用のプロンプト", "★軽さにびっくり" in s and '"start_id"' in s and "#1 [" in s)

    # ASS：タイトル帯・字幕
    tpl = Template(tpl_dir)
    ass = rc_shorts.build_ass(tpl, rc_shorts.cfg_of(tpl), "とても長いタイトルがここに入ります",
                              [(0.0, 2.0, "今日はこのドリルをレビューしていきます")], [], 30.0)
    check("ショート: タイトル帯と2行の字幕", "PlayResY: 1920" in ass and "\\p1}m 0 0 l 1080 0" in ass
          and "今日はこのドリルを\\Nレビューしていきます" in ass, "")

    # 顔も動きもない映像は中央
    still = proj / "still.mp4"
    if not still.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x334455:s=1280x720:r=30:d=3",
                        "-c:v", "libx264", "-preset", "ultrafast", str(still)], check=True)
    smp = rc_shorts.analyze(still, 404 / 1280, [])
    check("ショート: 顔も動きもなければ中央", smp and all(w == "center" and x == 0.5 for _t, x, w in smp),
          str({w for *_r, w in smp}))


# ---------------------------------------------------------------- AI（偽の通信）→ 書き出し
def t_ai_render(check, proj: Path, tpl_dir: Path):
    import rc_llm
    import rc_shorts
    from rc_template import Template

    answer = {"shorts": [
        {"start_id": 20, "end_id": 30, "title": "この道具、想像以上でした", "description": "道具を実際に使ってみました。",
         "hashtags": ["道具", "レビュー"], "hook": "いきなり動かす", "reason": "顔が映っていて分かりやすい"},
        {"start_id": 37, "end_id": 47, "title": "白い箱が動く", "description": "動きのある場面です。",
         "hashtags": ["テスト"], "hook": "箱", "reason": "動きがある"},
    ]}
    calls: list = []
    orig, old_key = rc_llm._client, os.environ.get("ANTHROPIC_API_KEY")
    rc_llm._client = fake_claude(answer, calls)
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    sdir = proj / "shorts"
    try:
        outs = rc_shorts.generate(proj, Template(tpl_dir), llm="api", effort="low", count=2)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        check("ショート: AIで選んで書き出し", False, repr(e))
        return
    finally:
        rc_llm._client = orig
        if old_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = old_key
    req = calls[0] if calls else {}
    fmt = (req.get("output_config") or {}).get("format") or {}
    check("ショート: Claude API の呼び出し（モデル・構造化出力）",
          req.get("model") == "claude-opus-5-5" and fmt.get("type") == "json_schema"
          and "shorts" in json.dumps(fmt.get("schema", {})) and "#20 [" in json.dumps(req.get("messages"),
                                                                                       ensure_ascii=False),
          str(req.get("model")))
    info = json.loads((sdir / "shorts.json").read_text(encoding="utf-8"))["shorts"]
    check("ショート: AIの区間で2本", len(outs) == 2 and [s["by"] for s in info] == ["ai", "ai"]
          and info[0]["title"] == "この道具、想像以上でした", str([s["title"] for s in info]))
    check("ショート: 説明文に #shorts", all("#shorts" in s["description"] for s in info)
          and info[0]["description"].startswith("道具を実際に使ってみました。"), info[0]["description"])
    ok = True
    for p in outs:
        pr = _probe(p)
        ok = ok and pr["w"] == 1080 and pr["h"] == 1920 and 29 <= pr["dur"] <= 61
    check("ショート: 1080x1920・30〜60秒で書き出し", ok, str([_probe(p) for p in outs]))
    f1, f2 = info[0]["follow"], info[1]["follow"]
    check("ショート: 顔のある場面は顔を追いかける", f1["face"] > 0.8 * sum(f1.values()), str(f1))
    check("ショート: 顔がない場面は動きを追いかける", f2["motion"] > 0.6 * sum(f2.values()), str(f2))

    # できた動画の中で、顔が画面の真ん中あたりにあるか（顔検出で確かめる）
    det = rc_shorts.face_detector()
    import cv2
    hits, pos = 0, []
    for k, t in enumerate((3.0, 12.0, 21.0, 28.0)):
        img = cv2.imread(str(_frame(outs[0], t, sdir / f"check_face_{k}.png")), cv2.IMREAD_GRAYSCALE)
        small = cv2.resize(img, (360, 640))
        faces = det.detectMultiScale(small, 1.1, 5, minSize=(40, 40))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx = (x + w / 2) / 360
            pos.append(round(float(cx), 2))
            hits += abs(cx - 0.5) < 0.15
    check("ショート: 顔が画面の中央付近に入っている", hits >= 3, str(pos))
    # 動きの場面：白い箱が画面に入っている
    seen = 0
    for k, t in enumerate((5.0, 15.0, 25.0)):
        img = cv2.imread(str(_frame(outs[1], t, sdir / f"check_box_{k}.png")), cv2.IMREAD_GRAYSCALE)
        mid = img[400:1500]
        seen += int((mid > 235).sum() > 20000)
    check("ショート: 動いている物が画面に入っている", seen >= 2, f"{seen}/3")


# ---------------------------------------------------------------- コマンド（荒編集の結果から）
def t_cli(check, out: Path, fx: Path):
    if not (out / "session.json").exists():
        check("ショート: コマンド（荒編集の結果が必要）", False, "先に荒編集が成功している必要があります")
        return
    sd = fx / "shorts_cli"
    if sd.exists():
        shutil.rmtree(sd)
    shutil.copytree(out, sd, ignore=shutil.ignore_patterns("work", "shorts", "thumbnails", "*.mp4"))
    tpl = str(fx / "tpl")
    p = _run(["shorts.py", str(sd), "--template", tpl, "--llm", "manual"])
    check("ショート: claude.ai 用のプロンプトを作る（--llm manual）",
          p.returncode == 0 and (sd / "shorts" / "prompt_shorts.md").exists(), _tail(p) if p.returncode else "")
    p = _run(["shorts.py", str(sd), "--template", tpl, "--llm", "api", "--count", "1"],
             env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""})
    ok = p.returncode == 0 and (sd / "shorts" / "short_1.mp4").exists()
    check("ショート: APIキーがなければ見どころから作る（shorts.py）", ok and "APIキー" in p.stdout,
          "" if ok else _tail(p))
    if ok:
        pr = _probe(sd / "shorts" / "short_1.mp4")
        info = json.loads((sd / "shorts" / "shorts.json").read_text(encoding="utf-8"))["shorts"]
        check("ショート: 荒編集の結果から1080x1920", pr["w"] == 1080 and pr["h"] == 1920 and pr["dur"] > 10, str(pr))
        check("ショート: shorts.json（タイトル・#shorts・一覧用の画像）",
              len(info) == 1 and info[0]["title"] and "#shorts" in info[0]["description"]
              and (sd / "shorts" / info[0]["thumb"]).exists(), info[0]["title"] if info else "")
        _frame(sd / "shorts" / "short_1.mp4", 8.0, sd / "shorts" / "check_frame.png")
    # もう一度作ると、前のショートは置き換わる
    p = _run(["shorts.py", str(sd), "--template", tpl, "--llm", "none", "--count", "1"],
             env={"ANTHROPIC_API_KEY": ""})
    check("ショート: 作り直すと前のショートを置き換える", p.returncode == 0
          and len(list((sd / "shorts").glob("short_*.mp4"))) == 1, "" if p.returncode == 0 else _tail(p))


# ---------------------------------------------------------------- 素材全体を1本に
def t_whole(check, out: Path, fx: Path):
    """shorts.py --whole: カット後の素材全体を1本の縦型ショートに（ショートだけ作りたいとき）"""
    import rc_llm
    import rc_shorts
    from rc_template import Template
    sd = fx / "shorts_cli"
    if not (sd / "session.json").exists():
        check("ショート（全体を1本に）: 荒編集の結果が必要", False, "先に荒編集が成功している必要があります")
        return
    tpl_dir = fx / "tpl"
    pj = rc_shorts.load_project(sd)
    cut_before = sorted(f.name for f in (sd / "shorts").glob("short_*.mp4"))
    p = _run(["shorts.py", str(sd), "--template", str(tpl_dir), "--whole", "--llm", "none"])
    video, thumb, info_path = rc_shorts.whole_names(sd)
    ok = p.returncode == 0 and video.exists() and info_path.exists()
    check("ショート（全体を1本に）: shorts.py --whole で作れる", ok, "" if ok else _tail(p))
    if not ok:
        return
    pr = _probe(video)
    info = json.loads(info_path.read_text(encoding="utf-8"))
    check("ショート（全体を1本に）: 1080x1920 で、カット後の全体の長さ",
          pr["w"] == 1080 and pr["h"] == 1920 and abs(pr["dur"] - pj.tl.duration) < 0.5, f"{pr} / {pj.tl.duration:.1f}")
    check("ショート（全体を1本に）: 情報（タイトル・#shorts・一覧用の画像）を残し、切り出したショートは消さない",
          info["by"] == "whole" and info["title"] and "#shorts" in info["description"] and thumb.exists()
          and video.name.endswith("_ショート.mp4") and not info["over_limit"]
          and sorted(f.name for f in (sd / "shorts").glob("short_*.mp4")) == cut_before, str(info.get("title")))
    tpl = Template(tpl_dir)
    want_bgm = bool(rc_shorts.whole_bgm(pj, tpl, pj.tl.duration))
    has_plan_bgm = bool(pj.plan.get("bgm")) and any(tpl.bgm_files().values())
    check("ショート（全体を1本に）: 演出プランのBGMを重ねる（プランか曲が無ければ付けない）",
          info["bgm"] == want_bgm == has_plan_bgm and not rc_shorts.whole_bgm(
              rc_shorts.Project(pj.out, pj.sess, pj.chunks, pj.decisions, {}, pj.meta, pj.src, pj.tl, pj.items),
              tpl, pj.tl.duration), f"{info['bgm']} {want_bgm} {has_plan_bgm}")
    # タイトルなど: AI（偽の通信）／使えなければ荒編集のタイトル案から
    answer = {"title": "30秒で分かる\\nドリル", "description": "ドリルを試しました。", "hashtags": ["#ドリル", "工具"],
              "hook": "いきなり穴あけ"}
    calls: list = []
    orig = rc_llm._client
    rc_llm._client = fake_claude(answer, calls)
    try:
        pick, _u = rc_shorts.whole_pick(pj, "api", "low")
    finally:
        rc_llm._client = orig
    fmt = ((calls[0] if calls else {}).get("output_config") or {}).get("format") or {}
    check("ショート（全体を1本に）: タイトル・説明文・タグをAIが決める（区間は全体）",
          pick.title == "30秒で分かるドリル" and pick.hashtags == ["ドリル", "工具"] and pick.a == 0.0
          and abs(pick.b - pj.tl.duration) < 0.01 and fmt.get("schema", {}).get("required") == rc_shorts.WHOLE_SCHEMA["required"],
          f"{pick.title} {pick.hashtags}")

    def broken():
        raise SystemExit("APIキーがありません")
    rc_llm._client = broken
    try:
        pick2, _u = rc_shorts.whole_pick(pj, "api", "low")
    finally:
        rc_llm._client = orig
    pick3, _u = rc_shorts.whole_pick(pj, "none", "low")
    check("ショート（全体を1本に）: AIが使えないとき・使わないときは、荒編集のタイトル案から",
          pick2.title and pick2.title == pick3.title and len(pick3.title) <= 20 and pick3.by == "whole", pick3.title)
    check("ショート（全体を1本に）: 3分を超えたら知らせる（上限は180秒）", rc_shorts.WHOLE_LIMIT == 180.0)


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import json, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
res = {}
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
res["loaded"] = "gui_ext_shorts" in w.extensions
ext = w.shorts_ext
res["disabled_before"] = not ext.btn.isEnabled()
w.review.load(Path(OUT))
w.tabs.setCurrentWidget(w.review)
for _ in range(5):
    app.processEvents()
res["enabled_after"] = ext.btn.isEnabled() and ext.view.isEnabled()
res["view_text"] = ext.view.text()
w.grab().save(str(Path(SHOTS) / "gui_shorts_main.png"))
calls = []
real = w.runner.start
w.runner.start = lambda script, args, title, on_done=None, handle_error=None: calls.append((script, list(args))) or True
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
ext.count.setValue(2)
ext.make()
res["call"] = calls[-1] if calls else None
# 素材全体を1本のショートに
res["whole_btn"] = ext.whole.isEnabled()
ext.make_whole()
res["whole_call"] = calls[-1] if calls else None
plans = []
real_plan = w.review.make_plan
w.review.make_plan = lambda then=None: plans.append(then)
for n in ("plan_edited.json", "plan_used.json"):
    if (Path(OUT) / n).exists():
        (Path(OUT) / n).rename(Path(OUT) / (n + ".bak"))
n_calls = len(calls)
ext.make_whole(plan=True)
res["plan_first"] = len(plans) == 1 and len(calls) == n_calls
for n in ("plan_edited.json", "plan_used.json"):
    if (Path(OUT) / (n + ".bak")).exists():
        (Path(OUT) / (n + ".bak")).rename(Path(OUT) / n)
w.review.make_plan = real_plan
res["target_default"] = w.create.target.currentData()
before = w.create.btn_full.text()
w.create.target.setCurrentIndex(w.create.target.findData("short"))
res["target_text"] = [before, w.create.btn_full.text()]
w.create.target.setCurrentIndex(w.create.target.findData("video"))
w.runner.start = real
# 処理中はボタンを押せない
w.runner.proc = object()
w.review._update_buttons()
res["busy_disabled"] = not ext.btn.isEnabled() and not ext.view.isEnabled()
w.runner.proc = None
w.review._update_buttons()
import gui_ext_shorts
dlg = gui_ext_shorts.ShortsDialog(ext)
dlg.show()
for _ in range(5):
    app.processEvents()
dlg.grab().save(str(Path(SHOTS) / "gui_shorts_list.png"))
from PySide6.QtWidgets import QPushButton
texts = [b.text() for b in dlg.findChildren(QPushButton)]
res["plays"] = texts.count("▶ 再生")
res["folder"] = "フォルダを開く" in texts
dlg.reject()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, proj: Path, fx: Path):
    if not (proj / "shorts" / "shorts.json").exists():
        check("ショート: GUI（ショートが必要）", False, "先にショートの書き出しが成功している必要があります")
        return
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(proj)!r}\nSHOTS = {str(proj)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("ショート: GUI", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("ショート: GUI", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("ショート: GUIの拡張を読み込む", r["loaded"])
    check("ショート: 結果を開くとボタンが押せる", r["disabled_before"] and r["enabled_after"], r["view_text"])
    call = r["call"] or ["", []]
    check("ショート: ボタンで shorts.py を実行", call[0] == "shorts.py" and str(proj) in call[1]
          and call[1][call[1].index("--count") + 1] == "2" and "none" in call[1], str(call))
    check("ショート: 処理中はボタンを押せない", r["busy_disabled"])
    wc = r["whole_call"] or ["", []]
    check("ショート（全体を1本に）: ボタンで shorts.py --whole を実行", r["whole_btn"] and wc[0] == "shorts.py"
          and "--whole" in wc[1] and "--count" not in wc[1], str(wc))
    check("ショート（全体を1本に）: 一気に作るときは、演出プランが無ければ先に作る", r["plan_first"])
    check("ショート（全体を1本に）: 「作成」タブの「作るもの」で切り替える（初めは横長の動画）",
          r["target_default"] == "video" and r["target_text"][0] != r["target_text"][1] and "ショート" in r["target_text"][1],
          str(r["target_text"]))
    check("ショート: 一覧に再生ボタンとフォルダ", r["plays"] == 2 and r["folder"], str(r["plays"]))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    try:
        import cv2  # noqa: F401
    except ImportError:
        check("ショート: OpenCV が入っている", False, "pip install opencv-python-headless")
        return
    proj = make_project(fx)
    tpl_dir = fx / "tpl"
    t_units(check, proj, tpl_dir)
    t_ai_render(check, proj, tpl_dir)
    t_cli(check, out, fx)
    t_whole(check, out, fx)
    t_gui(check, proj, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    if not (out / "session.json").exists():
        print("（先に tests/smoke_test.py を実行すると、荒編集の結果を使うテストもできます）")
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
