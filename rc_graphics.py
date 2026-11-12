"""テロップの種類（吹き出し・スペック表・比較表・STEP・場所）、章の進み具合のバー、
オープニング・エンディング動画の自動生成。

- テロップと進み具合のバーは、ASS字幕の図形（\\p1）と文字で描く（rc_ass.build_ass から呼ばれる）
- オープニング・エンディングは FFmpeg で動画ファイルを作る（graphics.py / finish.py から呼ばれる）

テロップの文字の書き方（演出プランの text。画面の表でも同じ書き方で直せます）:
  吹き出し  bubble  : 「なにこれ！？」          （「／」で改行）
  スペック  spec    : 「見出し／重さ：1.2kg／価格：9,800円」  （1行目が見出し。「：」の左が項目名）
  比較表    compare : 「｜A社｜B社／重さ｜1.2kg｜1.5kg／価格｜1万円｜8千円」（「／」で行、「｜」で列。1行目が見出し）
  STEP     step    : 「STEP 2 穴をあける」または「穴をあける」（番号を書かなければ、動画の中で順に数える）
  場所      place   : 「高尾山 山頂／標高599m」  （2行目は小さく出る）
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

from rc_template import Template

CARD_STYLES = ("bubble", "spec", "compare", "step", "place")
STYLE_JA = {"bubble": "吹き出し", "spec": "スペック表", "compare": "比較表", "step": "STEPカード", "place": "場所カード"}

FONT = "BIZ UDPGothic"
# テンプレートに書いていない値は、ここの値を使う（古いテンプレートでも動くように）
CARD_DEFAULTS = {
    "bubble": {"size": 60, "color": "#1A1A1A", "fill": "#FFFFFF", "border_color": "#1A1A1A", "border": 5,
               "position": "right", "animation": "pop"},
    # 表は白地に濃い字（映像の上でも読みやすい）。line_color = 行・列の区切りの線
    "spec": {"size": 44, "head_size": 50, "color": "#1A1A1A", "label_color": "#2B6CB0", "head_color": "#FFFFFF",
             "head_fill": "#2B6CB0", "fill": "#FFFFFF", "line_color": "#1A1A1A", "opacity": 0.96,
             "position": "right", "animation": "slide"},
    "compare": {"size": 44, "head_size": 46, "color": "#1A1A1A", "label_color": "#2B6CB0", "head_color": "#FFFFFF",
                "head_fill": "#2B6CB0", "fill": "#FFFFFF", "stripe": "#EAF1F8", "line_color": "#1A1A1A",
                "opacity": 0.96, "position": "top", "animation": "pop"},
    "step": {"size": 56, "number_size": 76, "color": "#1A1A1A", "fill": "#FFFFFF", "accent": "#FF7A00",
             "number_color": "#FFFFFF", "position": "top-left", "animation": "slide"},
    "place": {"size": 58, "sub_size": 36, "color": "#FFFFFF", "sub_color": "#CFE8D8", "accent": "#2FA36B",
              "fill": "#101820", "opacity": 0.85, "position": "bottom-left", "margin_bottom": 210,
              "animation": "slide"},
}
PROGRESS_DEFAULTS = {"enabled": False, "position": "top", "height": 8, "gap": 6, "color": "#FFE14D",
                     "track_color": "#FFFFFF", "track_opacity": 0.35, "show_title": True, "title_size": 30,
                     "title_color": "#FFFFFF", "title_box": "#000000", "title_opacity": 0.55,
                     "title_format": "{title}", "title_seconds": 0, "title_align": "left"}
OPENING_DEFAULTS = {"enabled": False, "duration": 3.0, "logo": "", "channel_name": "", "tagline": "",
                    "background": "#101820", "background_image": "", "accent_color": "#FFE14D",
                    "color": "#FFFFFF", "size": 110, "tagline_size": 44, "sound": ""}
ENDING_DEFAULTS = {"enabled": False, "duration": 20.0, "message": "チャンネル登録よろしくお願いします！",
                   "sub_message": "ほかの動画もぜひ見てください", "channel_name": "", "video_boxes": 2,
                   "background": "#101820", "background_image": "", "accent_color": "#FFE14D",
                   "color": "#FFFFFF", "size": 76, "sub_size": 40, "music": "", "music_volume_db": -12}

# YouTube の終了画面の要素を置く枠（1920x1080 での位置。ほかの解像度では比率で直す）
END_BOX_W, END_BOX_H = 616, 347          # 動画・再生リストの枠（16:9）
END_SUB_D = 290                          # チャンネル登録の丸
GEN_DIR = "generated"
_GEN_VERSION = 1


# ====================================================================== 共通
def _t(sec: float) -> str:
    cs = int(round(max(sec, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")


def _col(hex_rgb: str) -> str:
    """#RRGGBB → ASSの上書き用 &HBBGGRR&"""
    h = str(hex_rgb).lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _alpha(opacity: float) -> str:
    op = min(max(float(opacity), 0.0), 1.0)
    return f"&H{round((1 - op) * 255):02X}&"


def _n(v: float) -> str:
    return str(int(round(v)))


def split_lines(text: str) -> list[str]:
    """「／」（または改行）で行に分ける。半角の / は「1/2」などに使うので区切りにしない"""
    return [s.strip() for s in re.split(r"[／\n]|\\N", text or "") if s.strip()]


def split_cells(row: str) -> list[str]:
    cells = [c.strip() for c in re.split(r"[｜|]", row)]
    while len(cells) > 1 and not cells[-1]:
        cells.pop()
    return cells


def card_config(tpl: Template | None, kind: str) -> dict:
    c = dict(CARD_DEFAULTS[kind])
    base = (tpl.get("caption", {}) if tpl else {}) or {}
    c["font"] = base.get("font", FONT)
    if tpl:
        c.update(tpl.get("telop", {}).get(kind, {}) or {})
    return c


# ---------------------------------------------------------------- 文字の幅
_FONT_CACHE: dict = {}


