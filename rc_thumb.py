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
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

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
                "required": ["frame", "text", "label", "text_position", "subject_box", "reason"],
                "properties": {
                    "frame": {"type": "integer", "description": "候補画像の番号"},
                    "text": {"type": "string", "description": "サムネイルの文字。12字以内。改行は\\n（2行まで）"},
                    "label": {"type": "string", "description": "大きな文字の上に添える見出し（商品名・ブランド名など。10字以内。なければ空）"},
                    "text_position": {"type": "string", "enum": ["top", "bottom", "left", "right", "any"],
                                      "description": "大きな文字を置いても、顔・商品・表示などの大事な所が隠れない側"},
                    "subject_box": {
                        "type": "object", "additionalProperties": False, "required": ["x0", "y0", "x1", "y1"],
                        "description": "文字で隠してはいけない大事な所（顔・商品・ロゴ・表示など）をまとめて囲む枠。"
                                       "画面の左上を0、右下を100とした%",
                        "properties": {k: {"type": "integer", "description": d} for k, d in (
                            ("x0", "左端（%）"), ("y0", "上端（%）"), ("x1", "右端（%）"), ("y1", "下端（%）"))}},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


# ---------------- フォント ----------------

def find_font(tpl: Template) -> tuple[str, int]:
    """サムネイルの文字のフォント: font_file → テンプレートの fonts フォルダ → 見せ方（style）に合う Windows のフォント"""
    cfg = tpl.get("thumbnail", {})
    cands: list[tuple[str, int, str]] = []   # (ファイル, .ttc の中の番号, 太さを変えられるフォントの太さ)
    if cfg.get("font_file"):
        p = Path(cfg["font_file"])
        if not p.is_absolute():
            p = tpl.dir / p
        cands.append((str(p), int(cfg.get("font_index", 0)), str(cfg.get("font_weight", "") or "")))
    if tpl.fonts_dir:
        cands += [(str(p), 0, "") for p in sorted(tpl.fonts_dir.glob("*")) if p.suffix.lower() in (".ttf", ".otf", ".ttc")]
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    user = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"   # 「このユーザーだけ」に入れたフォント
    for name, idx, weight in text_style(tpl)["fonts"]:
        cands += [(str(win / name), idx, weight), (str(user / name), idx, weight)]
    cands += [(str(win / "BIZ-UDGothicB.ttc"), 1, ""), (str(win / "YuGothB.ttc"), 0, ""), (str(win / "meiryob.ttc"), 0, ""),
              ("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 0, ""),
              ("/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf", 0, "")]
    for path, idx, weight in cands:
        if Path(path).exists():
            for i in (idx, 0):
                try:
                    ImageFont.truetype(path, 40, index=i)
                except OSError:
                    continue
                if weight:
                    _VF[path] = weight
                else:
                    _VF.pop(path, None)
                return path, i
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

def pick_with_ai(cands, title: str, text_ideas: list[str], mode: str, effort: str, style: str = ""):
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
- サムネイルの見せ方は「{style or 'ポップ'}」です。言葉の調子もそれに合わせる（上品なら、煽りすぎない落ち着いた言葉）
見出し（label）:
- 商品名・ブランド名・場所など、大きな文字の上に添える言葉。10字以内で短く（短いほど大きく読める。例: 「野原工芸」「紅紫檀 シャーペン」）。大きな文字と同じ内容をくり返さない。なければ空
文字の位置（text_position）:
- 大きな文字（2行・画面の半分くらい）を置いても、顔・商品・はかりの表示など大事な所が隠れない側を top/bottom/left/right から選ぶ。どこでもよければ any
大事な所の枠（subject_box）:
- 文字で隠してはいけない所（顔・商品・ロゴ・はかりの表示など）をまとめて囲む枠。画面の左上を0、右下を100とした%で、少し余裕をもって囲む
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
    return [{"frame": c["no"], "text": texts[i % len(texts)], "label": "", "text_position": "any", "subject_box": None,
             "reason": c["why"]} for i, c in enumerate(order[:3])]


# ---------------- 合成 ----------------

def _hex(c, default=(255, 255, 255)):
    c = str(c or "").strip().lstrip("#")
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


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


def _font(path: str, idx: int, size: int):
    """フォント。太さを変えられるフォント（Noto の VF など）は、見せ方の太さにする"""
    f = ImageFont.truetype(path, size, index=idx)
    w = _VF.get(path)
    if w:
        try:
            f.set_variation_by_name(w)
        except Exception:  # noqa: BLE001  太さの名前が無い・この環境で使えないときは、そのまま
            pass
    return f


def _fit(font_path, idx, lines, max_w, max_h, start=170, pad=0.2):
    """枠（max_w × max_h）に収まる、いちばん大きい文字。pad: ふち・影の分の余白（文字の大きさに対する割合）"""
    size = start
    while size > 30:
        f = _font(font_path, idx, size)
        widths = [f.getbbox(s)[2] + 2 * int(size * pad) for s in lines]
        if max(widths) <= max_w and size * 1.15 * len(lines) <= max_h:
            return f, size
        size -= 6
    return _font(font_path, idx, size), size


def _line_pos(lines, font, size, x, y, align):
    """各行の左上。align: left=x が左端 / center=x が中央 / right=x が右端"""
    lh = int(size * 1.15)
    out = []
    for i, s in enumerate(lines):
        w = font.getbbox(s)[2]
        xx = x - w / 2 if align == "center" else (x - w if align == "right" else x)
        out.append((xx, y + i * lh, s))
    return out


def _mask(pos, font, stroke: int = 0) -> Image.Image:
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    for xx, yy, s in pos:
        d.text((xx, yy), s, font=font, fill=255, stroke_width=stroke, stroke_fill=255)
    return m


def _fill_holes(m: Image.Image) -> Image.Image:
    """文字のふちの内側にできる穴（画数の多い漢字のすき間）を埋めたもの"""
    hard = np.array(m) >= 96
    try:
        import cv2
        flood = np.pad(hard, 1).astype(np.uint8)
        cv2.floodFill(flood, None, (0, 0), 2)
        outside = flood[1:-1, 1:-1] == 2
    except ImportError:   # OpenCV が無いときは Pillow だけで（文字のまわりだけを調べるので、少し遅いだけ）
        box = m.getbbox()
        outside = np.ones_like(hard)
        if box:
            x0, y0, x1, y1 = max(box[0] - 2, 0), max(box[1] - 2, 0), min(box[2] + 2, W), min(box[3] + 2, H)
            crop = Image.fromarray(np.where(hard[y0:y1, x0:x1], 255, 0).astype(np.uint8))
            crop = ImageOps.expand(crop, 1, 0)
            ImageDraw.floodfill(crop, (0, 0), 128)
            outside[y0:y1, x0:x1] = np.array(crop)[1:-1, 1:-1] == 128
    holes = (~outside) & (~hard)
    return ImageChops.lighter(m, Image.fromarray((holes * 255).astype(np.uint8)))


def _pockets(white: Image.Image, near_out: Image.Image) -> Image.Image:
    """外側の白いふちのうち、外の縁につながらず、内側のふちに囲まれた所（2行の間などにできる白いかたまり）"""
    try:
        import cv2
    except ImportError:   # OpenCV が無いときは調べない（細い所だけは _draw_outline で直している）
        return Image.new("L", (W, H), 0)
    wm = (np.array(white) > 64).astype(np.uint8)
    n, lab = cv2.connectedComponents(wm, connectivity=8)
    keep = np.unique(lab[(np.array(near_out) > 0) & (wm > 0)])
    pockets = (wm > 0) & ~np.isin(lab, keep)
    return Image.fromarray((pockets * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))


def _paint(img: Image.Image, color, mask: Image.Image, alpha: float = 1.0):
    if alpha < 1:
        mask = mask.point(lambda v: int(v * alpha))
    img.paste(tuple(color), (0, 0), mask)


def _draw_outline(img, pos, font, size, fill, inner, outer):
    """太い文字に二重のふち（内側 inner・外側 outer）。外側の白いふちが漢字のすき間にはみ出して白い点に見えないよう、
    内側のふちの中の穴はふちの色で埋める"""
    ri, ro = max(2, int(size * 0.11)), max(3, int(size * 0.2))
    m = _mask(pos, font)
    s = Image.new("L", (W, H), 0)
    k = max(3, int(size * 0.05)) | 1
    for p in pos:   # 行ごとに埋める（2行の間のすき間まで穴として埋めないように）
        # 文字と文字の間の細いすき間を先にふさぐ（ふさがないと、外側の白いふちが小さな白い欠けとして見える）
        line = _mask([p], font, ri).filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))
        s = ImageChops.lighter(s, _fill_holes(line))
    if outer:
        o = _mask(pos, font, ro)
        # 外側の白いふちに囲まれて、写真がのぞく小さな穴（2行の間など）は、内側のふちの色で埋める
        of = _fill_holes(o)
        s = ImageChops.lighter(s, ImageChops.subtract(of, o).point(lambda v: 255 if v > 0 else 0)
                               .filter(ImageFilter.MaxFilter(3)))
        # 外側の白いふちのうち、文字と文字の間の細いすき間にのぞく所（外の縁につながらない細い部分）は、内側のふちの色にする
        white = ImageChops.subtract(o, s)
        kk = max(3, int(size * 0.06)) | 1
        thin = ImageChops.subtract(white, white.filter(ImageFilter.MinFilter(kk)).filter(ImageFilter.MaxFilter(kk)))
        near_out = of.point(lambda v: 255 if v < 32 else 0).filter(ImageFilter.MaxFilter(7))   # 本当の外（画面の端につながる所）
        thin = ImageChops.subtract(thin.point(lambda v: 255 if v > 64 else 0), near_out)
        s = ImageChops.lighter(s, thin.filter(ImageFilter.MaxFilter(3)))
        s = ImageChops.lighter(s, _pockets(ImageChops.subtract(o, s), near_out))
        kc = max(5, int(size * 0.06)) | 1   # 内側のふちの中の、ごく小さなすき間もふさぐ
        s = s.filter(ImageFilter.MaxFilter(kc)).filter(ImageFilter.MinFilter(kc))
        sh = o.filter(ImageFilter.GaussianBlur(max(2, size * 0.05)))   # 外側のふちの下に、うすい影
        _paint(img, (0, 0, 0), ImageChops.offset(sh, int(size * 0.03), int(size * 0.05)), 0.45)
        _paint(img, outer, o)
    _paint(img, inner, s)
    _paint(img, fill, m)


