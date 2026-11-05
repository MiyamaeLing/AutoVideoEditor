"""サムネイル案の自動生成。

1. 見どころ・強調テロップ・ズームの場面を優先して、候補の静止画を約12枚取り出す（ブレた画像は除く）
2. Claude に候補画像を見せて、サムネイル向きの3枚と文字を選んでもらう（AIなしならシャープさで選ぶ）
3. 3種類のデザインで合成して、1280x720 の JPEG を書き出す
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from rc_media import SourceSet, probe
from rc_template import Template
from rc_timeline import Timeline

W, H = 1280, 720
N_CANDIDATES = 12

PICK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["picks"],
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["frame", "text", "reason"],
                "properties": {
                    "frame": {"type": "integer", "description": "候補画像の番号"},
                    "text": {"type": "string", "description": "サムネイルの文字。12字以内。改行は\\n（2行まで）"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


# ---------------- フォント ----------------

def find_font(tpl: Template) -> tuple[str, int]:
    cfg = tpl.get("thumbnail", {})
    cands: list[tuple[str, int]] = []
    if cfg.get("font_file"):
        p = Path(cfg["font_file"])
        if not p.is_absolute():
            p = tpl.dir / p
        cands.append((str(p), int(cfg.get("font_index", 0))))
    if tpl.fonts_dir:
        cands += [(str(p), 0) for p in sorted(tpl.fonts_dir.glob("*")) if p.suffix.lower() in (".ttf", ".otf", ".ttc")]
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    cands += [(str(win / "BIZ-UDGothicB.ttc"), 1), (str(win / "YuGothB.ttc"), 0), (str(win / "meiryob.ttc"), 0),
              ("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 0),
              ("/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf", 0)]
    for path, idx in cands:
        if Path(path).exists():
            for i in (idx, 0):
                try:
                    ImageFont.truetype(path, 40, index=i)
                    return path, i
                except OSError:
                    continue
    raise SystemExit("[エラー] サムネイル用の日本語フォントが見つかりません。テンプレートの [thumbnail] font_file を設定してください。")


# ---------------- 候補の静止画 ----------------

def _grab(path: Path, t: float, out: Path) -> bool:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", "-q:v", "2",
                        str(out)], capture_output=True)
    return p.returncode == 0 and out.exists()


def sharpness(img: Image.Image) -> float:
    g = np.asarray(img.convert("L").resize((320, 180)), dtype=np.float32)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def candidate_times(sess: dict, tl: Timeline, chunks: dict, decisions: dict, plan: dict) -> list[tuple[float, str]]:
    """優先度の高い順に (元素材の時刻, 理由) を返す。"""
    cut = set(sess.get("cut_ids", []))
    want: list[tuple[float, str]] = []

    def add(cid, why):
        c = chunks.get(cid)
        if c and cid not in cut:
            want.append(((c["start"] + c["end"]) / 2, why))
    for h in decisions.get("highlights", []):
        add(h["id"], f"見どころ: {h.get('label', '')}")
    for t in plan.get("telops", []):
        if t.get("style") in ("emphasis", "funny"):
            add(t["start_id"], f"テロップ: {t.get('text', '')}")
    for z in plan.get("zooms", []):
        add(z["id"], "ズーム")
    # 残りは本編全体から均等に
    total = tl.duration
    for k in range(N_CANDIDATES):
        ot = total * (k + 0.5) / N_CANDIDATES
        for p in tl.pieces:
            s, e = p.seq_start / float(tl.fps), p.seq_end / float(tl.fps)
            if s <= ot < e:
                # 早送り（rc_speedup.SpeedTimeline）の区間は、完成の1秒が元素材の「倍率」秒にあたる
                speed = (p.g_out - p.g_in) / (e - s) if e > s else 1.0
                want.append((min(p.g_out - 0.01, p.g_in + (ot - s) * max(1.0, speed)), "全体から"))
                break
    out: list[tuple[float, str]] = []
    for t, why in want:
        if all(abs(t - u) > 4 for u, _ in out) and tl.map_time(t) is not None:
            out.append((t, why))
        if len(out) >= N_CANDIDATES:
            break
    return out


def extract_candidates(src: SourceSet, times, work: Path, key=None) -> list[dict]:
    work.mkdir(parents=True, exist_ok=True)
    cands = []
    for i, (t, why) in enumerate(times, 1):
        best = None
        for dt in (0.0, -0.4, 0.4):   # 少しずらした3枚から一番くっきりしたものを使う
            fi_list = src.split(t + dt, t + dt + 0.05)
            if not fi_list:
                continue
            fi, local, _ = fi_list[0]
            f = work / f"c{i:02d}_{dt:+.1f}.jpg"
            if _grab(src.files[fi].path, local, f):
                img = Image.open(f).convert("RGB")
                sc = sharpness(img)
                k = key(img, sc) if key else sc   # key: 目が開いている顔を優先する（rc_thumbspro）
                if best is None or k > best[2]:
                    best = (sc, img, k)
        if best:
            path = work / f"cand_{i:02d}.jpg"
            best[1].save(path, quality=92)
            cands.append({"no": len(cands) + 1, "t": t, "why": why, "sharp": best[0], "path": path})
    # ブレが大きいもの（中央値の25%未満）は除く
    if cands:
        med = float(np.median([c["sharp"] for c in cands]))
        cands = [c for c in cands if c["sharp"] >= med * 0.25] or cands
        for k, c in enumerate(cands, 1):
            c["no"] = k
    return cands


# ---------------- AIで選ぶ ----------------

def pick_with_ai(cands, title: str, text_ideas: list[str], mode: str, effort: str):
    import rc_llm
    anthropic, client = rc_llm._client()
    content = []
    for c in cands:
        im = Image.open(c["path"]).resize((512, 288))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        note = f"／{c['note']}" if c.get("note") else ""   # 顔の様子（rc_thumbspro）
        content.append({"type": "text", "text": f"候補{c['no']}（{c['why']}{note}）"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(buf.getvalue()).decode()}})
    content.append({"type": "text", "text": f"動画のタイトル案: {title}\n文字の案: {'、'.join(text_ideas) or '（なし）'}"})
    system = f"""あなたはYouTubeのサムネイルを作るデザイナーです。動画から取り出した候補画像の中から、サムネイルの背景に向いているものを3枚選び、それぞれに入れる文字を決めてください。