def _font_for(tpl: Template | None, family: str):
    """フォント名に合うフォントファイルを fonts フォルダなどから探す（見つからなければ None）"""
    key = (str(tpl.dir) if tpl else "", family)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    found = None
    try:
        from PIL import ImageFont
        cands = []
        if tpl and tpl.fonts_dir:
            cands += sorted(p for p in tpl.fonts_dir.iterdir() if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
        if tpl:
            ff = (tpl.get("thumbnail", {}) or {}).get("font_file", "")
            if ff:
                p = Path(ff) if Path(ff).is_absolute() else tpl.dir / ff
                if p.exists():
                    cands.append(p)
        for p in cands:
            for idx in range(4 if p.suffix.lower() == ".ttc" else 1):
                try:
                    f = ImageFont.truetype(str(p), 100, index=idx)
                except OSError:
                    break
                if f.getname()[0].lower() == family.lower():
                    found = (str(p), idx)
                    break
            if found:
                break
    except Exception:  # noqa: BLE001  幅の計算は目安なので、失敗しても続ける
        found = None
    _FONT_CACHE[key] = found
    return found


def text_width(text: str, size: float, tpl: Template | None = None, family: str = FONT) -> float:
    """文字の幅（画面の点の数）の目安"""
    f = _font_for(tpl, family)
    if f:
        try:
            from PIL import ImageFont
            # libass は「上下の高さ」が size になるように縮めるので、少し小さめになる。広めに見積もってそのまま使う
            return ImageFont.truetype(f[0], 100, index=f[1]).getlength(text) * size / 100
        except Exception:  # noqa: BLE001
            pass
    wsum = 0.0
    for ch in text:
        ea = unicodedata.east_asian_width(ch)
        wsum += 1.0 if ea in ("W", "F") else (0.32 if ch == " " else 0.6)
    return wsum * size


# ---------------------------------------------------------------- 図形
def rrect(x: float, y: float, w: float, h: float, r: float, corners: str = "tl tr br bl") -> str:
    """角の丸い四角形の図形（ASS の描画コマンド）"""
    r = max(0.0, min(r, w / 2, h / 2))
    rs = {k: (r if k in corners.split() else 0) for k in ("tl", "tr", "br", "bl")}
    x2, y2 = x + w, y + h
    p = [f"m {_n(x + rs['tl'])} {_n(y)}", f"l {_n(x2 - rs['tr'])} {_n(y)}"]
    if rs["tr"]:
        p.append(f"b {_n(x2)} {_n(y)} {_n(x2)} {_n(y)} {_n(x2)} {_n(y + rs['tr'])}")
    p.append(f"l {_n(x2)} {_n(y2 - rs['br'])}")
    if rs["br"]:
        p.append(f"b {_n(x2)} {_n(y2)} {_n(x2)} {_n(y2)} {_n(x2 - rs['br'])} {_n(y2)}")
    p.append(f"l {_n(x + rs['bl'])} {_n(y2)}")
    if rs["bl"]:
        p.append(f"b {_n(x)} {_n(y2)} {_n(x)} {_n(y2)} {_n(x)} {_n(y2 - rs['bl'])}")
    p.append(f"l {_n(x)} {_n(y + rs['tl'])}")
    if rs["tl"]:
        p.append(f"b {_n(x)} {_n(y)} {_n(x)} {_n(y)} {_n(x + rs['tl'])} {_n(y)}")
    return " ".join(p)


def circle(cx: float, cy: float, r: float) -> str:
    k = 0.5523 * r
    return (f"m {_n(cx)} {_n(cy - r)} b {_n(cx + k)} {_n(cy - r)} {_n(cx + r)} {_n(cy - k)} {_n(cx + r)} {_n(cy)} "
            f"b {_n(cx + r)} {_n(cy + k)} {_n(cx + k)} {_n(cy + r)} {_n(cx)} {_n(cy + r)} "
            f"b {_n(cx - k)} {_n(cy + r)} {_n(cx - r)} {_n(cy + k)} {_n(cx - r)} {_n(cy)} "
            f"b {_n(cx - r)} {_n(cy - k)} {_n(cx - k)} {_n(cy - r)} {_n(cx)} {_n(cy - r)}")


def ring(cx: float, cy: float, r: float, width: float) -> str:
    """輪（外の円を時計回り、内の円を反時計回りに描いて穴をあける）"""
    k, ri = 0.5523, r - width
    outer = circle(cx, cy, r)
    ki = k * ri
    inner = (f"m {_n(cx)} {_n(cy - ri)} b {_n(cx - ki)} {_n(cy - ri)} {_n(cx - ri)} {_n(cy - ki)} {_n(cx - ri)} {_n(cy)} "
             f"b {_n(cx - ri)} {_n(cy + ki)} {_n(cx - ki)} {_n(cy + ri)} {_n(cx)} {_n(cy + ri)} "
             f"b {_n(cx + ki)} {_n(cy + ri)} {_n(cx + ri)} {_n(cy + ki)} {_n(cx + ri)} {_n(cy)} "
             f"b {_n(cx + ri)} {_n(cy - ki)} {_n(cx + ki)} {_n(cy - ri)} {_n(cx)} {_n(cy - ri)}")
    return outer + " " + inner


class Layer:
    """1つのカード（またはオープニングなどの部品）をまとめて描く。全部の部品が同じ動きで出入りする。"""

    def __init__(self, s: float, e: float, anim: str = "fade", dx: float = 0, dy: float = 0,
                 base_layer: int = 4, fade_in: int | None = None, fade_out: int = 200):
        self.s, self.e, self.anim = s, e, anim
        self.dx, self.dy = dx, dy
        self.base = base_layer
        self.fade = (fade_in if fade_in is not None else {"pop": 120, "slide": 160}.get(anim, 220), fade_out)
        self.events: list[str] = []

    def _pos(self, x: float, y: float) -> str:
        if self.anim in ("slide", "pop") and (self.dx or self.dy):
            dur = 260 if self.anim == "slide" else 200
            return f"\\move({_n(x + self.dx)},{_n(y + self.dy)},{_n(x)},{_n(y)},0,{dur})"
        return f"\\pos({_n(x)},{_n(y)})"

    def _head(self, layer_add: int, tags: str, s: float | None = None, e: float | None = None) -> str:
        s = self.s if s is None else s
        e = self.e if e is None else e
        return (f"Dialogue: {self.base + layer_add},{_t(s)},{_t(e)},Card,,0,0,0,,"
                f"{{\\fad({self.fade[0]},{self.fade[1]}){tags}}}")

    def shape(self, path: str, color: str, opacity: float = 1.0, layer: int = 0, border: float = 0,
              border_color: str = "#000000", extra: str = "", s: float | None = None, e: float | None = None,
              anchor: str | None = None, border_opacity: float | None = None):
        """図形を描く。座標は画面の左上が (0,0)。anchor で位置の指定（\\pos や \\move）を差し替えられる"""
        tags = (f"\\an7{anchor if anchor is not None else self._pos(0, 0)}\\p1\\bord{_n(border)}\\shad0\\1c{_col(color)}\\1a{_alpha(opacity)}"
                f"\\3c{_col(border_color)}\\3a{_alpha(opacity if border_opacity is None else border_opacity)}{extra}")
        self.events.append(self._head(layer, tags, s, e) + path + "{\\p0}")

    def text(self, x: float, y: float, an: int, text: str, size: float, color: str, font: str = FONT,
             bold: bool = True, layer: int = 1, border: float = 0, border_color: str = "#000000",
             extra: str = "", s: float | None = None, e: float | None = None):
        tags = (f"\\an{an}{self._pos(x, y)}\\fn{font}\\fs{_n(size)}\\b{1 if bold else 0}\\1c{_col(color)}"
                f"\\bord{_n(border)}\\3c{_col(border_color)}\\shad0{extra}")
        self.events.append(self._head(layer, tags, s, e) + _esc(text))


def _anchor(pos: str, bw: float, bh: float, w: int, h: int, margin: float = 60,
            margin_bottom: float = 200, top_extra: float = 0) -> tuple[float, float]:
    """カードの左上の位置（top_extra: 章の進み具合のバーが上にあるときに、その分だけ下げる）"""
    pos = pos or "middle"
    if "left" in pos:
        x = margin
    elif "right" in pos:
        x = w - margin - bw
    else:
        x = (w - bw) / 2
    if pos.startswith("top"):
        y = margin + top_extra
    elif pos.startswith("bottom"):
        y = h - margin_bottom - bh
    elif pos in ("left", "right"):
        y = max(margin, (h - bh) / 2 - h * 0.06)
    else:
        y = (h - bh) / 2
    return x, y


def _slide_from(pos: str) -> tuple[float, float]:
    if "left" in pos:
        return -60, 0
    if "right" in pos:
        return 60, 0
    if pos.startswith("bottom"):
        return 0, 40
    return 0, -40


def _fit(build, max_w: float):
    """build(k) が返す幅が画面に収まるまで、文字を小さくする"""
    k = 1.0
    out = build(k)
    if out[0] > max_w:
        k = max(0.4, max_w / out[0])
        out = build(k)
    return out


# ====================================================================== テロップのカード
def card_style_line(tpl: Template | None = None) -> str:
    font = (tpl.get("caption", {}) if tpl else {}).get("font", FONT)
    return (f"Style: Card,{font},48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,"
            "7,0,0,0,1")


def _bubble(L: Layer, tpl, c, text, w, h):
    lines = split_lines(text) or [text]
    font = c["font"]

    def build(k):
        fs = c["size"] * k
        tw = max(text_width(ln, fs, tpl, font) for ln in lines)
        return tw + fs * 1.3, fs
    bw, fs = _fit(build, w * 0.6)
    lh = fs * 1.25
    bh = lh * len(lines) + fs * 0.8
    tail = fs * 0.8
    pos = c.get("position", "right")
    x, y = _anchor(pos, bw, bh + tail, w, h, margin=80, top_extra=c.get("_top", 0))
    if pos in ("left", "right"):
        y = h * 0.18
    # しっぽは、画面の中央寄りの下に向ける
    left_tail = "right" in pos
    r = min(fs * 0.7, bh / 2)
    tx0 = x + (bw * 0.22 if left_tail else bw * 0.62)
    tx1 = tx0 + fs * 0.7
    tip = (tx0 - fs * 0.5 if left_tail else tx1 + fs * 0.5, y + bh + tail)
    x2, y2 = x + bw, y + bh
    path = " ".join([
        f"m {_n(x + r)} {_n(y)} l {_n(x2 - r)} {_n(y)} b {_n(x2)} {_n(y)} {_n(x2)} {_n(y)} {_n(x2)} {_n(y + r)}",
        f"l {_n(x2)} {_n(y2 - r)} b {_n(x2)} {_n(y2)} {_n(x2)} {_n(y2)} {_n(x2 - r)} {_n(y2)}",
        f"l {_n(tx1)} {_n(y2)} l {_n(tip[0])} {_n(tip[1])} l {_n(tx0)} {_n(y2)}",
        f"l {_n(x + r)} {_n(y2)} b {_n(x)} {_n(y2)} {_n(x)} {_n(y2)} {_n(x)} {_n(y2 - r)}",
        f"l {_n(x)} {_n(y + r)} b {_n(x)} {_n(y)} {_n(x)} {_n(y)} {_n(x + r)} {_n(y)}",
    ])
    L.shape(path, c["fill"], 1.0, 0, border=c.get("border", 5), border_color=c.get("border_color", "#1A1A1A"))
    for i, ln in enumerate(lines):
        L.text(x + bw / 2, y + fs * 0.4 + lh * (i + 0.5), 5, ln, fs, c["color"], font)


def _spec(L: Layer, tpl, c, text, w, h):
    lines = split_lines(text) or ["スペック"]
    head, items = lines[0], lines[1:8]
    font = c["font"]
    rows = []
    for it in items:
        m = re.split(r"[：:]", it, maxsplit=1)
        rows.append((m[0].strip(), m[1].strip()) if len(m) == 2 and m[0].strip() else ("", it))

    def build(k):
        fs, hs = c["size"] * k, c["head_size"] * k
        pad = fs * 0.7
        lw = max([text_width(a, fs, tpl, font) for a, _ in rows if a] or [0])
        vw = max([text_width(b, fs, tpl, font) for _, b in rows] or [0])
        content = (lw + fs * 1.2 + vw) if lw else vw
        bw = max(text_width(head, hs, tpl, font) + pad * 2, content + pad * 2, 380 * k)
        return bw, fs, hs, pad
    bw, fs, hs, pad = _fit(build, w * 0.55)
    hh, rh = hs * 1.7, fs * 1.55
    bh = hh + rh * len(rows) + (pad * 0.6 if rows else 0)
    pos = c.get("position", "right")
    x, y = _anchor(pos, bw, bh, w, h, top_extra=c.get("_top", 0))
    r = fs * 0.35
    if rows:
        L.shape(rrect(x, y, bw, bh, r), c["fill"], c.get("opacity", 0.88), 0)
    L.shape(rrect(x, y, bw, hh, r, "tl tr" if rows else "tl tr br bl"), c["head_fill"], 1.0, 1)
    L.text(x + pad, y + hh / 2, 4, head, hs, c["head_color"], font, layer=2)
    for i, (a, b) in enumerate(rows):
        cy = y + hh + pad * 0.3 + rh * (i + 0.5)
        if i:
            L.shape(rrect(x + pad * 0.6, cy - rh / 2 - 1, bw - pad * 1.2, 2, 0), c.get("line_color", "#FFFFFF"), 0.18, 1)
        if a:
            L.text(x + pad, cy, 4, a, fs, c["label_color"], font, layer=2)
            L.text(x + bw - pad, cy, 6, b, fs, c["color"], font, layer=2)
        else:
            L.text(x + pad, cy, 4, b, fs, c["color"], font, layer=2)


def _compare(L: Layer, tpl, c, text, w, h):
    rows = [split_cells(r) for r in split_lines(text)][:7]
    if not rows:
        return
    ncol = min(max(len(r) for r in rows), 5)
    rows = [(r + [""] * ncol)[:ncol] for r in rows]
    font = c["font"]

    def build(k):
        fs, hs = c["size"] * k, c["head_size"] * k
        pad = fs * 0.75
        cols = []
        for j in range(ncol):
            cw = max(text_width(r[j], hs if i == 0 else fs, tpl, font) for i, r in enumerate(rows))
            cols.append(max(cw, fs * 2) + pad * 2)
        return sum(cols), fs, hs, pad, cols
    bw, fs, hs, pad, cols = _fit(build, w - 160)
    rh = fs * 1.7
    bh = rh * len(rows)
    x, y = _anchor(c.get("position", "middle"), bw, bh, w, h, top_extra=c.get("_top", 0))
    r = fs * 0.35
    L.shape(rrect(x, y, bw, bh, r), c["fill"], c.get("opacity", 0.9), 0)
    L.shape(rrect(x, y, bw, rh, r, "tl tr" if len(rows) > 1 else "tl tr br bl"), c["head_fill"], 1.0, 1)
    for i in range(2, len(rows), 2):
        L.shape(rrect(x, y + rh * i, bw, rh, r, "bl br" if i == len(rows) - 1 else ""),
                c.get("stripe", "#22303F"), c.get("opacity", 0.9), 1)
    # 列の区切りの線
    cx = x
    for j, cw in enumerate(cols[:-1]):
        cx += cw
        L.shape(rrect(cx - 1, y + rh * 0.18, 2, bh - rh * 0.36, 0), c.get("line_color", "#FFFFFF"), 0.2, 2)
    for i, row in enumerate(rows):
        cy = y + rh * (i + 0.5)
        cx = x
        for j, cell in enumerate(row):
            if cell:
                head = i == 0
                color = c["head_color"] if head else (c["label_color"] if j == 0 and ncol > 1 else c["color"])
                L.text(cx + cols[j] / 2, cy, 5, cell, hs if head else fs, color, font, layer=3)
            cx += cols[j]


_STEP_PFX = r"(?:STEP|Step|step|ＳＴＥＰ|ステップ)"


def parse_step(text: str, auto_no: int) -> tuple[int, str]:
    """「STEP 2 穴をあける」→ (2, "穴をあける")。番号がなければ auto_no を使う（「1.2kgの板」は番号と読まない）"""
    t = " ".join(split_lines(text))
    m = re.match(rf"^\s*{_STEP_PFX}\s*([0-9０-９]+)\s*[:：.．、)）]?\s*(.*)$", t)
    if not m:
        m = re.match(r"^\s*([0-9０-９]+)\s*(?:[:：、)）]|\s)\s*(.+)$", t)
    if m:
        return int(unicodedata.normalize("NFKC", m.group(1))), m.group(2).strip()
    return auto_no, t.strip()


def _step(L: Layer, tpl, c, text, w, h, no: int):
    num, body = parse_step(text, no)
    font = c["font"]

    def build(k):
        fs, ns = c["size"] * k, c["number_size"] * k
        sq = ns * 1.75
        tw = text_width(body, fs, tpl, font) + fs * 1.4 if body else 0
        return sq + tw, fs, ns, sq, tw
    bw, fs, ns, sq, tw = _fit(build, w * 0.8)
    x, y = _anchor(c.get("position", "top-left"), bw, sq, w, h, top_extra=c.get("_top", 0))
    r = sq * 0.12
    if body:
        L.shape(rrect(x + sq - r, y + sq * 0.14, tw + r, sq * 0.72, r, "tr br"), c["fill"], 1.0, 0)
        L.text(x + sq + fs * 0.7, y + sq / 2, 4, body, fs, c["color"], font, layer=2)
    L.shape(rrect(x, y, sq, sq, r), c["accent"], 1.0, 1)
    L.text(x + sq / 2, y + sq * 0.27, 5, "STEP", ns * 0.32, c["number_color"], font, layer=2)
    L.text(x + sq / 2, y + sq * 0.63, 5, str(num), ns, c["number_color"], font, layer=2)


def _place(L: Layer, tpl, c, text, w, h):
    lines = split_lines(text) or [text]
    name, sub = lines[0], " ".join(lines[1:])
    font = c["font"]

    def build(k):
        fs, ss = c["size"] * k, c["sub_size"] * k
        icon = fs * 1.1
        tw = max(text_width(name, fs, tpl, font), text_width(sub, ss, tpl, font) if sub else 0)
        return 14 * k + icon + fs * 0.9 + tw + fs * 0.8, fs, ss, icon
    bw, fs, ss, icon = _fit(build, w * 0.7)
    bh = fs * 1.6 + (ss * 1.35 if sub else 0)
    x, y = _anchor(c.get("position", "bottom-left"), bw, bh, w, h, margin_bottom=c.get("margin_bottom", 210),
                   top_extra=c.get("_top", 0))
    bar = fs * 0.22
    L.shape(rrect(x, y, bw, bh, fs * 0.2, "tr br"), c["fill"], c.get("opacity", 0.85), 0)
    L.shape(rrect(x, y, bar, bh, 0), c["accent"], 1.0, 1)
    # 地図のピン（丸と三角）
    pcx, pr = x + bar + fs * 0.45 + icon / 2, icon * 0.36
    pcy = y + (fs * 0.8 if sub else bh / 2) - pr * 0.35
    pin = (circle(pcx, pcy, pr) + f" m {_n(pcx - pr * 0.82)} {_n(pcy + pr * 0.55)} l {_n(pcx)} {_n(pcy + pr * 2.0)}"
           f" l {_n(pcx + pr * 0.82)} {_n(pcy + pr * 0.55)}")
    L.shape(pin, c["accent"], 1.0, 1)
    L.shape(circle(pcx, pcy, pr * 0.42), c["fill"], 1.0, 2)
    tx = x + bar + fs * 0.45 + icon + fs * 0.35
    L.text(tx, y + (fs * 0.8 if sub else bh / 2), 4, name, fs, c["color"], font, layer=2)
    if sub:
        L.text(tx, y + fs * 1.6 + ss * 0.55, 4, sub, ss, c.get("sub_color", "#CFE8D8"), font, bold=False, layer=2)


def card_events(tpl: Template | None, w: int, h: int, telops) -> list[str]:
    """telops: [(開始, 終了, 文字, 種類)]。カード型のテロップの ASS イベントを返す（ほかの種類は無視）"""
    ev = []
    step_no = 0
    pc = progress_config(tpl)
    # 章の進み具合のバー（と章の名前）が画面の上に出るときは、上に置くカードを少し下げる
    top = (pc["height"] + (pc["title_size"] * 1.4 + 8 if pc.get("show_title", True) else 0)
           if pc.get("enabled") and pc.get("position", "top") != "bottom" else 0)
    for s, e, text, style, *rest in sorted(telops, key=lambda t: t[0]):
        if style not in CARD_STYLES:
            continue
        c = card_config(tpl, style)
        c["_top"] = top
        o = rest[0] if rest and isinstance(rest[0], dict) else {}   # 見せ方（rc_direct。映像を見て決めた置き場所・出方）
        if o.get("position"):
            c["position"] = o["position"]
        if o.get("animation"):
            c["animation"] = o["animation"]
        pos = c.get("position", CARD_DEFAULTS[style]["position"])
        anim = c.get("animation", "slide")
        dx, dy = _slide_from(pos) if anim == "slide" else ((0, 30) if anim == "pop" else (0, 0))
        L = Layer(s, e, anim, dx, dy)
        if style == "bubble":
            _bubble(L, tpl, c, text, w, h)
        elif style == "spec":
            _spec(L, tpl, c, text, w, h)
        elif style == "compare":
            _compare(L, tpl, c, text, w, h)
        elif style == "step":
            step_no += 1
            num, _ = parse_step(text, step_no)
            step_no = num
            _step(L, tpl, c, text, w, h, num)
        elif style == "place":
            _place(L, tpl, c, text, w, h)
        ev += L.events
    return ev


# ====================================================================== 章の進み具合
def progress_config(tpl: Template | None) -> dict:
    c = dict(PROGRESS_DEFAULTS)
    c["font"] = (tpl.get("caption", {}) if tpl else {}).get("font", FONT)
    if tpl:
        c.update(tpl.get("progress", {}) or {})
    return c


def progress_events(tpl: Template | None, w: int, h: int, chapters, start: float, end: float,
                    force: bool = False) -> list[str]:
    """画面の上に細いバーを出し、章ごとに区切って今の位置まで色を塗る。今の章の名前も出す。

    chapters: [(完成動画の時刻, 章の名前)] / start〜end: 本編の範囲（オープニング・エンディングは除く）
    """
    c = progress_config(tpl)
    if not (c.get("enabled") or force) or end - start < 2:
        return []
    chs = sorted((float(t), str(title)) for t, title in chapters if start - 0.01 <= float(t) < end - 1.0)
    if not chs or chs[0][0] > start + 0.5:
        chs.insert(0, (start, ""))
    chs[0] = (start, chs[0][1])
    # 近すぎる区切り（1秒未満）はまとめる
    merged = []
    for t, title in chs:
        if merged and t - merged[-1][0] < 1.0:
            continue
        merged.append((t, title))
    chs = merged
    bounds = [t for t, _ in chs] + [end]
    span = end - start
    bh = max(2, int(c.get("height", 8)))
    gap = max(0, int(c.get("gap", 6)))
    top = c.get("position", "top") != "bottom"
    y0 = 0 if top else h - bh

    def X(t):
        return (t - start) / span * w
    segs = []
    for i in range(len(chs)):
        a, b = X(bounds[i]) + (gap / 2 if i else 0), X(bounds[i + 1]) - (gap / 2 if i < len(chs) - 1 else 0)
        if b - a >= 1:
            segs.append(rrect(a, y0, b - a, bh, 0))
    if not segs:
        return []
    allpath = " ".join(segs)
    ev = []
    track = Layer(start, end, "fade", base_layer=6, fade_in=300, fade_out=300)
    track.shape(allpath, c["track_color"], c.get("track_opacity", 0.35), 0)
    ev += track.events
    total = sum(1 for _, t in chs if t)
    n_named = 0
    for i, (t, title) in enumerate(chs):
        s, e = bounds[i], bounds[i + 1]
        dur = max(1, int(round((e - s) * 1000)))
        L = Layer(s, e, "fade", base_layer=6, fade_in=0, fade_out=0)
        # 塗りは本編の頭から「今」まで。章の間に右端を X(s) → X(e) へ動かす
        L.shape(allpath, c["color"], 1.0, 1,
                extra=f"\\clip(0,{_n(y0)},{_n(X(s))},{_n(y0 + bh)})"
                      f"\\t(0,{dur},\\clip(0,{_n(y0)},{_n(X(e))},{_n(y0 + bh)}))")
        ev += L.events
        if title:
            n_named += 1
        if title and c.get("show_title", True):
            label = str(c.get("title_format", "{title}")).replace("{title}", title) \
                .replace("{n}", str(n_named)).replace("{total}", str(total))
            fs = c.get("title_size", 30)
            tw = text_width(label, fs, tpl, c["font"])
            secs = float(c.get("title_seconds", 0) or 0)
            le = min(e, s + secs) if secs > 0 else e
            T = Layer(s, le, "fade", base_layer=6, fade_in=250, fade_out=250)
            ly = y0 + bh + 8 if top else y0 - 8 - fs * 1.4
            lx = w - 16 - (tw + fs) if c.get("title_align", "left") == "right" else 16
            T.shape(rrect(lx, ly, tw + fs * 1.0, fs * 1.4, fs * 0.3), c["title_box"], c.get("title_opacity", 0.55), 2)
            T.text(lx + fs * 0.5, ly + fs * 0.7, 4, label, fs, c["title_color"], c["font"], layer=3)
            ev += T.events
    return ev


# ====================================================================== オープニング・エンディング
def _cfg(tpl: Template, sec: str, defaults: dict) -> dict:
    c = dict(defaults)
    c["font"] = tpl.get("caption", {}).get("font", FONT)
    c.update(tpl.get(sec, {}) or {})
    return c


def _ass_doc(w: int, h: int, font: str, events: list[str]) -> str:
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Card,{font},48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, ""])


