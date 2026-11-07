"""サムネイルの凝った表現（rc_thumb.compose から使う）。

- 強調する言葉（emphasis）: 文字の一部（数字・素材名など）を大きく、見せ方の強調色にする（並べ方は rc_thumb._text_pos）
- 縦書き（layout = "vertical"）: 右の列から縦に。「ー」や括弧は向きを変え、句読点・小さい字は右上に寄せ、
  2〜3字の英数字（30g など）は横に並べて1字分に収める
- 主役の切り抜き: AIが囲んだ「大事な所の枠」をもとに、OpenCV の GrabCut で切り抜く（追加のダウンロードは不要）。
  主役とまわりの色の差が小さいなど、うまく切れないときは使わない
- 効果（effect）: focus=主役以外を暗く・ぼかす / sticker=主役に白いふちと影（上品はやわらかい光） /
  depth=文字を主役の後ろに回す（主役が文字の手前に来る）
- バッジ（badge）: 上品=朱色の落款（はんこ）風 / ポップ=ギザギザの丸いシール / シンプル=角丸の札
"""
from __future__ import annotations

import hashlib
import math
import re

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

import rc_thumb as T

W, H = T.W, T.H
LAYOUTS = ("bottom", "band", "side", "vertical")   # AIが選べるデザイン
EFFECTS = {"none": "", "focus": "主役を際立たせる", "sticker": "主役に白いふち", "depth": "文字を主役の後ろに"}
EMPH_MAX = 8        # 強調する言葉の長さの上限
BADGE_MAX = 6       # バッジの言葉の長さの上限
MIN_CONTRAST = 20   # 切り抜き: 主役とまわりの色の差（Lab）がこれより小さければ、切り抜きは当てにしない
MIN_EDGE = 0.65     # 切り抜き: 縁のうち、写真の輪郭に沿っている割合の下限（低いのは、紙の一部などを主役と間違えている）
MIN_AGREE = 0.85    # 切り抜き: 2つのやり方の切り抜きが重なる割合（IoU）の下限
DEPTH_MAX_COVER = 0.35   # 文字を主役の後ろに回すのは、文字の枠の中で主役が占める割合がこれ以下のとき（読めなくならない）
LABEL_MAX_COVER = 0.02   # 見出し（小さい字）は少し隠れるだけで読めなくなるので、ほぼ隠れないときだけ


# ================================================================ デザインの値を整える
def clean_design(p, text: str, label: str = "") -> dict:
    """AIが答えたデザインを、使える値にする（おかしい値は使わない）"""
    p = p if isinstance(p, dict) else {}
    flat = str(text or "").replace("\\n", "").replace("\n", "")
    emph = str(p.get("emphasis") or "").strip()
    if not emph or len(emph) > EMPH_MAX or emph == flat or emph not in flat:
        emph = ""
    badge = re.sub(r"\s+", "", str(p.get("badge") or ""))
    if len(badge) > BADGE_MAX or badge in (flat, re.sub(r"\s+", "", label or "")):
        badge = ""
    effect = str(p.get("effect") or "none")
    return {"emphasis": emph, "badge": badge, "effect": effect if effect in EFFECTS else "none"}


_NUM = re.compile(r"[0-9０-９]+(?:[.,．，][0-9０-９]+)?\s*(?:kg|mm|cm|km|mAh|GB|TB|g|m|W|V|%|％|万円|円|万|倍|年|日|時間|分|秒|"
                  r"個|本|枚|台|回|人|歳|インチ)?")


def auto_emphasis(text: str) -> str:
    """AIを使わないとき: 数字（単位つき。「30g」「1.2kg」など）があれば、それを強調する"""
    flat = str(text or "").replace("\\n", "").replace("\n", "")
    m = _NUM.search(flat)
    e = m.group(0).strip() if m else ""
    return e if e and e != flat else ""