動画のジャンル: {rc_llm.MODE_HINT.get(mode, '')}

選ぶ基準:
- 表情がいい（目をつぶっていない、口が変な形で止まっていない）、または商品・作品・景色がはっきり写っている
- ブレていない、暗すぎない
- 3枚はなるべく違う場面から選ぶ
文字:
- 12字以内。思わず見たくなる短い言葉。2行にするときは \\n で区切る
- 文字の案があれば参考にしてよいが、画像に合わせて変えてもよい
出力はJSONのみです。"""
    usage = {"in": 0, "out": 0}
    res = rc_llm._call(client, anthropic, system, content, PICK_SCHEMA, effort, usage)
    nos = {c["no"] for c in cands}
    picks = [p for p in res.get("picks", []) if p.get("frame") in nos][:3]
    return picks, usage


def pick_simple(cands, text_ideas: list[str], title: str):
    """AIなし：見どころ優先・くっきりした順に、離れた場面を3枚"""
    order = sorted(cands, key=lambda c: (-c["score"],) if "score" in c else (c["why"] == "全体から", -c["sharp"]))
    texts = text_ideas or [title or "タイトル"]
    return [{"frame": c["no"], "text": texts[i % len(texts)], "reason": c["why"]} for i, c in enumerate(order[:3])]


# ---------------- 合成 ----------------

def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _kind(ch: str) -> str:
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "H"   # ひらがな
    if 0x30A0 <= o <= 0x30FF:
        return "K"   # カタカナ
    if 0x4E00 <= o <= 0x9FFF or ch in "々〇":
        return "C"   # 漢字
    return "O"


def _lines(text: str, one_line_max: int = 7) -> list[str]:
    """2行に分ける。AIが \\n を入れていればそれに従い、なければ自然な区切りを探す。"""
    text = text.replace("\\n", "\n").strip()
    if "\n" in text:
        return [s.strip() for s in text.split("\n") if s.strip()][:2]
    if len(text) <= one_line_max:
        return [text]
    mid = len(text) / 2
    best, best_score = len(text) // 2, 1e9
    for k in range(2, len(text) - 1):
        prev, nxt = text[k - 1], text[k]
        if prev in "、。！？!? ・":
            rank = 0          # 句読点・記号の後ろ（一番自然）
        elif _kind(prev) == "H" and _kind(nxt) in "CK":
            rank = 1          # ひらがな → 漢字・カタカナ（「穴あけ／一瞬」）
        elif _kind(prev) != _kind(nxt) and not (_kind(prev) == "C" and _kind(nxt) == "H"):
            rank = 2          # 文字の種類が変わる所（ただし送り仮名の前は避ける）
        else:
            rank = 5
        score = rank * 3 + abs(k - mid)
        if score < best_score:
            best, best_score = k, score
    return [text[:best].strip(), text[best:].strip()]


def _fit(font_path, idx, lines, max_w, max_h, start=170):
    size = start
    while size > 30:
        f = ImageFont.truetype(font_path, size, index=idx)
        widths = [f.getbbox(s, stroke_width=int(size * 0.2))[2] for s in lines]
        if max(widths) <= max_w and size * 1.15 * len(lines) <= max_h:
            return f, size
        size -= 6
    return ImageFont.truetype(font_path, size, index=idx), size


def _draw_text(img, lines, font, size, x, y, align, colors):
    d = ImageDraw.Draw(img)
    fill, inner, outer = colors
    lh = int(size * 1.15)
    for i, s in enumerate(lines):
        w = font.getbbox(s, stroke_width=int(size * 0.2))[2]
        xx = x - w / 2 if align == "center" else x
        yy = y + i * lh
        d.text((xx, yy), s, font=font, fill=outer, stroke_width=int(size * 0.2), stroke_fill=outer)
        d.text((xx, yy), s, font=font, fill=fill, stroke_width=int(size * 0.11), stroke_fill=inner)


def compose(frame: Image.Image, text: str, layout: str, tpl: Template, font: tuple[str, int]) -> Image.Image:
    cfg = tpl.get("thumbnail", {})
    colors = (_hex(cfg.get("text_color", "#FFE14D")), _hex(cfg.get("outline_color", "#111111")),
              _hex(cfg.get("outer_color", "#FFFFFF")))
    accent = _hex(cfg.get("accent_color", "#2B6CB0"))
    img = frame.convert("RGB").resize((W, H))
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Brightness(img).enhance(1.04)
    lines = _lines(text)
    fp, idx = font

    if layout == "band":
        band_h = int(H * 0.30)
        img = img.convert("RGBA")
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        d.rectangle([0, 0, W, band_h], fill=accent + (255,))
        d.polygon([(0, band_h), (W, band_h), (W, band_h + 18), (0, band_h + 30)], fill=accent + (255,))
        img = Image.alpha_composite(img, ov).convert("RGB")
        f, size = _fit(fp, idx, lines, W - 80, band_h - 10, start=180)
        total_h = int(size * 1.15) * len(lines)
        _draw_text(img, lines, f, size, W / 2, (band_h - total_h) / 2 - size * 0.05, "center",
                   ((255, 255, 255), colors[1], colors[1]))
    elif layout == "side":
        # 映像を少し寄せて右に置き、左に暗いグラデーション＋大きな文字
        z = frame.convert("RGB").resize((int(W * 1.15), int(H * 1.15)))
        img2 = Image.new("RGB", (W, H))
        img2.paste(ImageEnhance.Color(z).enhance(1.25), (int(W * 0.12), -int(H * 0.075)))
        img = img2.convert("RGBA")
        grad = Image.new("L", (W, 1))
        for x in range(W):
            grad.putpixel((x, 0), int(235 * max(0.0, 1 - x / (W * 0.62))))
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        shade.putalpha(grad.resize((W, H)))
        img = Image.alpha_composite(img, shade).convert("RGB")
        lines = _lines(text, one_line_max=4)
        f, size = _fit(fp, idx, lines, int(W * 0.6), H - 100, start=200)
        total_h = int(size * 1.15) * len(lines)
        _draw_text(img, lines, f, size, 50, (H - total_h) / 2, "left", colors)
    else:  # bottom
        img = img.convert("RGBA")
        grad = Image.new("L", (1, H))
        for y in range(H):
            grad.putpixel((0, y), int(200 * max(0.0, (y - H * 0.45) / (H * 0.55))))
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        shade.putalpha(grad.resize((W, H)))
        img = Image.alpha_composite(img, shade).convert("RGB")
        f, size = _fit(fp, idx, lines, W - 80, int(H * 0.5))
        total_h = int(size * 1.15) * len(lines)
        _draw_text(img, lines, f, size, W / 2, H - total_h - 40, "center", colors)
    return img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))


def _pro(tpl: Template):
    """顔で候補を選ぶ・人物を切り抜くための道具（rc_thumbspro）。使えない・使わない設定なら None"""
    try:
        import rc_thumbspro
        d = rc_thumbspro.Designer(tpl)
        return d if d.active else None
    except Exception as e:  # noqa: BLE001
        print(f"  [注意] 人物の切り抜き・顔の検出を使えませんでした: {e}")
        return None


def save_jpeg(img: Image.Image, path: Path):
    for q in (92, 85, 75):   # YouTubeの上限（2MB）に収める
        img.save(path, "JPEG", quality=q, optimize=True)
        if path.stat().st_size < 1_900_000:
            return


# ---------------- まとめ ----------------

def generate(out_dir: Path, tpl: Template, llm: str = "api", effort: str = "medium",
             custom_text: str | None = None) -> list[Path]:
    sess = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    chunks = {c["id"]: c for c in json.loads((out_dir / "chunks.json").read_text(encoding="utf-8"))}
    decisions = json.loads((out_dir / "decisions_used.json").read_text(encoding="utf-8")) \
        if (out_dir / "decisions_used.json").exists() else {}
    plan = {}
    for name in ("plan_edited.json", "plan_used.json"):
        if (out_dir / name).exists():
            plan = json.loads((out_dir / name).read_text(encoding="utf-8"))
            break
    meta = sess.get("meta") or {}
    title = (meta.get("titles") or [""])[0]
    ideas = meta.get("thumbnail_texts") or []

    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    import rc_speedup
    tl = rc_speedup.wrap(tl, plan)   # 早送りがあれば、完成動画の時刻で均等に候補を選ぶ
    tdir = out_dir / "thumbnails"
    work = tdir / "_candidates"
    print("■ サムネイル案")
    print("  候補の場面を取り出しています…", flush=True)
    pro = _pro(tpl)   # 顔で選ぶ・人物の切り抜き（rc_thumbspro。使えなければ None）
    cands = extract_candidates(src, candidate_times(sess, tl, chunks, decisions, plan), work,
                               pro.frame_key if pro else None)
    if not cands:
        raise SystemExit("[エラー] 候補の静止画を取り出せませんでした。")
    if pro:
        cands = pro.annotate(cands)

    usage = None
    if llm == "api" and not custom_text:
        print(f"  Claudeが候補{len(cands)}枚から選んでいます…", flush=True)
        try:
            picks, usage = pick_with_ai(cands, title, ideas, sess.get("mode", "talk"), effort)
        except Exception as e:  # noqa: BLE001
            print(f"  [注意] AIでの選択に失敗したので、自動で選びます: {e}")
            picks = []
        if not picks:
            picks = pick_simple(cands, ideas, title)
    else:
        picks = pick_simple(cands, [custom_text] if custom_text else ideas, title)
        if custom_text:
            for p in picks:
                p["text"] = custom_text

    font = find_font(tpl)
    layouts = tpl.get("thumbnail", {}).get("layouts", ["bottom", "band", "side"])
    for old in [*tdir.glob("thumb_*.jpg"), *tdir.glob("cutout_*.png")]:
        old.unlink()
    by_no = {c["no"]: c for c in cands}
    outs, info = [], []
    for i, p in enumerate(picks, 1):
        frame = Image.open(by_no[p["frame"]]["path"])
        # 人物の切り抜きデザイン（thumb_i_person.jpg）。frame は「背景だけぼかす」設定のときに差し替わる
        pouts, frame = pro.make(frame, p["text"], i, font, tdir, by_no[p["frame"]]) if pro else ([], frame)
        for lay in layouts:
            if lay == "person":
                continue
            path = tdir / f"thumb_{i}_{lay}.jpg"
            save_jpeg(compose(frame, p["text"], lay, tpl, font), path)
            outs.append(path)
        outs += pouts
        info.append({"image": i, "text": p["text"], "reason": p.get("reason", ""),
                     "source_time": round(by_no[p["frame"]]["t"], 2)})
    (tdir / "thumbnails.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(outs)}枚のサムネイル案を作りました: {tdir}")
    if usage and usage["in"]:
        import rc_llm
        print(f"  Claude API: 約${rc_llm.cost_usd(usage):.2f}")
    return outs