def _run(cmd: list[str], cwd: Path, what: str) -> None:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-12:])
        raise RuntimeError(f"FFmpeg（{what}）が失敗しました:\n{tail}")


def _hex_ff(hex_rgb: str) -> str:
    h = str(hex_rgb).lstrip("#")
    return "0x" + (h if len(h) == 6 else "101820")


def _file(tpl: Template, rel: str) -> Path | None:
    return tpl.path(rel) if rel else None


def _background_inputs(tpl: Template, c: dict, w: int, h: int, d: float, fps: int) -> tuple[list[str], str]:
    """背景（画像があれば画像、なければ単色）の入力と、フィルターの最初の部分"""
    img = _file(tpl, c.get("background_image", ""))
    if img:
        return (["-loop", "1", "-framerate", str(fps), "-t", f"{d:.3f}", "-i", str(img)],
                f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p")
    return (["-f", "lavfi", "-i", f"color=c={_hex_ff(c['background'])}:s={w}x{h}:r={fps}:d={d:.3f}"],
            "[0:v]format=yuv420p")


def _audio_input(path: Path | None, d: float, loop: bool) -> list[str]:
    if path:
        return (["-stream_loop", "-1"] if loop else []) + ["-i", str(path)]
    return ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]


def _prepare_work(tpl: Template, work: Path) -> str:
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    if tpl.fonts_dir:
        shutil.copytree(tpl.fonts_dir, work / "fonts")
        return ":fontsdir=fonts"
    return ""


