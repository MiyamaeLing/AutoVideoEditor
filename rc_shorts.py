"""縦型ショート（1080x1920）の自動生成。

1. 荒編集の結果（発言・見どころ・強調テロップ）から、ショートに向いた区間（30〜60秒・冒頭につかみ）を1〜3本選ぶ
   - Claude API / claude.ai に貼り付け / AIなし（見どころ・強調テロップの周りから選ぶ）
2. その区間だけを書き出し、人の顔を追いかけて縦長に切り抜く（顔がなければ動きの多い所、それもなければ中央）
   - カメラが急に動かないように、少しの動きは無視して、なめらかに追いかける
3. 縦型向けの大きな字幕と、上部のタイトル帯を焼き込んで <出力フォルダ>/shorts/short_N.mp4 に保存
4. タイトル・説明文（#shorts 付き）を shorts/shorts.json に保存

見た目はテンプレートの [shorts] で変えられます。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import rc_render
from rc_export import build_captions
from rc_media import SourceSet, probe
from rc_template import Template, ass_color
from rc_timeline import Timeline
from rc_transcribe import Chunk, fmt_ts

OUT_W, OUT_H = 1080, 1920
ANALYSIS_FPS = 5          # 顔・動きを調べる間隔（1秒に何回）
ANALYSIS_W = 640          # 調べるときの画像の幅（小さくして速く・軽く）
PROMPT_LIMIT = 150_000    # Claude に送る文字数の上限（これを超える長い素材は見どころの周りだけ送る）

# テンプレートに [shorts] がない（古いテンプレート）ときの値
DEFAULTS = {
    "count": 3, "min_sec": 30, "max_sec": 60,
    "layout": "crop", "zoom": 1.0,
    "follow_smooth": 0.7, "follow_deadzone": 0.04, "follow_max_speed": 0.35,
    "band": True, "band_height": 300, "band_color": "#111111", "band_opacity": 0.88,
    "band_accent_color": "#FFE14D", "band_accent_height": 10,
    "title_font": "BIZ UDPGothic", "title_size": 100, "title_color": "#FFFFFF", "title_chars_per_line": 11,
    "caption_font": "BIZ UDPGothic", "caption_size": 84, "caption_color": "#FFFFFF",
    "caption_outline_color": "#111111", "caption_outline": 9, "caption_margin_bottom": 560, "caption_line_chars": 11,
    "telops": True, "telop_size": 88,
    "hashtags": ["shorts"],
}

SHORTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["shorts"],
    "properties": {
        "shorts": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["start_id", "end_id", "title", "description", "hashtags", "hook", "reason"],
                "properties": {
                    "start_id": {"type": "integer", "description": "ショートの最初の発言のID（ここがつかみ）"},
                    "end_id": {"type": "integer", "description": "ショートの最後の発言のID"},
                    "title": {"type": "string", "description": "ショートのタイトル。画面上部の帯にも出る。20字以内"},
                    "description": {"type": "string", "description": "説明文（2〜3行。ハッシュタグは含めない）"},
                    "hashtags": {"type": "array", "items": {"type": "string"}, "description": "#を付けないタグ 2〜4個"},
                    "hook": {"type": "string", "description": "冒頭のつかみ（最初の数秒で何が興味を引くか）"},
                    "reason": {"type": "string", "description": "この区間を選んだ理由"},
                },
            },
        },
    },
}


def cfg_of(tpl: Template) -> dict:
    c = dict(DEFAULTS)
    c.update(tpl.get("shorts", {}) or {})
    return c


# ============================================================
#  荒編集の結果を読む
# ============================================================
@dataclass
class Project:
    out: Path
    sess: dict
    chunks: list[Chunk]
    decisions: dict
    plan: dict
    meta: dict
    src: SourceSet
    tl: Timeline
    items: list[tuple[int, float, float, str]] = field(default_factory=list)  # (ID, 編集後の開始, 終了, 文字)

    @property
    def index(self) -> dict[int, int]:
        return {cid: k for k, (cid, *_r) in enumerate(self.items)}


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def load_project(out_dir: Path) -> Project:
    out_dir = Path(out_dir).resolve()
    sess = _read(out_dir / "session.json", None)
    if not sess:
        sys.exit(f"[エラー] {out_dir / 'session.json'} がありません。先に荒編集を実行してください。")
    chunks = [Chunk(**c) for c in _read(out_dir / "chunks.json", [])]
    decisions = _read(out_dir / "decisions_used.json", {}) or {}
    plan = {}
    for name in ("plan_edited.json", "plan_used.json"):   # 画面での手直しを優先（finish / rc_thumb と同じ順）
        if (out_dir / name).exists():
            plan = _read(out_dir / name, {}) or {}
            break
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    cut = set(sess.get("cut_ids", []))
    corr = {int(k): v for k, v in (sess.get("corrections") or {}).items()}
    items = []
    for c in chunks:
        if c.id in cut:
            continue
        r = tl.map_range(c.start, c.end)
        if r:
            items.append((c.id, r[0][0], r[-1][1], corr.get(c.id, c.text)))
    return Project(out_dir, sess, chunks, decisions, plan, sess.get("meta") or {}, src, tl, items)


# ============================================================
#  区間を選ぶ
# ============================================================
@dataclass
class Pick:
    start_id: int
    end_id: int
    title: str
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    hook: str = ""
    reason: str = ""
    by: str = "auto"
    a: float = 0.0       # 編集後の時刻（開始）
    b: float = 0.0       # 編集後の時刻（終了）


def seeds(pj: Project) -> list[tuple[int, float, str]]:
    """見どころ・強調テロップ・ズームの発言を (ID, 重み, ラベル) で返す（重い順ではなく、見つかった順）。"""
    ok = {i for i, *_ in pj.items}
    out = []
    for h in pj.decisions.get("highlights", []) or []:
        if h.get("id") in ok:
            out.append((h["id"], 3.0, h.get("label", "見どころ")))
    for t in pj.plan.get("telops", []) or []:
        if t.get("start_id") in ok:
            w = 2.0 if t.get("style") in ("emphasis", "funny") else 1.0
            out.append((t["start_id"], w, t.get("text", "")))
    for z in pj.plan.get("zooms", []) or []:
        if z.get("id") in ok:
            out.append((z["id"], 1.0, "ズーム"))
    return out


def fit_window(pj: Project, si: int, ei: int, min_sec: float, max_sec: float) -> tuple[int, int, float, float]:
    """items の si〜ei 番目を、min_sec〜max_sec 秒に収まるように広げる・縮める。

    冒頭のつかみを残すため、足りないときは後ろに広げる（動画の最後まで来たら前に広げる）。
    """
    items = pj.items
    n = len(items)
    si, ei = max(0, min(si, n - 1)), max(0, min(ei, n - 1))
    if ei < si:
        si, ei = ei, si

    def bounds(i, j):
        a = max(0.0, items[i][1] - 0.15)
        if i > 0:
            a = max(a, min(items[i - 1][2], items[i][1]))   # 前の発言の終わりまでは戻らない
        b = min(pj.tl.duration, items[j][2] + 0.4)
        if j + 1 < n:
            b = min(b, max(items[j + 1][1], items[j][2]))   # 次の発言の頭が入らないように
        return a, b

    a, b = bounds(si, ei)
    while b - a > max_sec and ei > si:
        ei -= 1
        a, b = bounds(si, ei)
    while b - a < min_sec:
        if ei + 1 < n and bounds(si, ei + 1)[1] - a <= max_sec:
            ei += 1
        elif si > 0 and b - bounds(si - 1, ei)[0] <= max_sec:
            si -= 1
        else:
            break
        a, b = bounds(si, ei)
    if b - a > max_sec:          # 1つの発言がとても長い場合
        b = a + max_sec
    return si, ei, a, b


def _overlap(p: Pick, others: list[Pick]) -> bool:
    for o in others:
        inter = min(p.b, o.b) - max(p.a, o.a)
        if inter > 0.25 * min(p.b - p.a, o.b - o.a):
            return True
    return False


def _title_from(pj: Project, label: str) -> str:
    label = (label or "").strip()
    if label and label not in ("ズーム", "見どころ"):
        return label[:20]
    titles = pj.meta.get("titles") or []
    return (titles[0] if titles else pj.sess.get("stem", "ショート"))[:20]


def pick_auto(pj: Project, count: int, min_sec: float, max_sec: float) -> list[Pick]:
    """AIなし：見どころ・強調テロップの周りから選ぶ（なければ、よく喋っている所）。"""
    if not pj.items:
        return []
    idx = pj.index
    sd = seeds(pj)
    cands = []
    if sd:
        for cid, _w, label in sd:
            si, ei, a, b = fit_window(pj, idx[cid], idx[cid], min_sec, max_sec)
            inside = [w for c, w, _ in sd if a - 0.01 <= pj.items[idx[c]][1] < b]
            density = sum(len(t) for _i, s, _e, t in pj.items[si:ei + 1]) / max(b - a, 1.0)
            cands.append((sum(inside) + 0.02 * density, si, ei, a, b, label))
    else:
        step = max(1, len(pj.items) // 60)   # 長い素材でも候補が多くなりすぎないように
        for k in range(0, len(pj.items), step):
            si, ei, a, b = fit_window(pj, k, k, min_sec, max_sec)
            density = sum(len(t) for _i, s, _e, t in pj.items[si:ei + 1]) / max(b - a, 1.0)
            cands.append((density, si, ei, a, b, ""))
    cands.sort(key=lambda c: (-c[0], c[3]))
    picks: list[Pick] = []
    for _score, si, ei, a, b, label in cands:
        p = Pick(pj.items[si][0], pj.items[ei][0], _title_from(pj, label), a=a, b=b,
                 reason=f"見どころ「{label}」の周り" if label else "よく喋っている所", by="auto")
        if not _overlap(p, picks):
            picks.append(p)
        if len(picks) >= count:
            break
    picks.sort(key=lambda p: p.a)
    for p in picks:
        p.description = _auto_description(pj, p)
        p.hashtags = list((pj.meta.get("tags") or [])[:3])
    return picks


def _auto_description(pj: Project, p: Pick) -> str:
    text = "".join(t for _i, s, _e, t in pj.items if p.a - 0.01 <= s < p.b)
    text = text.replace("\n", " ").strip()
    if len(text) > 70:
        text = text[:70] + "…"
    return text


# ---------- Claude ----------

def _instructions(mode: str, count: int, min_sec: float, max_sec: float) -> str:
    import rc_llm
    return f"""あなたはYouTubeショート（縦型の短い動画）の編集者です。カット済みの本編の文字起こしから、ショートとして切り出すのに向いた区間を選んでください。