def pick_layouts(want, default: list[str], n: int = 3) -> list[str]:
    """AIが選んだデザインの組み合わせ（足りない分はテンプレートの順で補う）"""
    out = []
    for lay in list(want or []) + [x for x in default if x in LAYOUTS] + list(LAYOUTS):
        if lay in LAYOUTS and lay not in out:
            out.append(lay)
    return out[:n]


def describe(d: dict) -> str:
    """画面に出す短い説明（例: 強調「30g」・バッジ「限定」・主役を際立たせる）"""
    parts = []
    if d.get("emphasis"):
        parts.append(f"強調「{d['emphasis']}」")
    if d.get("badge"):
        parts.append(f"バッジ「{d['badge']}」")
    if EFFECTS.get(d.get("effect") or "none"):
        parts.append(EFFECTS[d["effect"]])
    return "・".join(parts)


# ================================================================ 縦書き
_ROT = set("ー―‐－-—〜～…‥=＝()（）「」『』[]［］【】〔〕<>＜＞→←")   # 縦書きでは向きを変える字
_SMALL = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")   # 小さい字（少し右上に寄せる）
_PUNCT = set("、。，．")                                       # 句読点（右上に寄せる）
_ASCII = re.compile(r"[A-Za-z0-9.%+#&!?]+")


def _units(s: str) -> list[tuple[str, str]]:
    """1列の文字を、字の単位に分ける。種類: up=そのまま / rot=横に倒す / tcy=横に並べて1字分 / small / punct / space"""
    out, i = [], 0
    while i < len(s):
        m = _ASCII.match(s, i)
        if m:
            t = m.group(0)
            out.append((t, "tcy" if len(t) <= 3 else "rot"))
            i = m.end()
            continue
        ch = s[i]
        out.append((ch, "space" if ch in " 　" else "rot" if ch in _ROT else "small" if ch in _SMALL
                    else "punct" if ch in _PUNCT else "up"))
        i += 1
    return out


def vpos(cols, font, size, x_right, y_top, emphasis: str = "", track: float = 1.04, pitch: float = 1.22):
    """縦書きの字の置き場所（rc_thumb.G のリスト）と、全体の (幅, 高さ)。cols は右の列から順"""
    runs = T._emph_runs(cols, emphasis)
    ef = T._font(font.path, font.index, int(size * T.EMPH_SCALE)) if any(e for r in runs for _, e in r) else font
    out, x, width, height = [], float(x_right), 0.0, 0.0
    for li, run in enumerate(runs):
        units = [(t, k, e) for s, e in run for t, k in _units(s)]
        fs = max([(ef if e else font).size for _, _, e in units] or [size])
        cx, y = x - fs / 2, float(y_top)
        for t, k, e in units:
            f = ef if e else font
            z = f.size
            if k == "space":
                y += z * 0.5
                continue
            if k == "rot":
                h = f.getlength(t) + z * 0.08
                out.append(T.G(cx, y + h / 2, t, f, li, e, "mm", 90))
            elif k == "tcy":   # 英数字（2〜3字）は横に並べて1字分に収める
                h = z * track
                w = f.getlength(t)
                f2 = f if w <= z * 0.92 else T._font(f.path, f.index, max(12, int(z * z * 0.92 / w)))
                out.append(T.G(cx, y + h / 2, t, f2, li, e, "mm"))
            else:
                h = z * track
                dx, dy = {"small": (0.1, -0.1), "punct": (0.58, -0.58)}.get(k, (0.0, 0.0))
                out.append(T.G(cx + dx * z, y + h / 2 + dy * z, t, f, li, e, "mm"))
            y += h
        height = max(height, y - y_top)
        width = (x_right - x) + fs
        x -= fs * pitch
    return out, (width, height)


def vfit(fp, idx, cols, max_w, max_h, emphasis: str = "", start: int = 170, pad: float = 0.08):
    """縦書きで枠（max_w × max_h）に収まる、いちばん大きい文字"""
    size = start
    while size > 30:
        f = T._font(fp, idx, size)
        w, h = vpos(cols, f, size, 0, 0, emphasis)[1]
        if w + 2 * size * pad <= max_w and h + 2 * size * pad <= max_h:
            return f, size
        size -= 6
    return T._font(fp, idx, size), size