def opening_events(tpl: Template, c: dict, w: int, h: int, has_logo: bool) -> list[str]:
    d = float(c["duration"])
    k = h / 1080
    font = c["font"]
    name = c.get("channel_name") or ""
    acc = c.get("accent_color", "#FFE14D")
    ev = []
    ny = h * (0.70 if has_logo else 0.47)
    # 中央から左右に伸びる線
    lw = w * 0.42
    line = Layer(0.25, d, "fade", base_layer=1, fade_in=0, fade_out=0)
    line.shape(rrect(w / 2 - lw / 2, ny + 78 * k, lw, 6 * k, 3 * k), acc, 1.0, 0,
               extra=f"\\clip({_n(w / 2)},0,{_n(w / 2)},{h})\\t(0,500,0.6,\\clip({_n(w / 2 - lw / 2)},0,"
                     f"{_n(w / 2 + lw / 2)},{h}))")
    ev += line.events
    # 背景の飾り（ゆっくり動く大きな丸）
    deco = Layer(0, d, "fade", base_layer=0, fade_in=400, fade_out=0)
    deco.shape(ring(w * 0.16, h * 0.2, 260 * k, 26 * k), acc, 0.12, 0,
               anchor=f"\\move(0,0,{_n(40 * k)},{_n(30 * k)})")
    deco.shape(ring(w * 0.86, h * 0.84, 340 * k, 30 * k), acc, 0.10, 0,
               anchor=f"\\move(0,0,{_n(-40 * k)},{_n(-24 * k)})")
    ev += deco.events
    if name:
        T = Layer(0.55, d, "slide", 0, 40 * k, base_layer=3, fade_in=300, fade_out=0)
        T.text(w / 2, ny, 5, name, c["size"] * k, c.get("color", "#FFFFFF"), font,
               extra="\\fsp24\\t(0,600,0.5,\\fsp0)")
        ev += T.events
    if c.get("tagline"):
        T2 = Layer(1.0, d, "fade", base_layer=3, fade_in=400, fade_out=0)
        T2.text(w / 2, ny + 140 * k, 5, c["tagline"], c["tagline_size"] * k, acc, font, bold=False)
        ev += T2.events
    return ev