本編のジャンル: {rc_llm.MODE_HINT.get(mode, '')}

## 入力の形式
`#ID [編集後の時刻] テキスト`。行の終わりに「★」があるのは、編集者が付けた見どころ・強調テロップです。

## 選び方
- 1〜{count}本。良い所が少なければ少なくてよい。区間どうしは重ねない
- 長さは{min_sec:.0f}〜{max_sec:.0f}秒（start_id の発言の頭から end_id の発言の終わりまで）
- **最初の数秒でつかみがある所から始める**（驚き・結論・問いかけ・面白い一言など）。あいさつや前置きから始めない
- 本編を見ていない人にも、その区間だけで話が分かること。オチや結論まで入れる
- title: 20字以内。画面上部の帯にも出る。思わず見たくなる短い言葉
- description: 説明文2〜3行（ハッシュタグは入れない。#shorts は自動で付きます）
- hashtags: # を付けないタグを2〜4個

出力はJSONのみです。"""


def ai_lines(pj: Project) -> str:
    marks: dict[int, list[str]] = {}
    for cid, _w, label in seeds(pj):
        marks.setdefault(cid, []).append(label)
    lines = [f"#{i} [{fmt_ts(s, ms=True)}] {t}" + (f" ★{'／'.join(marks[i])}" if i in marks else "")
             for i, s, e, t in pj.items]
    if sum(len(x) + 1 for x in lines) <= PROMPT_LIMIT:
        return "\n".join(lines)
    # とても長い素材：見どころの前後90秒と、全体から均等に選んだ所だけを送る
    keep = set()
    for cid in marks:
        t0 = pj.items[pj.index[cid]][1]
        keep |= {k for k, (_i, s, _e, _t) in enumerate(pj.items) if t0 - 20 <= s <= t0 + 90}
    step = max(1, len(pj.items) // 40)
    for k in range(0, len(pj.items), step):
        keep |= set(range(k, min(k + 25, len(pj.items))))
    out, size = [], 0
    for k in sorted(keep):
        size += len(lines[k]) + 1
        if size > PROMPT_LIMIT:
            break
        out.append(lines[k])
    return "\n".join(out)


def write_manual_prompt(pj: Project, count: int, min_sec: float, max_sec: float, path: Path) -> None:
    schema = json.dumps(SHORTS_SCHEMA, ensure_ascii=False, indent=1)
    path.write_text(_instructions(pj.sess.get("mode", "talk"), count, min_sec, max_sec)
                    + f"\n\n## 出力形式（JSON Schema）\n```json\n{schema}\n```\n\n## 文字起こし\n" + ai_lines(pj),
                    encoding="utf-8")


def ask_ai(pj: Project, count: int, min_sec: float, max_sec: float, effort: str) -> tuple[dict, dict]:
    import rc_llm
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    print("  Claudeでショートに向いた区間を選んでいます…", flush=True)
    res = rc_llm._call(client, anthropic, _instructions(pj.sess.get("mode", "talk"), count, min_sec, max_sec),
                       ai_lines(pj), SHORTS_SCHEMA, effort, usage)
    return res, usage


def picks_from_ai(pj: Project, res: dict, count: int, min_sec: float, max_sec: float) -> list[Pick]:
    """Claude の返答を確かめて Pick にする（存在しないID・長さの外れ・重なりを直す）。"""
    idx = pj.index
    picks: list[Pick] = []
    for s in res.get("shorts", []) or []:
        if s.get("start_id") not in idx:
            continue
        ei_id = s.get("end_id") if s.get("end_id") in idx else s["start_id"]
        si, ei, a, b = fit_window(pj, idx[s["start_id"]], idx[ei_id], min_sec, max_sec)
        tags = [str(t).lstrip("#＃").strip() for t in s.get("hashtags", []) or [] if str(t).strip()]
        p = Pick(pj.items[si][0], pj.items[ei][0], (s.get("title") or "").strip()[:30] or _title_from(pj, ""),
                 (s.get("description") or "").strip(), tags, s.get("hook", ""), s.get("reason", ""), "ai", a, b)
        if not _overlap(p, picks):
            picks.append(p)
        if len(picks) >= count:
            break
    return picks


# ============================================================
#  被写体を追いかける切り抜き
# ============================================================
_CASCADE = None


def face_detector():
    """OpenCV の顔検出（Haar）。日本語のフォルダでも読めるように、ファイルは中身を読んで渡す。"""
    global _CASCADE
    if _CASCADE is None:
        import cv2
        xml = (Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml").read_text(encoding="utf-8")
        fs = cv2.FileStorage(xml, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        cc = cv2.CascadeClassifier()
        if not cc.read(fs.getFirstTopLevelNode()) or cc.empty():
            raise RuntimeError("顔検出のデータを読み込めませんでした")
        _CASCADE = cc
    return _CASCADE


def _pick_face(faces, weights, prev: float | None, aw: int) -> float | None:
    """見つかった顔の中から追いかける1つを選ぶ（顔らしさ・大きさ・前の位置との近さで決める）"""
    if len(faces) == 0:
        return None
    ws = [float(w) for w in weights] if weights is not None and len(weights) == len(faces) else [1.0] * len(faces)
    best, best_sc = None, -1.0
    for (x, y, w, h), conf in zip(faces, ws):
        cx = (x + w / 2) / aw
        sc = max(conf, 0.01) * math.sqrt(w * h)   # 服の模様などの誤検出は「顔らしさ」が低い
        if prev is not None:   # 前に追いかけていた人の近くを優先（2人いても行ったり来たりしない）
            sc *= 1.0 - 0.6 * min(1.0, abs(cx - prev) / 0.3)
        if sc > best_sc:
            best, best_sc = cx, sc
    return best


def analyze(video: Path, crop_frac: float, cut_times: list[float]) -> list[tuple[float, float, str]]:
    """動画を1秒に数回だけ小さく読み込んで、追いかける位置（横方向 0〜1）を調べる。

    戻り値: [(時刻, 中心x, 理由 face/motion/center)]。動画全体をメモリに載せず、1枚ずつ処理する。
    """
    import cv2
    det = face_detector()
    info = probe_size(video)
    vw, vh = info
    aw = ANALYSIS_W
    ah = max(2, int(round(vh * aw / vw / 2)) * 2)
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-an", "-vf",
           f"fps={ANALYSIS_FPS},scale={aw}:{ah},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = aw * ah
    out = []
    recent: list = []   # 少し前の画像（ゆっくりした動きも分かるように、0.6秒前と比べる）
    last_face, last_face_t = None, -99.0
    cuts = sorted(cut_times)
    k = 0
    min_face = max(16, int(ah * 0.07))
    win = max(1, int(round(crop_frac * aw)))
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            t = k / ANALYSIS_FPS
            k += 1
            img = np.frombuffer(buf, np.uint8).reshape(ah, aw)
            # 直前にカット（つなぎ目）があれば、前の画像との差や前の顔は使わない
            if any(t - 1.0 / ANALYSIS_FPS < c <= t for c in cuts):
                recent, last_face, last_face_t = [], None, -99.0
            faces, _levels, weights = det.detectMultiScale3(img, scaleFactor=1.1, minNeighbors=5,
                                                            minSize=(min_face, min_face), outputRejectLevels=True)
            cx = _pick_face(faces, weights, last_face, aw)
            blur = cv2.GaussianBlur(img, (5, 5), 0)
            if cx is not None:
                last_face, last_face_t = cx, t
                out.append((t, cx, "face"))
            elif last_face is not None and t - last_face_t <= 1.2:
                out.append((t, last_face, "face"))   # 一瞬見失っただけなら、その場にとどまる
            else:
                mx = None
                if recent:
                    diff = cv2.absdiff(blur, recent[0])
                    mask = (diff > 25).astype(np.float32)
                    if mask.mean() > 0.0015:
                        col = mask.sum(axis=0)
                        cs = np.concatenate([[0.0], np.cumsum(col)])
                        sums = cs[win:] - cs[:-win] if win < aw else np.array([cs[-1]])
                        j = int(np.argmax(sums))
                        part = col[j:j + win]   # 一番動きの多い範囲の中の、動きの重心
                        mx = float((np.arange(j, j + len(part)) + 0.5) @ part / max(part.sum(), 1e-6)) / aw
                out.append((t, mx if mx is not None else 0.5, "motion" if mx is not None else "center"))
            recent = (recent + [blur])[-3:]
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    return out


def probe_size(video: Path) -> tuple[int, int]:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0:s=x", str(video)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    w, h = (int(v) for v in p.stdout.strip().split("x")[:2])
    return w, h


def _median(x: np.ndarray, k: int = 5) -> np.ndarray:
    if len(x) < 3:
        return x.copy()
    r = k // 2
    pad = np.pad(x, r, mode="edge")
    return np.array([np.median(pad[i:i + k]) for i in range(len(x))])


def smooth_path(samples: list[tuple[float, float, str]], fps: float, n_frames: int, cut_frames: list[int],
                cfg: dict) -> np.ndarray:
    """調べた位置から、1フレームごとのなめらかな中心位置（0〜1）を作る。

    つなぎ目（カット）ごとに別々に計算するので、カットの所では切り替わってよい（編集の「カット」と同じ）。
    """
    centers = np.full(n_frames, 0.5)
    if not samples or n_frames <= 0:
        return centers
    st = np.array([s[0] for s in samples])
    sx = np.array([s[1] for s in samples])
    dz = float(cfg.get("follow_deadzone", 0.06))
    tau = max(0.05, float(cfg.get("follow_smooth", 0.7)))
    vmax = float(cfg.get("follow_max_speed", 0.35)) / fps     # 1フレームで動ける量（画面の幅に対する割合）
    bounds = sorted({0, n_frames, *[c for c in cut_frames if 0 < c < n_frames]})
    for f0, f1 in zip(bounds, bounds[1:]):
        t0, t1 = f0 / fps, f1 / fps
        sel = (st >= t0 - 1e-6) & (st < t1)
        if not sel.any():
            j = int(np.argmin(np.abs(st - (t0 + t1) / 2)))
            ts, xs = np.array([t0]), np.array([sx[j]])
        else:
            ts, xs = st[sel], _median(sx[sel])
        # 少しの動きは無視する（デッドゾーン）
        held, cur = [], xs[0]
        for i, x in enumerate(xs):
            if abs(x - cur) > dz:
                cur = x   # 外れたら、もう一度真ん中に入れ直す（なめらかさは下の処理で付ける）
            elif i >= ANALYSIS_FPS and abs(x - cur) > dz * 0.3:
                # 少しずれた所で1秒ほど止まっているなら、ゆっくり真ん中に入れ直す
                last = xs[i - ANALYSIS_FPS:i + 1]
                if float(last.max() - last.min()) < dz * 0.3:
                    cur = float(last.mean())
            held.append(cur)
        held = np.array(held)
        # 前後両方向からなめらかにする（遅れが出ない）
        a = 1 - math.exp(-1.0 / (tau * ANALYSIS_FPS))
        fwd = held.copy()
        for i in range(1, len(fwd)):
            fwd[i] = fwd[i - 1] + a * (held[i] - fwd[i - 1])
        both = fwd.copy()
        for i in range(len(both) - 2, -1, -1):
            both[i] = both[i + 1] + a * (fwd[i] - both[i + 1])
        ft = np.arange(f0, f1) / fps
        path = np.interp(ft, ts, both)
        # 速く動きすぎないようにする
        for i in range(1, len(path)):
            d = path[i] - path[i - 1]
            if abs(d) > vmax:
                path[i] = path[i - 1] + math.copysign(vmax, d)
        centers[f0:f1] = path
    return centers


def crop_box(vw: int, vh: int, zoom: float = 1.0) -> tuple[int, int, int]:
    """縦長（9:16）に切り抜く大きさ (幅, 高さ, 上端) 。横長の素材なら高さいっぱい、縦長の素材なら幅いっぱい。"""
    zoom = max(1.0, float(zoom))
    ch = vh / zoom
    cw = ch * 9 / 16
    if cw > vw:   # 素材がもともと細長い（縦型スマホなど）
        cw = vw / zoom
        ch = cw * 16 / 9
    cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2
    return cw, ch, (vh - ch) // 2


def write_crop_cmds(path: Path, centers: np.ndarray, vw: int, cw: int, fps: float) -> tuple[int, int]:
    """FFmpeg の sendcmd 用に、フレームごとの切り抜き位置を書く。(最初の位置, 動く回数) を返す。"""
    xs = np.clip(np.round(centers * vw - cw / 2), 0, vw - cw).astype(int)
    lines, last = [], int(xs[0]) if len(xs) else 0
    for n in range(1, len(xs)):
        x = int(xs[n])
        if x != last:
            lines.append(f"{(n - 0.5) / fps:.4f} crop x {x};")
            last = x
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (int(xs[0]) if len(xs) else 0), len(lines)


# ============================================================
#  字幕・タイトル帯（ASS）
# ============================================================
def _t(sec: float) -> str:
    cs = int(round(max(sec, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def _kind(ch: str) -> str:
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "H"
    if 0x30A0 <= o <= 0x30FF:
        return "K"
    if 0x4E00 <= o <= 0x9FFF or ch in "々〇":
        return "C"
    return "O"


def wrap_title(text: str, per_line: int) -> list[str]:
    """タイトルを2行までに分ける（句読点や、ひらがな→漢字の切れ目など、自然な所で）。"""
    text = text.replace("\\n", "\n").strip()
    if "\n" in text:
        return [s.strip() for s in text.split("\n") if s.strip()][:2]
    if len(text) <= per_line:
        return [text]
    mid = len(text) / 2
    best, best_score = len(text) // 2, 1e9
    for k in range(2, len(text) - 1):
        prev, nxt = text[k - 1], text[k]
        if prev in "、。！？!? ・":
            rank = 0
        elif _kind(prev) == "H" and _kind(nxt) in "CK":
            rank = 1
        elif _kind(prev) != _kind(nxt) and not (_kind(prev) == "C" and _kind(nxt) == "H"):
            rank = 2
        else:
            rank = 5
        score = rank * 3 + abs(k - mid)
        if score < best_score:
            best, best_score = k, score
    return [text[:best].strip(), text[best:].strip()]


def _style(name, font, size, color, outline_color, outline, align, margin_v, margin_lr=60, bold=True, border=1):
    c = ass_color(color)
    return (f"Style: {name},{font},{size},{c},{c},{ass_color(outline_color)},&H80000000,{-1 if bold else 0},0,0,0,"
            f"100,100,0,0,{border},{outline},0,{align},{margin_lr},{margin_lr},{margin_v},1")


def build_ass(tpl: Template, cfg: dict, title: str, captions, telops, duration: float) -> str:
    """captions: [(s, e, text)] / telops: [(s, e, text, style)] ショートの中の時刻"""
    band_h = int(cfg["band_height"]) if cfg.get("band", True) else 0
    styles = [
        _style("Caption", cfg["caption_font"], cfg["caption_size"], cfg["caption_color"],
               cfg["caption_outline_color"], cfg["caption_outline"], 2, int(cfg["caption_margin_bottom"]), 70),
        _style("Band", cfg["title_font"], 20, "#FFFFFF", "#000000", 0, 7, 0, 0, bold=False),
    ]
    lines = wrap_title(title, int(cfg["title_chars_per_line"]))
    longest = max((len(s) for s in lines), default=1)
    tsize = int(cfg["title_size"])
    tsize = max(40, min(tsize, int((OUT_W - 100) / max(longest, 1)), int((band_h - 40) / max(len(lines), 1) / 1.2)
                        if band_h else tsize))
    styles.append(_style("Title", cfg["title_font"], tsize, cfg["title_color"], cfg.get("title_outline_color", "#000000"),
                         2 if band_h else 8, 5, 0, 40))
    tel = tpl.get("telop", {}) or {}
    for name in ("emphasis", "info", "warning", "funny"):
        c = tel.get(name, {}) or {}
        styles.append(_style(f"T_{name}", c.get("font", cfg["title_font"]), int(cfg["telop_size"]),
                             c.get("color", "#FFE14D"), c.get("outline_color", c.get("box_color", "#1A1A1A")),
                             max(6, int(c.get("outline", 8))), 8, band_h + 70, 60))

    ev = []
    end = _t(duration + 0.5)
    if band_h:
        alpha = int(round((1 - float(cfg["band_opacity"])) * 255))
        col = ass_color(cfg["band_color"])[4:]   # &H00BBGGRR → BBGGRR
        ev.append(f"Dialogue: 0,{_t(0)},{end},Band,,0,0,0,,{{\\pos(0,0)\\1c&H{col}&\\1a&H{alpha:02X}&\\bord0\\shad0\\p1}}"
                  f"m 0 0 l {OUT_W} 0 l {OUT_W} {band_h} l 0 {band_h}{{\\p0}}")
        ah = int(cfg.get("band_accent_height", 0) or 0)
        if ah > 0:
            acc = ass_color(cfg["band_accent_color"])[4:]
            ev.append(f"Dialogue: 0,{_t(0)},{end},Band,,0,0,0,,{{\\pos(0,{band_h})\\1c&H{acc}&\\bord0\\shad0\\p1}}"
                      f"m 0 0 l {OUT_W} 0 l {OUT_W} {ah} l 0 {ah}{{\\p0}}")
        ty = band_h / 2
    else:
        ty = 200
    ev.append(f"Dialogue: 1,{_t(0)},{end},Title,,0,0,0,,{{\\pos({OUT_W // 2},{ty:.0f})\\fad(200,0)}}"
              + _esc("\n".join(lines)))
    per_line = int(cfg.get("caption_line_chars", 11))
    for s, e, text in captions:
        # 縦型は横幅がせまいので、長い字幕は自然な所で2行にする
        ev.append(f"Dialogue: 2,{_t(s)},{_t(e)},Caption,,0,0,0,,{_esc(chr(10).join(wrap_title(text, per_line)))}")
    for s, e, text, style in telops:
        ev.append(f"Dialogue: 3,{_t(s)},{_t(e)},T_{style},,0,0,0,,"
                  r"{\fscx40\fscy40\t(0,110,\fscx112\fscy112)\t(110,190,\fscx100\fscy100)\fad(0,160)}" + _esc(text))
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *styles, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *ev, "",
    ])


# ============================================================
#  書き出し
# ============================================================
def range_segments(tl: Timeline, a: float, b: float) -> tuple[list[rc_render.Seg], list[int]]:
    """編集後の時刻 a〜b に入る素材の部分と、つなぎ目のフレーム位置を返す。"""
    fps = tl.fps
    fa, fb = round(a * fps), round(b * fps)
    segs, cuts, cur = [], [], 0
    for p in tl.pieces:
        s, e = max(p.seq_start, fa), min(p.seq_end, fb)
        if e - s < 2:
            continue
        f = tl.sources.files[p.file_index]
        local = p.g_in - tl.sources.offsets[p.file_index] + float((s - p.seq_start) / fps)
        if segs:
            cuts.append(cur)
        segs.append(rc_render.Seg(f.path, local, e - s, 1.0, True))
        cur += e - s
    return segs, cuts


def body_size(src: SourceSet) -> tuple[int, int]:
    """中間ファイルの大きさ。素材の大きさのまま（4Kより大きければ縮める）。"""
    w, h = src.main.width, src.main.height
    if h > 2160:
        w, h = w * 2160 / h, 2160
    return int(w) // 2 * 2, int(h) // 2 * 2


def captions_in(pj: Project, a: float, b: float, max_chars: int):
    items = [(max(s, a) - a, min(e, b) - a, t) for _i, s, e, t in pj.items if e > a + 0.05 and s < b - 0.05]
    return build_captions(items, max_chars)


def telops_in(pj: Project, a: float, b: float) -> list[tuple[float, float, str, str]]:
    span = {i: (s, e) for i, s, e, _t in pj.items}
    out = []
    for t in pj.plan.get("telops", []) or []:
        if t.get("start_id") not in span:
            continue
        s = span[t["start_id"]][0]
        e = span.get(t.get("end_id"), span[t["start_id"]])[1]
        e = min(max(e, s + 1.8), s + 6.0)
        if a <= s < b - 1.0:
            out.append((s - a, min(e, b) - a, t.get("text", ""), t.get("style", "emphasis")
                        if t.get("style") in ("emphasis", "info", "warning", "funny") else "emphasis"))
    return out


def render_short(pj: Project, pick: Pick, tpl: Template, cfg: dict, out_path: Path, work: Path,
                 inter_args: list[str], final_args: list[str], bgm_sections=None) -> dict:
    """bgm_sections: [(開始, 終了, ファイル)]（ショートの中の時刻）。あれば、声に合わせて下げながら重ねる"""
    fps = pj.tl.fps
    segs, cuts = range_segments(pj.tl, pick.a, pick.b)
    if not segs:
        raise RuntimeError("この区間の映像が見つかりませんでした")
    audio = {}
    for s in segs:
        if s.path not in audio:
            audio[s.path] = rc_render.has_audio(s.path)
        s.has_audio = audio[s.path]
    vw, vh = body_size(pj.src)
    work.mkdir(parents=True, exist_ok=True)
    body = rc_render.render_body(segs, vw, vh, fps, work, inter_args)
    n_frames = sum(s.frames for s in segs)
    dur = float(n_frames / fps)

    layout = str(cfg.get("layout", "crop"))
    cw, ch, cy = crop_box(vw, vh, float(cfg.get("zoom", 1.0)))
    follow = {"face": 0, "motion": 0, "center": 0}
    vparts = []
    if layout == "blur":
        vparts.append(f"[0:v]split[bg0][fg0];[bg0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                      f"crop={OUT_W}:{OUT_H},gblur=sigma=40,eq=brightness=-0.12[bg];"
                      f"[fg0]scale={OUT_W}:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1")
    else:
        if cw < vw - 2:   # 横に動かせる余地があるときだけ追いかける
            print("    被写体の位置を調べています", flush=True)
            samples = analyze(body, cw / vw, [c / float(fps) for c in cuts])
            for _t0, _x, why in samples:
                follow[why] += 1
            centers = smooth_path(samples, float(fps), n_frames, cuts, cfg)
        else:
            centers = np.full(n_frames, 0.5)
        x0, moves = write_crop_cmds(work / "crop.txt", centers, vw, cw, float(fps))
        vparts.append(f"[0:v]{'sendcmd=f=crop.txt,' if moves else ''}crop={cw}:{ch}:{x0}:{cy},"
                      f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1")

    caps = captions_in(pj, pick.a, pick.b, 2 * int(cfg.get("caption_line_chars", 11)))
    telops = telops_in(pj, pick.a, pick.b) if cfg.get("telops", True) else []
    (work / "subs.ass").write_text(build_ass(tpl, cfg, pick.title, caps, telops, dur), encoding="utf-8")
    fonts = tpl.fonts_dir
    if fonts and not (work / "fonts").exists():
        shutil.copytree(fonts, work / "fonts")
    vf = vparts[0] + ",ass=subs.ass" + (":fontsdir=fonts" if fonts else "") + "[v]"
    target = (tpl.get("audio", {}) or {}).get("loudness", -14.0)
    ain, extra = "0:a", []
    if bgm_sections:
        print("    BGMを重ねています", flush=True)
        rc_render.mix_audio(body.resolve(), dur, bgm_sections, [], tpl, work)
        ain, extra = "1:a", ["-i", "mix.wav"]
    af = f"[{ain}]loudnorm=I={target}:TP=-1.5:LRA=11,aresample={rc_render.SR}[a]"
    out_path = Path(out_path).resolve()
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(body.resolve()), *extra, "-filter_complex", f"{vf};{af}",
           "-map", "[v]", "-map", "[a]", *final_args, "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(out_path)]
    print("    最終書き出し（切り抜き・字幕・タイトル帯）", flush=True)
    rc_render.run_progress(cmd, dur, cwd=work, what="ショートの書き出し")
    return {"duration": round(dur, 2), "follow": follow}


def make_thumb(video: Path, out: Path, t: float = 1.0) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=270:480", "-q:v", "3", str(out)], capture_output=True)


def description_text(p: Pick, cfg: dict) -> str:
    tags = []
    for t in list(p.hashtags) + list(cfg.get("hashtags") or ["shorts"]) + ["shorts"]:
        t = str(t).lstrip("#＃").strip().replace(" ", "")
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    # #shorts は先頭に置く
    tags.sort(key=lambda t: t.lower() != "shorts")
    body = p.description.strip()
    return (body + "\n\n" if body else "") + " ".join(f"#{t}" for t in tags[:5])


# ============================================================
#  素材全体を1本のショートに（ショート単体で作る）
# ============================================================
WHOLE_LIMIT = 180.0   # YouTube がショートとして扱う長さの上限（秒）
WHOLE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "description", "hashtags", "hook"],
    "properties": {
        "title": {"type": "string", "description": "ショートのタイトル。画面上部の帯にも出る。20字以内"},
        "description": {"type": "string", "description": "説明文（2〜3行。ハッシュタグは含めない）"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "#を付けないタグ 2〜4個"},
        "hook": {"type": "string", "description": "冒頭のつかみ（最初の数秒で何が興味を引くか）"},
    },
}


def whole_names(out_dir: Path) -> tuple[Path, Path, Path]:
    """素材全体のショートの (動画, 一覧用の画像, 情報のJSON)"""
    out_dir = Path(out_dir)
    stem = (_read(out_dir / "session.json", {}) or {}).get("stem") or "short"
    return out_dir / f"{stem}_ショート.mp4", out_dir / f"{stem}_ショート.jpg", out_dir / "short_whole.json"


def _whole_instructions(mode: str) -> str:
    import rc_llm
    return f"""あなたはYouTubeショート（縦型の短い動画）の編集者です。カット済みの動画の文字起こしを読んで、この動画を1本のショートとして公開するときのタイトルなどを決めてください。