def vlabel_fit(label, fp, idx, st, max_h):
    """縦書きの見出し (フォント, 文字)。付けないときは None。長ければ縮め、それでも入らなければ末尾を「…」に"""
    label = re.sub(r"\s+", " ", str(label or "").strip())
    if not label or not st.get("show_label", True):
        return None
    f = T._font(fp, idx, T.LABEL_MIN)
    h = vpos([label], f, f.size, 0, 0)[1][1]
    if h > max_h:
        f = T._font(fp, idx, max(T.LABEL_FLOOR, int(T.LABEL_MIN * max_h / h)))
        while len(label) > 1 and vpos([label], f, f.size, 0, 0)[1][1] > max_h:
            label = label[:-2].rstrip() + "…"
    return f, label


def vlabel_h(vl) -> float:
    return vpos([vl[1]], vl[0], vl[0].size, 0, 0)[1][1] if vl else 0.0


def draw_vlabel(img, vl, x_right, y, st, accent, avoid=None):
    """縦書きの見出し（大きな文字の右の列）。上品は金の字と下に細い縦線、ポップは写真の色の縦長の札"""
    if not vl:
        return
    f, label = vl
    z = f.size
    pos, (w, h) = vpos([label], f, z, x_right, y)
    if st["effect"] == "outline":
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle([x_right - w - z * 0.3, y - z * 0.3, x_right + z * 0.3, y + h + z * 0.2],
                                            radius=int(z * 0.25), fill=255)
        T._paint(img, accent, m)
        T._paint(img, (255, 255, 255), T._mask(pos, f))
        return
    color = accent if st["effect"] == "shadow" else T._hex(st["text_color"])
    sh = T._mask(pos, f, max(2, z // 18)).filter(ImageFilter.GaussianBlur(max(3, z * 0.08)))
    T._paint(img, (0, 0, 0), sh, 0.9)
    T._paint(img, color, T._mask(pos, f))
    if st["effect"] == "shadow":
        cx = x_right - w / 2
        y0, y1 = y + h + z * 0.4, y + h + z * 2.6
        if y1 < H - 16 and not T._hide(avoid, (cx - 8, y0, cx + 8, y1)):
            line = Image.new("L", (W, H), 0)
            t = max(2, z // 14)
            ImageDraw.Draw(line).rectangle([cx - t / 2, y0, cx + t / 2, y1], fill=255)
            T._paint(img, accent, line, 0.9)


# ================================================================ 主役の切り抜き
_CUT_CACHE: dict = {}


def cutout(img: Image.Image, subject):
    """大事な所の枠（1280×720 の座標）の中の主役を切り抜いた透明度（H×W・0〜1）。うまく切れないときは None。
    同じ写真・同じ枠なら、前の結果を使う（3つのデザインで同じ写真を使うため）"""
    if not subject:
        return None
    try:
        import cv2
    except ImportError:
        return None
    small = img.convert("RGB").resize((W // 2, H // 2), Image.BILINEAR)
    key = (hashlib.md5(small.tobytes()).hexdigest(), tuple(int(round(v)) for v in subject))
    if key not in _CUT_CACHE:
        if len(_CUT_CACHE) > 12:
            _CUT_CACHE.clear()
        _CUT_CACHE[key] = _grabcut(cv2, small, subject)
    return _CUT_CACHE[key]


def _grabcut(cv2, small: Image.Image, subject):
    """2つのやり方で切り抜き、両方が同じ形になったときだけ使う（重なった所）。違う形・縁が写真の輪郭に沿わないなら None。
    1. 枠だけを手がかりにする  2. 枠の中で、枠のまわりと同じ色（机・紙など）を、はじめから背景の見込みにする
    切り抜きが少しでもずれると白いふちなどで目立つので、確かなときだけにする"""
    sw, sh = small.size
    x0, y0, x1, y1 = (int(round(v / 2)) for v in subject)
    x0, y0, x1, y1 = max(x0, 1), max(y0, 1), min(x1, sw - 2), min(y1, sh - 2)
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    bgr = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2BGR)
    lab_img = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    edges = cv2.dilate(cv2.Canny(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 40, 120), _ell(cv2, 5)) > 0
    rect = (x0, y0, x1 - x0, y1 - y0)
    found = []
    for way in ("rect", "mask"):
        mask = np.zeros((sh, sw), np.uint8)
        if way == "mask":
            out = np.ones((sh, sw), bool)
            out[max(y0 - 12, 0):y1 + 12, max(x0 - 12, 0):x1 + 12] = False
            if out.sum() < 200:
                continue
            bg = np.median(lab_img[out], axis=0)
            mask[y0:y1, x0:x1] = cv2.GC_PR_FGD
            near_bg = np.linalg.norm(lab_img - bg, axis=2) < 14
            mask[(mask == cv2.GC_PR_FGD) & near_bg] = cv2.GC_PR_BGD
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.setRNGSeed(0)   # 毎回同じ結果にする（GrabCut は乱数を使う）
        try:
            if way == "rect":
                cv2.grabCut(bgr, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
            else:
                cv2.grabCut(bgr, mask, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
        except cv2.error:
            continue
        fg = _clean_fg(cv2, np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8), rect, lab_img)
        if fg is None:
            continue
        border = (fg - cv2.erode(fg, _ell(cv2, 3))) > 0
        inner = np.zeros_like(border)
        inner[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = True   # 枠の外周に沿った縁（主役が枠からはみ出す所）は数えない
        b = border & inner
        score = float(edges[b].mean()) if b.sum() > 20 else float(edges[border].mean() if border.any() else 0)
        if score < MIN_EDGE:
            return None
        found.append(fg)
    if len(found) < 2:
        return None
    a, b = found[0] > 0, found[1] > 0
    if (a & b).sum() / max((a | b).sum(), 1) < MIN_AGREE:
        return None
    fg = cv2.morphologyEx((a & b).astype(np.uint8), cv2.MORPH_OPEN, _ell(cv2, 3))
    full = cv2.GaussianBlur(cv2.resize(fg * 255, (W, H), interpolation=cv2.INTER_LINEAR), (0, 0), 1.0)
    return full.astype(np.float32) / 255


def _ell(cv2, n):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))


def _clean_fg(cv2, fg, rect, lab_img):
    """切り抜いた形を整える。小さすぎる・枠のほぼ全部・ばらばら・まわりと色が同じなら None"""
    x0, y0, w, h = rect
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, _ell(cv2, 3))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if n <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    big = int(areas.max())
    fg = np.isin(lab, 1 + np.where(areas >= big * 0.2)[0]).astype(np.uint8)
    total = int(fg.sum())
    if not 0.05 <= total / (w * h) <= 0.9 or big / total < 0.7:
        return None
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, _ell(cv2, 7))
    ring = cv2.dilate(fg, _ell(cv2, 15)) - fg
    if ring.sum() < 50:
        return None
    if float(np.linalg.norm(lab_img[fg > 0].mean(0) - lab_img[ring > 0].mean(0))) < MIN_CONTRAST:
        return None
    return fg


def crop_alpha(a, crop):
    """切り抜きを、写真と同じように切り出して広げる（横に文字・縦書き）"""
    if a is None:
        return None
    x0, y0, cw, ch = crop
    im = _alpha_img(a).crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.BILINEAR)
    return np.asarray(im, np.float32) / 255


def _alpha_img(a) -> Image.Image:
    return Image.fromarray(np.clip(np.asarray(a) * 255, 0, 255).astype(np.uint8))


def _box_alpha(subject) -> Image.Image:
    """切り抜けないときの代わり: 大事な所の枠をぼかした形"""
    x0, y0, x1, y1 = subject
    r = max(12, int(min(x1 - x0, y1 - y0) * 0.15))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rounded_rectangle([x0, y0, x1, y1], radius=r, fill=255)
    return m.filter(ImageFilter.GaussianBlur(max(8, r)))


def _dilate(m: Image.Image, r: int) -> Image.Image:
    import cv2
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    return Image.fromarray(cv2.dilate(np.asarray(m), k))


# ================================================================ 効果
def apply_effect(img: Image.Image, effect: str, cut, subject, st: dict) -> Image.Image:
    """写真の効果。cut = 主役の切り抜き（None なら枠をぼかした形で代わりにする。白いふちは切り抜けたときだけ）"""
    if effect == "none" or not subject:
        return img
    elegant = st.get("name") == "elegant"
    if effect == "sticker" and cut is not None:
        m = _alpha_img(cut)
        out = ImageEnhance.Brightness(img.filter(ImageFilter.GaussianBlur(2.5))).enhance(0.8)
        big = _dilate(m, 10)
        T._paint(out, (0, 0, 0), ImageChops.offset(big.filter(ImageFilter.GaussianBlur(10)), 8, 12), 0.55)
        if elegant:   # 上品: 白いふちではなく、やわらかい光
            T._paint(out, (255, 240, 214), _dilate(m, 14).filter(ImageFilter.GaussianBlur(9)), 0.7)
        else:
            T._paint(out, (255, 255, 255), big.filter(ImageFilter.GaussianBlur(0.8)))
        out.paste(img, (0, 0), m)
        return out
    # focus（と、切り抜けなかった sticker）・depth: 主役以外を暗く（focus は少しぼかす）
    m = _alpha_img(cut).filter(ImageFilter.GaussianBlur(2)) if cut is not None else _box_alpha(subject)
    blur, dark = {"depth": (0.0, 0.85)}.get(effect, (4.0, 0.62 if elegant else 0.68))
    bg = img.filter(ImageFilter.GaussianBlur(blur if cut is not None else blur / 2)) if blur else img
    return Image.composite(img, ImageEnhance.Brightness(bg).enhance(dark), m)


# ================================================================ バッジ
def _badge_color(st: dict, accent):
    c = str(st.get("badge_color") or "auto").strip().lower()
    return tuple(accent) if c == "auto" else T._hex(c, (181, 55, 43))


def badge_image(text: str, st: dict, color, fp, idx) -> Image.Image:
    """バッジの画像（RGBA。まわりに影の分の余白つき）"""
    kind = {"shadow": "seal", "outline": "burst"}.get(st["effect"], "tag")
    color = tuple(int(c) for c in color[:3])
    if kind == "seal":   # 上品: 朱色の落款。右の列から縦に。少し傾け、かすれを入れる
        n = len(text)
        rows, cols = (n, 1) if n <= 3 else ((n + 1) // 2, 2)
        cell = 60 if n <= 2 else (52 if n == 3 else 48)
        f = T._font(fp, idx, cell)
        pad, M = int(cell * 0.32), 24
        bw, bh = int(cols * cell * 1.04 + 2 * pad), int(rows * cell * 1.04 + 2 * pad)
        im = Image.new("RGBA", (bw + 2 * M, bh + 2 * M), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([M, M, M + bw, M + bh], radius=int(cell * 0.12), fill=color + (255,))
        ins = int(cell * 0.13)
        d.rounded_rectangle([M + ins, M + ins, M + bw - ins, M + bh - ins], radius=int(cell * 0.08),
                            outline=(255, 255, 255, 225), width=max(2, cell // 22))
        for k, ch in enumerate(text):   # 縦書きなので「ー」などは縦向きに回し、小さい字は右上に寄せる
            c, r = divmod(k, rows)
            x, y = M + bw - pad - cell * 1.04 * (c + 0.5), M + pad + cell * 1.04 * (r + 0.5)
            if ch in _ROT:
                q = int(cell * 0.75)
                t = Image.new("RGBA", (2 * q, 2 * q), (0, 0, 0, 0))
                ImageDraw.Draw(t).text((q, q), ch, font=f, fill=(255, 255, 255, 255), anchor="mm")
                im.alpha_composite(t.rotate(-90, resample=Image.BICUBIC), (int(round(x)) - q, int(round(y)) - q))
                continue
            if ch in _SMALL:
                x, y = x + cell * 0.1, y - cell * 0.1
            d.text((x, y), ch, font=f, fill=(255, 255, 255, 255), anchor="mm")
        rng = np.random.default_rng(int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16))
        speck = Image.fromarray(((rng.random((im.height, im.width)) < 0.035) * 255).astype(np.uint8))
        a = np.asarray(im.getchannel("A"), np.float32) * (1 - np.asarray(speck.filter(ImageFilter.GaussianBlur(0.7)),
                                                                          np.float32) / 255 * 0.8)
        im.putalpha(Image.fromarray(a.astype(np.uint8)))
        sh = im.getchannel("A").filter(ImageFilter.GaussianBlur(5))
        out = Image.new("RGBA", im.size, (0, 0, 0, 0))
        out.paste((0, 0, 0, 255), (3, 5), sh.point(lambda v: int(v * 0.35)))
        out.alpha_composite(im)
        return out.rotate(4, resample=Image.BICUBIC, expand=True)
    if kind == "burst":   # ポップ: ギザギザの丸いシール（白いふち・影つき）。少し傾ける
        lines = [text] if len(text) <= 3 else [text[:(len(text) + 1) // 2], text[(len(text) + 1) // 2:]]
        R, r_in, N, M = 112, 98, 22, 30
        S = 2 * R + 2 * M
        star = lambda ro, ri: [(S / 2 + (ro if k % 2 == 0 else ri) * math.cos(math.pi * k / N - math.pi / 2),  # noqa: E731
                                S / 2 + (ro if k % 2 == 0 else ri) * math.sin(math.pi * k / N - math.pi / 2))
                               for k in range(2 * N)]
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sh = Image.new("L", (S, S), 0)
        ImageDraw.Draw(sh).polygon(star(R + 8, r_in + 8), fill=255)
        im.paste((0, 0, 0, 255), (6, 9), sh.filter(ImageFilter.GaussianBlur(7)).point(lambda v: int(v * 0.45)))
        d = ImageDraw.Draw(im)
        d.polygon(star(R + 8, r_in + 8), fill=(255, 255, 255, 255))
        d.polygon(star(R, r_in), fill=color + (255,))
        size = int(min(r_in * 1.25 / max(len(s) for s in lines), r_in * 1.2 / (len(lines) * 1.05)))
        f = T._font(fp, idx, size)
        dark = tuple(int(c * 0.45) for c in color)
        for li, s in enumerate(lines):
            d.text((S / 2, S / 2 + (li - (len(lines) - 1) / 2) * size * 1.05), s, font=f, anchor="mm",
                   fill=(255, 255, 255, 255), stroke_width=max(2, size // 14), stroke_fill=dark + (255,))
        return im.rotate(10, resample=Image.BICUBIC, expand=True)
    # シンプル: 角丸の札（影つき）
    size = 48
    f = T._font(fp, idx, size)
    bw, bh, M = int(f.getlength(text) + 2 * 26), int(size * 1.7), 16
    im = Image.new("RGBA", (bw + 2 * M, bh + 2 * M), (0, 0, 0, 0))
    sh = Image.new("L", im.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle([M, M, M + bw, M + bh], radius=12, fill=255)
    im.paste((0, 0, 0, 255), (3, 5), sh.filter(ImageFilter.GaussianBlur(6)).point(lambda v: int(v * 0.4)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([M, M, M + bw, M + bh], radius=12, fill=color + (255,))
    d.text((M + bw / 2, M + bh / 2), text, font=f, anchor="mm", fill=(255, 255, 255, 255))
    return im


def _overlap(a, b) -> float:
    """枠 a のうち、枠 b と重なる割合"""
    if not b:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / max((a[2] - a[0]) * (a[3] - a[1]), 1.0)


def draw_badge(img: Image.Image, text: str, st: dict, accent, tbox, subject, fp, idx):
    """バッジを、文字にも大事な所にも重ならない、文字の近くか画面の角に置く。置いた枠（置けなければ None）"""
    if not text:
        return None
    b = badge_image(text, st, _badge_color(st, accent), fp, idx)
    bw, bh = b.size
    det = T._detail(img)
    m = 12
    cands = [(m, m), (W - m - bw, m), (m, H - m - bh), (W - m - bw, H - m - bh)]
    if tbox:   # 文字の近く（文字の右上・左上・下・上）
        x0, y0, x1, y1 = tbox
        cands += [(x1 - bw * 0.25, y0 - bh * 0.7), (x0 - bw * 0.75, y0 - bh * 0.7), (x0, y1 + 4), (x1 - bw, y1 + 4),
                  (x0, y0 - bh - 4), (x1 - bw, y0 - bh - 4), (x1 + 4, y1 - bh)]
    best = None
    for x, y in cands:
        x, y = min(max(x, 4), W - bw - 4), min(max(y, 4), H - bh - 4)
        box = (x + bw * 0.15, y + bh * 0.15, x + bw * 0.85, y + bh * 0.85)   # 影・余白を除いた中身
        over_t, over_s = _overlap(box, tbox), _overlap(box, subject)
        if over_t > 0.12:   # 文字にはほとんど重ねない（シールが文字の角に少しかかるのはよい）
            continue
        near = 0.0
        if tbox:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            near = math.hypot(cx - (tbox[0] + tbox[2]) / 2, cy - (tbox[1] + tbox[3]) / 2) / W
        sc = 2.0 * over_s + 0.5 * T._busy(det, box) + 0.3 * near
        if best is None or sc < best[0]:
            best = (sc, x, y, box, over_s)
    if best is None or best[4] > 0.25:   # どこに置いても大事な所を隠すなら、付けない
        return None
    _, x, y, box, _ = best
    img.paste(b.convert("RGB"), (int(x), int(y)), b.getchannel("A"))
    return box


# ================================================================ 仕上げ
def glyph_cover(cut, glyphs) -> float:
    """文字1字ずつの枠のうち、主役が手前に来て隠れる割合の、いちばん大きいもの"""
    a = np.asarray(cut)
    worst = 0.0
    for x0, y0, x1, y1 in glyphs:
        x0, y0, x1, y1 = max(int(x0), 0), max(int(y0), 0), min(int(x1), W), min(int(y1), H)
        if x1 > x0 and y1 > y0:
            worst = max(worst, float(a[y0:y1, x0:x1].mean()))
    return worst


def finish(img, base, cut, subject, design: dict, st: dict, accent, tbox, fp, idx, glyphs=None,
           label_box=None) -> Image.Image:
    """文字を描いたあと: 文字を主役の後ろに回す（depth）・バッジを置く。
    glyphs: 大きな文字1字ずつの枠。主役が1字でも大きく隠すなら（読めなくなるので）後ろには回さない。
    label_box: 見出しの枠。こちらは少しでも隠れるなら回さない"""
    if design.get("effect") == "depth" and cut is not None:
        if (glyph_cover(cut, glyphs or [tbox]) <= DEPTH_MAX_COVER
                and (not label_box or glyph_cover(cut, [label_box]) <= LABEL_MAX_COVER)):
            img.paste(base, (0, 0), _alpha_img(cut))
    if design.get("badge"):
        draw_badge(img, design["badge"], st, accent, tbox, subject, fp, idx)
    return img