def make_opening(tpl: Template, out: Path, fps: int = 60) -> Path:
    """オープニング動画（ロゴとチャンネル名のアニメーション）を作る"""
    c = _cfg(tpl, "opening", OPENING_DEFAULTS)
    w, h = tpl["video"]["width"], tpl["video"]["height"]
    d = max(1.0, float(c["duration"]))
    c["duration"] = d
    out = Path(out).resolve()
    work = out.parent / "_work_opening"
    fontsdir = _prepare_work(tpl, work)
    logo = _file(tpl, c.get("logo") or tpl.get("logo", {}).get("file", ""))
    if not c.get("channel_name") and not logo:
        c["channel_name"] = "チャンネル名"
    (work / "op.ass").write_text(_ass_doc(w, h, c["font"], opening_events(tpl, c, w, h, bool(logo))),
                                 encoding="utf-8")
    bg_in, chain = _background_inputs(tpl, c, w, h, d, fps)
    cmd = ["ffmpeg", "-y", "-v", "error", *bg_in]
    parts = []
    idx = 1
    cur = "[bg]"
    parts.append(chain + cur)
    if logo:
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{d:.3f}", "-i", str(logo)]
        lw = int(min(w * 0.3, h * 0.42))
        # 0.15秒から、小さい所から少し大きくなって戻る（ポンッと出る）
        sc = ("if(lt(t,0.15),0.01,if(lt(t,0.55),0.6+0.5*(t-0.15)/0.4,"
              "if(lt(t,0.75),1.1-0.1*(t-0.55)/0.2,1)))")
        parts.append(f"[{idx}:v]format=rgba,scale=w='max(2,trunc({lw}*{sc}/2)*2)':h=-2:eval=frame,"
                     f"fade=t=in:st=0.15:d=0.35:alpha=1[lg]")
        parts.append(f"{cur}[lg]overlay=x='(W-w)/2':y='{int(h * 0.40)}-h/2':eval=frame:shortest=1[v1]")
        cur = "[v1]"
        idx += 1
    parts.append(f"{cur}ass=op.ass{fontsdir},fade=t=out:st={d - 0.35:.3f}:d=0.35,format=yuv420p[v]")
    snd = _file(tpl, c.get("sound", ""))
    cmd += _audio_input(snd, d, False)
    parts.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=end={d:.3f},"
                 f"afade=t=out:st={max(0, d - 0.5):.3f}:d=0.5[a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", f"{d:.3f}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", str(out)]
    _run(cmd, work, "オープニング")
    shutil.rmtree(work, ignore_errors=True)
    return out