動画のジャンル: {rc_llm.MODE_HINT.get(mode, '')}

- title: 20字以内。画面上部の帯にも出る。思わず見たくなる短い言葉
- description: 説明文2〜3行（ハッシュタグは入れない。#shorts は自動で付きます）
- hashtags: # を付けないタグを2〜4個
- hook: 冒頭のつかみ（最初の数秒で何が興味を引くか）

出力はJSONのみです。"""


def _flat(text) -> str:
    return str(text).replace(chr(92) + "n", "").replace(chr(10), "").strip()


def whole_pick(pj: Project, llm: str, effort: str) -> tuple[Pick, dict | None]:
    """素材全体（カット後）を1本にする Pick。タイトルなどは、AI（api）か、荒編集で作ったタイトル案・タグから"""
    p = Pick(pj.items[0][0], pj.items[-1][0], "", by="whole", a=0.0, b=pj.tl.duration)
    usage = None
    if llm == "api":
        try:
            import rc_llm
            anthropic, client = rc_llm._client()
            usage = {"in": 0, "out": 0}
            print("  Claudeでタイトル・説明文を考えています…", flush=True)
            res = rc_llm._call(client, anthropic, _whole_instructions(pj.sess.get("mode", "talk")), ai_lines(pj),
                               WHOLE_SCHEMA, effort, usage)
            p.title = _flat(res.get("title") or "")[:30]
            p.description = str(res.get("description") or "").strip()
            p.hashtags = [str(t).lstrip("#＃").strip() for t in res.get("hashtags") or [] if str(t).strip()]
            p.hook = str(res.get("hook") or "")
            p.reason = "素材全体を1本に（タイトルなどはAI）"
        except (Exception, SystemExit) as e:  # noqa: BLE001  AIが使えなくても（APIキーなしも）、タイトル案から作って続ける
            print(f"  [注意] AIでタイトルを作れなかったので、タイトル案から作ります: {e}")
    if not p.title:
        texts = pj.meta.get("thumbnail_texts") or []
        p.title = (_flat(texts[0]) if texts else "")[:20] or _title_from(pj, "")
        p.description = _auto_description(pj, p)
        p.hashtags = [str(t).lstrip("#＃").strip() for t in (pj.meta.get("tags") or [])[:3] if str(t).strip()]
        p.reason = "素材全体を1本に"
    return p, usage


def whole_bgm(pj: Project, tpl: Template, dur: float) -> list[tuple[float, float, Path]]:
    """演出プランのBGMの区間（finish.py と同じ決め方）。プランやBGMの曲が無ければ空"""
    import itertools
    span = {i: s for i, s, _e, _t in pj.items}
    cyc = {m: itertools.cycle(fs) for m, fs in tpl.bgm_files().items() if fs}
    starts = [(span[b["start_id"]], b.get("mood")) for b in pj.plan.get("bgm", []) or [] if b.get("start_id") in span]
    if starts:
        starts[0] = (0.0, starts[0][1])
    out = []
    for (s, mood), nxt in zip(starts, starts[1:] + [(dur, None)]):
        e = min(nxt[0], dur)
        if mood in cyc and e - s > 1:
            out.append((s, e, next(cyc[mood])))
    return out


def generate_whole(out_dir: Path, tpl: Template, llm: str = "api", effort: str = "medium",
                   keep_work: bool = False) -> Path:
    """カット後の素材全体を、1本の縦型ショートにする（本編を作らずに、ショートだけ作りたいとき）"""
    t0 = time.time()
    cfg = cfg_of(tpl)
    pj = load_project(out_dir)
    print("■ 素材全体を1本のショートにします")
    if not pj.items:
        sys.exit("[エラー] 発言が見つからないので、ショートを作れません。")
    dur = pj.tl.duration
    print(f"  長さ: {fmt_ts(dur)}")
    over = dur > WHOLE_LIMIT
    if over:
        print(f"  [注意] {WHOLE_LIMIT / 60:.0f}分を超えています。YouTube ではショートとして扱われません"
              "（カットを増やすか、「ショートを作る」で短い区間を切り出してください）。このまま作ります。")
    if llm == "manual":
        print("  [お知らせ] タイトル・説明文は、荒編集のタイトル案から作ります（claude.ai への貼り付けはありません）。")
    pick, usage = whole_pick(pj, llm, effort)
    print(f"  タイトル「{pick.title}」")
    video, thumb, info_path = whole_names(pj.out)
    bgm = whole_bgm(pj, tpl, dur)
    final_args, inter_args, enc = rc_render.pick_encoder((tpl.get("video", {}) or {}).get("encoder", "auto"),
                                                         int((tpl.get("video", {}) or {}).get("quality", 19)))
    print(f"■ ショートを書き出しています（{enc}{'・BGMあり' if bgm else ''}）")
    work = pj.out / "shorts" / "_work_whole"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    r = render_short(pj, pick, tpl, cfg, video, work, inter_args, final_args, bgm)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    make_thumb(video, thumb, min(1.0, r["duration"] / 2))
    info = {"no": 1, "file": video.name, "thumb": thumb.name, "title": pick.title,
            "description": description_text(pick, cfg), "hashtags": pick.hashtags, "hook": pick.hook,
            "reason": pick.reason, "start": 0.0, "end": round(dur, 2), "duration": r["duration"],
            "start_id": pick.start_id, "end_id": pick.end_id, "by": "whole", "follow": r["follow"],
            "bgm": bool(bgm), "over_limit": over, "layout": cfg.get("layout", "crop")}
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print("■ ショート完成")
    print(f"  {video}")
    print(f"  タイトル・説明文: {info_path}")
    print(f"  所要時間: {fmt_ts(time.time() - t0)}")
    if usage and usage.get("in"):
        import rc_llm
        print(f"  Claude API: {rc_llm.cost_text(usage)}")
    return video


# ============================================================
#  まとめ
# ============================================================
def generate(out_dir: Path, tpl: Template, llm: str = "api", effort: str = "medium", count: int | None = None,
             picks_file: Path | None = None, keep_work: bool = False) -> list[Path]:
    t0 = time.time()
    cfg = cfg_of(tpl)
    count = max(1, min(3, int(count or cfg.get("count", 3))))
    min_sec, max_sec = float(cfg["min_sec"]), float(cfg["max_sec"])
    pj = load_project(out_dir)
    sdir = pj.out / "shorts"
    sdir.mkdir(exist_ok=True)
    print(f"■ ショートの区間を選んでいます（最大{count}本・{min_sec:.0f}〜{max_sec:.0f}秒）")
    if not pj.items:
        sys.exit("[エラー] 発言が見つからないので、ショートの区間を選べません。")
    if pj.tl.duration < min_sec:
        print(f"  [注意] 本編が{pj.tl.duration:.0f}秒しかないので、本編全体を1本のショートにします。")

    usage = None
    picks: list[Pick] = []
    if picks_file:
        s = Path(picks_file).read_text(encoding="utf-8-sig")
        res = json.loads(s[s.find("{"): s.rfind("}") + 1])
        picks = picks_from_ai(pj, res, count, min_sec, max_sec)
        print(f"  {Path(picks_file).name} の区間を使います")
    elif llm == "api" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("  [お知らせ] APIキーが設定されていないので、見どころから自動で選びます。")
    elif llm == "api":
        try:
            res, usage = ask_ai(pj, count, min_sec, max_sec, effort)
            picks = picks_from_ai(pj, res, count, min_sec, max_sec)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001  AIが使えなくても、見どころから選んで続ける
            print(f"  [注意] AIでの選択に失敗したので、見どころから自動で選びます: {e}")
    elif llm == "manual":
        p = sdir / "prompt_shorts.md"
        write_manual_prompt(pj, count, min_sec, max_sec, p)
        print(f"  claude.ai に貼るプロンプトを作りました: {p}")
        print("  → 返答を claude_shorts.json に保存して、--picks claude_shorts.json を付けてもう一度実行してください。")
        return []
    if not picks:
        if llm == "api" or picks_file:
            print("  [お知らせ] AIの答えに使える区間がなかったので、見どころから自動で選びます。")
        picks = pick_auto(pj, count, min_sec, max_sec)
    if not picks:
        sys.exit("[エラー] ショートにできる区間が見つかりませんでした。")
    for i, p in enumerate(picks, 1):
        print(f"  {i}. {fmt_ts(p.a)}〜{fmt_ts(p.b)}（{p.b - p.a:.0f}秒）「{p.title}」")

    final_args, inter_args, enc = rc_render.pick_encoder((tpl.get("video", {}) or {}).get("encoder", "auto"),
                                                         int((tpl.get("video", {}) or {}).get("quality", 19)))
    for old in list(sdir.glob("short_*.mp4")) + list(sdir.glob("short_*.jpg")):
        old.unlink()
    outs, info = [], []
    for i, p in enumerate(picks, 1):
        print(f"■ ショート {i}/{len(picks)} を書き出しています（{enc}）")
        work = sdir / f"_work_{i}"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        path = sdir / f"short_{i}.mp4"
        r = render_short(pj, p, tpl, cfg, path, work, inter_args, final_args)
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        make_thumb(path, sdir / f"short_{i}.jpg", min(1.0, r["duration"] / 2))
        outs.append(path)
        src_t = next((c.start for c in pj.chunks if c.id == p.start_id), 0.0)
        info.append({
            "no": i, "file": path.name, "thumb": f"short_{i}.jpg", "title": p.title,
            "description": description_text(p, cfg), "hashtags": p.hashtags, "hook": p.hook, "reason": p.reason,
            "start": round(p.a, 2), "end": round(p.b, 2), "duration": r["duration"],
            "start_id": p.start_id, "end_id": p.end_id, "source_start": round(src_t, 2),
            "by": p.by, "follow": r["follow"],
        })
    (sdir / "shorts.json").write_text(json.dumps({"layout": cfg.get("layout", "crop"), "shorts": info},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    print("■ ショート完成")
    print(f"  {len(outs)}本: {sdir}")
    print(f"  タイトル・説明文: {sdir / 'shorts.json'}")
    print(f"  所要時間: {fmt_ts(time.time() - t0)}")
    if usage and usage.get("in"):
        import rc_llm
        print(f"  Claude API: {rc_llm.cost_text(usage)}")
    return outs