def _draw_shadow(img, pos, font, size, fill, inner):
    """ふちを付けず、柔らかい影で読ませる（上品）。明るい所でも読めるよう、ごく細い縁も付ける"""
    m = _mask(pos, font)
    sh = _mask(pos, font, max(1, int(size * 0.04))).filter(ImageFilter.GaussianBlur(max(3, size * 0.12)))
    _paint(img, (0, 0, 0), ImageChops.offset(sh, int(size * 0.03), int(size * 0.06)), 0.85)
    _paint(img, inner, _mask(pos, font, max(1, int(size * 0.025))), 0.55)
    _paint(img, fill, m)


def _draw_text(img, lines, font, size, x, y, align, colors):
    """今までの見せ方（太い文字に二重のふち）。colors = (文字, 内側のふち, 外側のふち)"""
    _draw_outline(img, _line_pos(lines, font, size, x, y, align), font, size, *colors)


def _draw_styled(img, lines, font, size, x, y, align, st: dict):
    """見せ方（st = text_style()）に合わせて文字を描く。panel（シンプル）は文字の後ろに半透明の板を敷く"""
    pos = _line_pos(lines, font, size, x, y, align)
    fill, inner, outer = _hex(st["text_color"]), _hex(st["outline_color"], (0, 0, 0)), st.get("outer_color")
    if st["effect"] == "outline":
        _draw_outline(img, pos, font, size, fill, inner, _hex(outer) if outer else None)
        return
    if st["effect"] == "panel":
        box = _mask(pos, font).getbbox()
        if box:
            px, py = int(size * 0.35), int(size * 0.22)
            panel = Image.new("L", (W, H), 0)
            ImageDraw.Draw(panel).rounded_rectangle([box[0] - px, box[1] - py, box[2] + px, box[3] + py],
                                                    radius=int(size * 0.18), fill=255)
            _paint(img, inner, panel, float(st.get("panel_alpha", 0.6)))
            ac = st.get("_accent")
            if ac:   # 板の左に、写真から取った色の細い線
                bar = Image.new("L", (W, H), 0)
                ImageDraw.Draw(bar).rectangle([box[0] - px, box[1] - py, box[0] - px + max(6, size // 14),
                                               box[3] + py], fill=255)
                _paint(img, ac, bar)
    _draw_shadow(img, pos, font, size, fill, inner)


# ---------------- 見せ方（style） ----------------
_VF: dict[str, str] = {}   # 太さを変えられるフォントのファイル → 使う太さ（find_font で決める）

# 見せ方ごとの初期値。テンプレートの [thumbnail] に値があれば、そちらを使う（空なら見せ方の初期値）
STYLES = {
    "elegant": {   # 上品: 明朝体の白い文字に柔らかい影。暗いグラデーションの上に置き、細い金の線と小さな見出しを添える
        "label": "上品（明朝体の白い文字に影）", "effect": "shadow", "align": "left",
        "fonts": [("NotoSerifJP-VF.ttf", 0, "Bold"), ("yumindb.ttf", 0, ""), ("HGRME.TTC", 0, ""),
                  ("BIZ-UDMinchoM.ttc", 0, ""), ("msmincho.ttc", 0, "")],
        "text_color": "#FFFFFF", "outline_color": "#000000", "outer_color": "", "accent_color": "#D9B76E",
        "contrast": 1.05, "saturation": 1.0, "vignette": 0.45, "shade": 215,
    },
    "pop": {       # ポップ: 太い文字に二重のふち（今までの見せ方を整えたもの）
        "label": "ポップ（太い文字に二重のふち）", "effect": "outline", "align": "center",
        "fonts": [("NotoSansJP-VF.ttf", 0, "Black"), ("HGRSGU.TTC", 0, ""), ("YuGothB.ttc", 0, ""),
                  ("meiryob.ttc", 0, ""), ("BIZ-UDGothicB.ttc", 1, "")],
        "text_color": "#FFE14D", "outline_color": "#111111", "outer_color": "#FFFFFF", "accent_color": "auto",
        "contrast": 1.08, "saturation": 1.1, "vignette": 0.15, "shade": 150,
    },
    "simple": {    # シンプル: 半透明の板の上に白い文字
        "label": "シンプル（半透明の板に白い文字）", "effect": "panel", "align": "left",
        "fonts": [("NotoSansJP-VF.ttf", 0, "Bold"), ("YuGothB.ttc", 0, ""), ("meiryob.ttc", 0, ""),
                  ("BIZ-UDGothicB.ttc", 1, "")],
        "text_color": "#FFFFFF", "outline_color": "#101418", "outer_color": "", "accent_color": "auto",
        "contrast": 1.05, "saturation": 1.04, "vignette": 0.2, "shade": 0, "panel_alpha": 0.62,
    },
}
DEFAULT_STYLE = "pop"   # [thumbnail] に style が無い（前の版の）テンプレートは、今までの見せ方


def text_style(tpl) -> dict:
    """テンプレートの [thumbnail] の見せ方。style の初期値に、テンプレートで決めた色などを重ねる"""
    cfg = tpl.get("thumbnail", {}) or {}
    name = str(cfg.get("style") or DEFAULT_STYLE).strip()
    name = name if name in STYLES else DEFAULT_STYLE
    st = {**STYLES[name], "name": name}
    for k in ("text_color", "outline_color", "outer_color", "accent_color", "align"):
        v = str(cfg.get(k) or "").strip()
        if v:
            st[k] = v
    for k in ("contrast", "saturation", "vignette", "panel_alpha"):
        if cfg.get(k) not in (None, ""):
            st[k] = float(cfg[k])
    if str(cfg.get("outer_color", "x")).strip().lower() == "none":
        st["outer_color"] = ""
    st["show_label"] = bool(cfg.get("label", True))
    return st


# ---------------- 合成 ----------------
def _grade(img: Image.Image, st: dict) -> Image.Image:
    """写真の補正は控えめに。暗い写真だけ少し明るくし、まわりを少し暗くして（ビネット）目を真ん中に集める"""
    img = ImageEnhance.Contrast(img).enhance(st.get("contrast", 1.05))
    img = ImageEnhance.Color(img).enhance(st.get("saturation", 1.0))
    mean = float(np.array(img.convert("L").resize((64, 36))).mean())
    if mean < 100:
        img = ImageEnhance.Brightness(img).enhance(min(1.25, 112 / max(mean, 1)))
    v = float(st.get("vignette", 0))
    if v > 0:
        yy, xx = np.mgrid[0:H // 8, 0:W // 8].astype(np.float32)
        r = np.sqrt(((xx - W / 16) / (W / 16)) ** 2 + ((yy - H / 16) / (H / 16)) ** 2) / np.sqrt(2)
        dark = Image.fromarray((np.clip((r - 0.45) / 0.55, 0, 1) ** 1.5 * 255 * v).astype(np.uint8)).resize((W, H))
        _paint(img, (0, 0, 0), dark)
    return img


def _detail(img: Image.Image) -> np.ndarray:
    """写真の細かさ（輪郭の多さ）の地図（160×90・0〜1）。文字は細かさの少ない所に置く"""
    g = img.convert("L").resize((160, 90)).filter(ImageFilter.FIND_EDGES).filter(ImageFilter.BoxBlur(2))
    a = np.array(g, np.float32)
    return np.clip(a / max(float(np.percentile(a, 95)), 1.0), 0, 1)


def _busy(det: np.ndarray, box) -> float:
    x0, y0, x1, y1 = [int(round(v / 8)) for v in box]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(160, max(x1, x0 + 1)), min(90, max(y1, y0 + 1))
    return float(det[y0:y1, x0:x1].mean())


def _accent(img: Image.Image, st: dict):
    """帯・線の色。accent_color が auto なら写真から取る（いちばん目立つ色を、見せ方に合う明るさにする）"""
    import colorsys
    a = str(st.get("accent_color", "auto")).strip().lower()
    if a and a != "auto":
        return _hex(a, (43, 108, 176))
    q = img.resize((64, 36)).convert("RGB").quantize(8)
    pal, best, score = q.getpalette(), None, 0.0
    for count, i in q.getcolors():
        r, g, b = (v / 255 for v in pal[i * 3:i * 3 + 3])
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        sc = s * min(v, 0.85) * count ** 0.5
        if s > 0.18 and sc > score:
            best, score = (h, s), sc
    if not best:
        return (31, 42, 68) if st["name"] != "pop" else (43, 108, 176)
    h, s = best
    s, v = (max(s, 0.7), 0.62) if st["name"] == "pop" else (min(max(s, 0.35), 0.6), 0.3)
    return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def _shade(img: Image.Image, side: str, reach: float, strength: int):
    """文字を置く側（side）から、暗いグラデーションをかける。reach: 画面のどこまで（0〜1）"""
    if strength <= 0:
        return
    n = W if side in ("left", "right") else H
    ramp = np.clip(1 - np.arange(n) / (n * reach), 0, 1) ** 1.4 * strength
    if side in ("right", "bottom"):
        ramp = ramp[::-1]
    g = Image.fromarray(ramp.astype(np.uint8)[None, :] if side in ("left", "right") else ramp.astype(np.uint8)[:, None])
    _paint(img, (0, 0, 0), g.resize((W, H)))


def _pad(st: dict) -> float:
    """文字の大きさを決めるときの、ふち・影・板の分の余白（文字の大きさに対する割合）"""
    return {"outline": 0.2, "panel": 0.45}.get(st["effect"], 0.08)


LABEL_MIN = 54     # 見出しの大きさの下限（高さ720に対して約7.5%。スマホの一覧の小さな表示でも読める）
LABEL_FLOOR = 42   # 長い見出しを幅に収めるために縮めるときの下限。これでも入らなければ末尾を「…」にする


def _label_fit(label, fp, idx, size, max_w, st):
    """見出し（商品名など）の (フォント, 文字)。付けないときは None。

    大きさは大きな文字の 0.42 倍で、LABEL_MIN 以上（ただし大きな文字の 0.7 倍まで）。
    幅（max_w）に収まらなければ LABEL_FLOOR まで縮め、それでも入らなければ末尾を「…」にする"""
    label = (label or "").strip()
    if not label or not st.get("show_label", True):
        return None
    chip = 0.9 if st["effect"] == "outline" else 0.0   # ポップの札の左右の余白（文字の大きさに対する割合）
    ls = max(LABEL_FLOOR, min(max(LABEL_MIN, int(size * 0.42)), int(size * 0.7)))
    f = _font(fp, idx, ls)
    w = f.getbbox(label)[2] + chip * ls
    if w > max_w:
        ls = max(LABEL_FLOOR, int(ls * max_w / w))
        f = _font(fp, idx, ls)
        if f.getbbox(label)[2] + chip * ls > max_w:
            while len(label) > 1 and f.getbbox(label + "…")[2] + chip * ls > max_w:
                label = label[:-1]
            label = label.rstrip() + "…"
    return f, label


def _label_h(lab, st) -> int:
    """見出しの行の高さ（下の大きな文字との間を含む）"""
    if not lab:
        return 0
    return int(lab[0].size * (1.75 if st["effect"] == "outline" else 1.55))


def _draw_label(img, lab, x, y, align, st, accent, avoid=None):
    """大きな文字の上に添える見出し（lab は _label_fit の結果）。描いた高さを返す。
    avoid: 大事な所の枠。上品の飾りの線は、ここに重なるなら引かない（ロゴなどに線が刺さらない）"""
    if not lab:
        return 0
    f, label = lab
    w = f.getbbox(label)[2]
    lx = x - w / 2 if align == "center" else (x - w if align == "right" else x)
    lsize = f.size
    if st["effect"] == "outline":   # ポップ: 写真から取った色の札に白い文字
        bx = [lx - lsize * 0.45, y, lx + w + lsize * 0.45, y + lsize * 1.45]
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle(bx, radius=int(lsize * 0.25), fill=255)
        _paint(img, accent, m)
        ImageDraw.Draw(img).text((lx, y + lsize * 0.18), label, font=f, fill=(255, 255, 255))
        return _label_h(lab, st)
    color = accent if st["effect"] == "shadow" else _hex(st["text_color"])
    pos = [(lx, y, label)]
    # 影は文字の大きさに合わせて濃く広く（明るい写真の上でも見出しが沈まない）
    sh = _mask(pos, f, max(2, lsize // 18)).filter(ImageFilter.GaussianBlur(max(3, lsize * 0.08)))
    _paint(img, (0, 0, 0), sh, 0.9)
    _paint(img, color, _mask(pos, f))
    if st["effect"] == "shadow":   # 上品: 見出しの横に細い線（中央寄せなら両側）
        ly = int(y + lsize * 0.62)
        line = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(line)
        gap, length = lsize * 0.5, lsize * 2.5
        sides = {"center": ("left", "right"), "right": ("left",)}.get(align, ("right",))
        for s in sides:
            x0, x1 = (lx + w + gap, lx + w + gap + length) if s == "right" else (lx - gap - length, lx - gap)
            x0, x1 = max(x0, 16), min(x1, W - 16)
            if x1 - x0 >= lsize and not _hide(avoid, (x0, ly - 8, x1, ly + 8)):
                d.rectangle([x0, ly, x1, ly + max(2, lsize // 14)], fill=255)
        _paint(img, accent, line, 0.9)
    return _label_h(lab, st)


def _block(lines, f, size, lab, st):
    """文字のまとまりの幅と高さ（見出しを含む。lab は _label_fit の結果）"""
    w = max(f.getbbox(s)[2] for s in lines)
    h = int(size * 1.15) * len(lines)
    if lab:
        chip = 0.9 * lab[0].size if st["effect"] == "outline" else 0
        w = max(w, lab[0].getbbox(lab[1])[2] + chip)
        h += _label_h(lab, st)
    return w, h


def subject_px(box) -> tuple[float, float, float, float] | None:
    """AIが答えた大事な所の枠（画面に対する%）を、1280×720 の座標にする。おかしい・無いときは None"""
    if not isinstance(box, dict):
        return None
    try:
        x0, y0, x1, y1 = (min(max(float(box[k]), 0.0), 100.0) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 < 2 or y1 - y0 < 2 or (x1 - x0) * (y1 - y0) > 95 * 95:   # 小さすぎる・ほぼ全部は、使わない
        return None
    return x0 * W / 100, y0 * H / 100, x1 * W / 100, y1 * H / 100


def _hide(subject, box) -> float:
    """文字の枠（box）が、大事な所（subject）をどれだけ隠すか（隠す割合 × 重み）。大事な所が無ければ 0"""
    if not subject:
        return 0.0
    ix = max(0.0, min(box[2], subject[2]) - max(box[0], subject[0]))
    iy = max(0.0, min(box[3], subject[3]) - max(box[1], subject[1]))
    area = max((subject[2] - subject[0]) * (subject[3] - subject[1]), 1.0)
    return 1.5 * ix * iy / area


def _moved(subject, x0: float, y0: float, cw: float, ch: float):
    """写真を (x0, y0, cw, ch) で切り出して 1280×720 に広げたとき、大事な所の枠がどこに来るか"""
    if not subject:
        return None
    sx, sy = W / cw, H / ch
    return ((subject[0] - x0) * sx, (subject[1] - y0) * sy, (subject[2] - x0) * sx, (subject[3] - y0) * sy)


def compose(frame: Image.Image, text: str, layout: str, tpl: Template, font: tuple[str, int],
            label: str = "", position: str = "auto", subject=None) -> Image.Image:
    """1枚のサムネイル（1280×720）。

    layout: bottom=大きな文字（上か下）/ band=帯（上か下）/ side=横に文字（左か右・上中下）
    position: 文字を置く側の希望（AIが選んだ top/bottom/left/right。auto なら写真の空いている側）
    label: 大きな文字に添える小さな見出し（商品名など。空なら付けない）
    subject: 文字で隠してはいけない大事な所の枠（1280×720 の座標。subject_px で作る）。いちばん隠さない置き方にする
    """
    st = text_style(tpl)
    fp, idx = font
    img = frame.convert("RGB").resize((W, H), Image.LANCZOS)
    margin = 64
    pad = _pad(st)

    if layout == "side":
        # 文字を置く側（左右）と高さ（上・中・下）を決め、写真を反対側に少し寄せる（左右の端に黒い帯を作らない）。
        # 大事な所を隠さない置き方を優先し、次に写真の細かさの少ない所
        lines = _lines(text, one_line_max=4)
        room = H - 160 - (int(LABEL_MIN * 1.75) if label and st.get("show_label", True) else 0)
        f, size = _fit(fp, idx, lines, int(W * 0.5), room, start=200, pad=pad)
        lab = _label_fit(label, fp, idx, size, int(W * 0.6), st)
        bw, bh = _block(lines, f, size, lab, st)
        cw, ch = int(W * 0.86), int(H * 0.86)
        cands = []
        for side in ("left", "right"):
            x0 = W - cw if side == "left" else 0
            det = _detail(img.crop((x0, 0, x0 + cw, ch)).resize((W, H)))
            subj = _moved(subject, x0, 0, cw, ch)
            tx = margin if side == "left" else W - margin - bw
            for v in ("center", "top", "bottom"):
                ty = (H - bh) / 2 if v == "center" else (56.0 if v == "top" else H - bh - 56.0)
                box = (tx, ty, tx + bw, ty + bh)
                sc = _busy(det, box) + _hide(subj, box) + 0.03 * (v != "center")
                if position == side:
                    sc *= 0.7
                cands.append((sc, side, v, ty))
        _, side, v, y = min(cands)
        subj = _moved(subject, W - cw if side == "left" else 0, 0, cw, ch)
        x0 = W - cw if side == "left" else 0
        img = img.crop((x0, 0, x0 + cw, ch)).resize((W, H), Image.LANCZOS)
        img = _grade(img, st)
        accent = _accent(img, st)
        _shade(img, side, 0.7, max(st.get("shade", 150), 120))
        x, align = (margin, "left") if side == "left" else (W - margin, "right")
        st = {**st, "_accent": accent}
        y += _draw_label(img, lab, x, y, align, st, accent, subj)
        _draw_styled(img, lines, f, size, x, y, align, st)

    elif layout == "band":
        # 帯を、写真の細かい所を隠しにくい側（上か下）に置く。帯の色は写真から取る（上品・シンプルは深い色）
        img = _grade(img, st)
        det = _detail(img)
        # 見出しは帯の中で読める大きさ（LABEL_MIN）にし、その分だけ帯を少し太くする
        lab = _label_fit(label, fp, idx, int(LABEL_MIN / 0.42) + 1, W - 2 * margin, st)
        band_h = int(H * (0.38 if lab else 0.34))
        score = {"top": _busy(det, (0, 0, W, band_h)) + _hide(subject, (0, 0, W, band_h)),
                 "bottom": _busy(det, (0, H - band_h, W, H)) + _hide(subject, (0, H - band_h, W, H))}
        if position in score:
            score[position] *= 0.7
        side = min(score, key=score.get)
        y0 = 0 if side == "top" else H - band_h
        pop = st["effect"] == "outline"
        color = _accent(img, st if pop else {**st, "accent_color": "auto"})
        band = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(band)
        if pop:   # ポップ: 斜めの端の帯
            if side == "top":
                d.polygon([(0, 0), (W, 0), (W, band_h + 10), (0, band_h + 26)], fill=255)
            else:
                d.polygon([(0, y0 - 26), (W, y0 - 10), (W, H), (0, H)], fill=255)
            _paint(img, color, band)
        else:
            d.rectangle([0, y0, W, y0 + band_h], fill=255)
            _paint(img, color, band, 0.9)
        gold = _accent(img, st) if st["effect"] == "shadow" else None
        if gold:   # 上品: 帯の内側の端に細い金の線
            ly = y0 + band_h - 4 if side == "top" else y0
            line = Image.new("L", (W, H), 0)
            ImageDraw.Draw(line).rectangle([0, ly, W, ly + 3], fill=255)
            _paint(img, gold, line)
        room = band_h - 36 - _label_h(lab, st)
        # 1行に収まるなら1行で大きく。収まらなければ2行
        one = [_lines(text)[0] + (_lines(text)[1] if len(_lines(text)) > 1 else "")]
        f, size = _fit(fp, idx, one, W - 2 * margin, room, start=150, pad=pad)
        lines = one
        if size < 84:
            lines = _lines(text)
            f, size = _fit(fp, idx, lines, W - 2 * margin, room, start=150, pad=pad)
        bw, bh = _block(lines, f, size, lab, st)
        y = y0 + (band_h - bh) / 2 - size * 0.04
        if pop:   # ポップ: 白い文字に濃いふち1本（帯の上では外側の白いふちは付けない）。見出しは濃い色の札
            bst = {**st, "text_color": "#FFFFFF", "outer_color": "", "_accent": None}
            tag = (17, 17, 17)
        else:     # 上品・シンプル: 帯の上では板は使わず、影で読ませる。見出しは金（上品）か白
            bst = {**st, "effect": "shadow", "_accent": None}
            tag = gold or _hex(st["text_color"])
        y += _draw_label(img, lab, W / 2, y, "center", bst, tag)
        _draw_styled(img, lines, f, size, W / 2, y, "center", bst)

    else:  # bottom: 大きな文字を、写真の空いている側（上か下）に
        img = _grade(img, st)
        accent = _accent(img, st)
        det = _detail(img)
        lines = _lines(text)
        f, size = _fit(fp, idx, lines, W - 2 * margin, int(H * 0.5), pad=pad)
        lab = _label_fit(label, fp, idx, size, W - 2 * margin, st)
        bw, bh = _block(lines, f, size, lab, st)
        align = st.get("align", "center")
        cands = []
        for v in ("bottom", "top"):
            y = H - bh - 48 if v == "bottom" else 44
            for a in (("center", "left", "right") if align == "center" else ("left", "right")):
                x0 = (W - bw) / 2 if a == "center" else (margin if a == "left" else W - margin - bw)
                # 中央寄せの見せ方は、中央を少しひいきする（左右に寄せるのは、中央が写真の大事な所と重なるとき）
                bias = 0.04 * (v == "top") + 0.02 * (a == "right") + 0.02 * (align == "center" and a != "center")
                sc = _busy(det, (x0, y, x0 + bw, y + bh)) + _hide(subject, (x0, y, x0 + bw, y + bh)) + bias
                if position == v:
                    sc *= 0.7
                cands.append((sc, v, a, y))
        _, v, a, y = min(cands)
        _shade(img, v, 0.62, st.get("shade", 150))
        x = W / 2 if a == "center" else (margin if a == "left" else W - margin)
        st = {**st, "_accent": accent}
        y += _draw_label(img, lab, x, y, a, st, accent, subject)
        _draw_styled(img, lines, f, size, x, y, a, st)
    return img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=2))


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
            picks, usage = pick_with_ai(cands, title, ideas, sess.get("mode", "talk"), effort,
                                        text_style(tpl)["label"])
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
            save_jpeg(compose(frame, p["text"], lay, tpl, font, p.get("label", ""), p.get("text_position", "auto"),
                              subject_px(p.get("subject_box"))), path)
            outs.append(path)
        outs += pouts
        info.append({"image": i, "text": p["text"], "label": p.get("label", ""),
                     "text_position": p.get("text_position", "any"), "subject_box": p.get("subject_box"),
                     "reason": p.get("reason", ""),
                     "source_time": round(by_no[p["frame"]]["t"], 2)})
    (tdir / "thumbnails.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(outs)}枚のサムネイル案を作りました: {tdir}")
    if usage and usage["in"]:
        import rc_llm
        print(f"  Claude API: {rc_llm.cost_text(usage)}")
    return outs