def ending_layout(w: int, h: int, n_boxes: int) -> dict:
    """終了画面の要素の枠（画面の点の位置）。YouTube Studio でこの枠に合わせて要素を置く"""
    sx, sy = w / 1920, h / 1080
    bw, bh, sd = END_BOX_W * sx, END_BOX_H * sx, END_SUB_D * sx
    top = 380 * sy
    n = max(0, min(int(n_boxes), 2))
    boxes = []
    if n == 2:
        boxes = [(150 * sx, top), (w - 150 * sx - bw, top)]
        sub = (w / 2, top + bh / 2)
    elif n == 1:
        boxes = [(w / 2 + 40 * sx, top)]
        sub = (w / 2 - 40 * sx - 330 * sx, top + bh / 2)
    else:
        sub = (w / 2, top + bh / 2)
    return {"boxes": [(x, y, bw, bh) for x, y in boxes], "subscribe": (sub[0], sub[1], sd / 2)}


def ending_events(tpl: Template, c: dict, w: int, h: int) -> list[str]:
    d = float(c["duration"])
    k = h / 1080
    font = c["font"]
    acc = c.get("accent_color", "#FFE14D")
    col = c.get("color", "#FFFFFF")
    lay = ending_layout(w, h, c.get("video_boxes", 2))
    ev = []
    M = Layer(0.2, d, "pop", 0, 30 * k, base_layer=3, fade_in=200, fade_out=0)
    M.text(w / 2, 150 * k, 5, c.get("message", ""), c["size"] * k, col, font,
           extra="\\fscx70\\fscy70\\t(0,180,\\fscx106\\fscy106)\\t(180,280,\\fscx100\\fscy100)")
    if c.get("sub_message"):
        M.text(w / 2, 250 * k, 5, c["sub_message"], c["sub_size"] * k, acc, font, bold=False, s=0.6)
    ev += M.events
    for i, (x, y, bw, bh) in enumerate(lay["boxes"]):
        B = Layer(0.5 + 0.15 * i, d, "slide", (-80 if i == 0 and len(lay["boxes"]) == 2 else 80) * k, 0,
                  base_layer=1, fade_in=250, fade_out=0)
        B.shape(rrect(x, y, bw, bh, 14 * k), "#000000", 0.45, 0, border=5 * k, border_color=acc,
                border_opacity=1.0)
        B.text(x + bw / 2, y + bh / 2, 5, "▶", 70 * k, col, font, layer=1, extra="\\alpha&H90&")
        B.text(x + bw / 2, y + bh + 44 * k, 5, "おすすめの動画" if i == 0 else "こちらもどうぞ", 38 * k, col, font,
               layer=1)
        ev += B.events
    sx, sy, sr = lay["subscribe"]
    S = Layer(0.8, d, "pop", 0, 20 * k, base_layer=1, fade_in=200, fade_out=0)
    S.shape(circle(sx, sy, sr), "#000000", 0.45, 0)
    S.shape(ring(sx, sy, sr, 8 * k), acc, 1.0, 1)
    S.text(sx, sy, 5, "＋", 90 * k, col, font, layer=2, extra="\\alpha&H90&")
    label_y = (lay["boxes"][0][1] + lay["boxes"][0][3] if lay["boxes"] else sy + sr) + 44 * k
    S.text(sx, max(label_y, sy + sr + 44 * k), 5, "チャンネル登録", 38 * k, col, font, layer=2)
    ev += S.events
    # 登録の丸のまわりに広がる輪（2秒ごと）
    t = 1.6
    while t < d - 1.0:
        P = Layer(t, min(t + 1.2, d), "fade", base_layer=0, fade_in=0, fade_out=600)
        P.shape(ring(0, 0, sr + 6 * k, 6 * k), acc, 0.8, 0,
                extra="\\fscx100\\fscy100\\t(0,1200,\\fscx135\\fscy135)", anchor=f"\\pos({_n(sx)},{_n(sy)})")
        ev += P.events
        t += 2.0
    if c.get("channel_name"):
        N = Layer(1.0, d, "fade", base_layer=3)
        N.text(w / 2, h - 70 * k, 5, c["channel_name"], 40 * k, col, font, bold=False)
        ev += N.events
    return ev


def make_ending(tpl: Template, out: Path, fps: int = 60) -> Path:
    """エンディング動画（チャンネル登録の呼びかけと、YouTube の終了画面の枠）を作る"""
    c = _cfg(tpl, "ending", ENDING_DEFAULTS)
    w, h = tpl["video"]["width"], tpl["video"]["height"]
    d = min(max(5.0, float(c["duration"])), 20.0)   # YouTube の終了画面は最後の5〜20秒
    c["duration"] = d
    out = Path(out).resolve()
    work = out.parent / "_work_ending"
    fontsdir = _prepare_work(tpl, work)
    (work / "ed.ass").write_text(_ass_doc(w, h, c["font"], ending_events(tpl, c, w, h)), encoding="utf-8")
    bg_in, chain = _background_inputs(tpl, c, w, h, d, fps)
    cmd = ["ffmpeg", "-y", "-v", "error", *bg_in]
    music = _file(tpl, c.get("music", ""))
    cmd += _audio_input(music, d, True)
    vol = float(c.get("music_volume_db", -12)) if music else 0
    parts = [f"{chain},ass=ed.ass{fontsdir},fade=t=in:d=0.3,fade=t=out:st={d - 0.5:.3f}:d=0.5,format=yuv420p[v]",
             f"[1:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol}dB,apad,atrim=end={d:.3f},"
             f"afade=t=in:d=0.5,afade=t=out:st={max(0, d - 2.5):.3f}:d=2.5[a]"]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-t", f"{d:.3f}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", str(out)]
    _run(cmd, work, "エンディング")
    shutil.rmtree(work, ignore_errors=True)
    lay = ending_layout(w, h, c.get("video_boxes", 2))
    info = {"説明": "YouTube Studio の「終了画面」で、要素をこの枠に合わせて置いてください（%は画面の幅・高さに対する位置）",
            "動画の長さ": d,
            "動画の枠": [{"左": f"{x / w * 100:.1f}%", "上": f"{y / h * 100:.1f}%", "幅": f"{bw / w * 100:.1f}%"}
                       for x, y, bw, _bh in lay["boxes"]],
            "チャンネル登録": {"中心の左から": f"{lay['subscribe'][0] / w * 100:.1f}%",
                         "中心の上から": f"{lay['subscribe'][1] / h * 100:.1f}%"}}
    out.with_name(out.stem + "_終了画面の枠.json").write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
    return out


# ---------------------------------------------------------------- 自動で作って使う
def _signature(tpl: Template, sec: str) -> str:
    c = dict(tpl.get(sec, {}) or {})
    files = []
    for key in ("logo", "background_image", "sound", "music"):
        if c.get(key):
            files.append(c[key])
    if sec == "opening" and not c.get("logo"):
        files.append(tpl.get("logo", {}).get("file", ""))
        c["_logo"] = files[-1]
    stamps = []
    for rel in files:
        p = (tpl.dir / rel) if rel else None
        if p and p.exists():
            st = p.stat()
            stamps.append([rel, st.st_size, int(st.st_mtime)])
    fonts = sorted(p.name for p in tpl.fonts_dir.iterdir()) if tpl.fonts_dir else []
    blob = json.dumps({"v": _GEN_VERSION, "c": c, "f": stamps, "video": [tpl["video"]["width"], tpl["video"]["height"]],
                       "font": tpl.get("caption", {}).get("font", FONT), "fonts": fonts},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def ensure(tpl: Template, kind: str, force: bool = False) -> Path:
    """テンプレートの generated フォルダに intro.mp4 / outro.mp4 を作る（設定が変わっていなければ作り直さない）"""
    sec = "opening" if kind == "intro" else "ending"
    gen = tpl.dir / GEN_DIR
    gen.mkdir(exist_ok=True)
    out = gen / f"{kind}.mp4"
    stamp = gen / f"{kind}.sig"
    sig = _signature(tpl, sec)
    if not force and out.exists() and stamp.exists() and stamp.read_text(encoding="utf-8").strip() == sig:
        return out
    print(f"    {'オープニング' if kind == 'intro' else 'エンディング'}を作成中…", flush=True)
    (make_opening if kind == "intro" else make_ending)(tpl, out)
    stamp.write_text(sig, encoding="utf-8")
    return out


def auto_intro_outro(tpl: Template, intro: Path | None, outro: Path | None) -> tuple[Path | None, Path | None]:
    """[intro] / [outro] の file が空で、[opening] / [ending] の enabled = true なら、自動で作ったものを使う"""
    for kind, sec in (("intro", "opening"), ("outro", "ending")):
        cur = intro if kind == "intro" else outro
        if cur or not (tpl.get(sec, {}) or {}).get("enabled", False):
            continue
        try:
            p = ensure(tpl, kind)
        except (Exception, SystemExit) as e:  # noqa: BLE001  作れなくても本編の書き出しは続ける
            print(f"  [注意] {'オープニング' if kind == 'intro' else 'エンディング'}を自動で作れませんでした: {e}",
                  file=sys.stderr)
            continue
        if kind == "intro":
            intro = p
        else:
            outro = p
    return intro, outro


def still(video: Path, t: float, out: Path) -> None:
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(out)],
         Path(video).parent, "静止画")


SAMPLE_TELOPS = [
    (0.2, 3.8, "マキタ DF033D／重さ：1.2kg／電圧：10.8V／価格：9,800円", "spec"),
    (4.0, 7.8, "｜A社｜B社／重さ｜1.2kg｜1.5kg／価格｜9,800円｜7,500円／音｜静か｜うるさい", "compare"),
    (8.0, 11.8, "STEP 2 穴をあける", "step"),
    (8.2, 11.8, "高尾山 山頂／標高599m・東京都八王子市", "place"),
    (12.0, 15.8, "なにこれ！？／軽すぎる…", "bubble"),
]



# ============================================================
#  画面から「使う／使わない」を切り替える（template.toml の enabled を書き換える）
# ============================================================
TOGGLES = {"opening": "完成動画の最初にオープニングを付ける", "ending": "完成動画の最後にエンディングを付ける",
           "progress": "章の進み具合（画面の上のバー）を出す"}


def get_enabled(tdir: Path, section: str) -> bool:
    try:
        return bool((Template(Path(tdir)).get(section, {}) or {}).get("enabled", False))
    except Exception:  # noqa: BLE001
        return False


def set_enabled(tdir: Path, section: str, on: bool) -> bool:
    """template.toml の [section] の enabled を true / false にする（ほかの行・コメントはそのまま）。

    その節に enabled の行がなければ、節の見出しのすぐ下に足す。節がなければ、ファイルの末尾に足す。
    """
    p = Path(tdir) / "template.toml"
    text = p.read_text(encoding="utf-8-sig") if p.exists() else ""
    lines = text.splitlines(keepends=True)
    val = "true" if on else "false"
    head = re.compile(r"^\s*\[\s*([^\]\s]+)\s*\]")
    start = next((i for i, ln in enumerate(lines) if (m := head.match(ln)) and m.group(1) == section), None)
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n[{section}]\nenabled = {val}\n"
    else:
        end = next((i for i in range(start + 1, len(lines)) if head.match(lines[i])), len(lines))
        for i in range(start + 1, end):
            m = re.match(r"^(\s*enabled\s*=\s*)(\S+)(.*)$", lines[i].rstrip("\r\n"))
            if m:
                nl = lines[i][len(lines[i].rstrip("\r\n")):] or "\n"
                lines[i] = f"{m.group(1)}{val}{m.group(3)}{nl}"
                break
        else:
            lines.insert(start + 1, f"enabled = {val}\n")
        text = "".join(lines)
    p.write_text(text, encoding="utf-8")
    return get_enabled(tdir, section) == on
