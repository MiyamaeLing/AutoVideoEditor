"""字幕・テロップ・章カードを ASS 字幕ファイル（アニメーション付き）として作る。"""
from __future__ import annotations

from rc_template import Template, ass_color

POS = {"top": 8, "middle": 5, "top-left": 7, "bottom": 2}


def _t(sec: float) -> str:
    cs = int(round(max(sec, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def _style(name, c: dict, align: int, margin_v: int, margin_lr: int = 60, box: bool = False) -> str:
    primary = ass_color(c.get("color", "#FFFFFF"))
    if box:
        # BorderStyle=3 は文字の後ろに箱を描く（箱の色は OutlineColour）
        outline_col = ass_color(c.get("box_color", "#000000"), 0x20)
        border, outline = 3, 18
    else:
        outline_col = ass_color(c.get("outline_color", "#000000"))
        border, outline = 1, c.get("outline", 4)
    bold = -1 if c.get("bold", True) else 0
    return (f"Style: {name},{c.get('font', 'BIZ UDPGothic')},{c.get('size', 64)},{primary},{primary},"
            f"{outline_col},&H80000000,{bold},0,0,0,100,100,0,0,{border},{outline},{c.get('shadow', 0)},"
            f"{align},{margin_lr},{margin_lr},{margin_v},1")


def _color_tag(c: dict | None) -> str:
    """{"color": "#RRGGBB", "outline_color": "#RRGGBB"} → その字幕だけ色を変える ASS のタグ"""
    if not c:
        return ""
    tag = ""
    for key, n in (("color", 1), ("outline_color", 3)):
        if c.get(key):
            h = c[key].lstrip("#").upper()
            tag += f"\\{n}c&H{h[4:6]}{h[2:4]}{h[0:2]}&"
    return "{" + tag + "}" if tag else ""


def _anim(kind: str, dur_ms: int) -> str:
    if kind == "pop":
        return r"{\fscx40\fscy40\t(0,110,\fscx112\fscy112)\t(110,190,\fscx100\fscy100)\fad(0,160)}"
    if kind == "slide":
        return r"{\fad(180,180)\fsp-8\t(0,220,\fsp0)}"
    return r"{\fad(220,220)}"


def build_ass(tpl: Template, w: int, h: int, captions, telops, chapters) -> str:
    """captions: [(s, e, text)] / telops: [(s, e, text, style)] / chapters: [(t, title)] すべて完成動画の時刻"""
    cap = tpl["caption"]
    tel = tpl.get("telop", {})
    chap = tpl.get("chapter_card", {})
    styles = [_style("Caption", cap, 2, cap.get("margin_bottom", 56))]
    for name in ("emphasis", "info", "warning", "funny"):
        c = tel.get(name, {})
        box = "box_color" in c
        styles.append(_style(f"T_{name}", c, POS.get(c.get("position", "top"), 8), 70, 70, box=box))
    styles.append(_style("Chapter", chap, 5, 0, box=True))

    ev = []
    for s, e, text, *spk in captions:   # 4つ目があれば、その字幕だけの色（話者ごとの色分け）
        ev.append(f"Dialogue: 0,{_t(s)},{_t(e)},Caption,,0,0,0,,{_color_tag(spk[0]) if spk else ''}{_esc(text)}")
    for s, e, text, style in telops:
        c = tel.get(style, {})
        ev.append(f"Dialogue: 2,{_t(s)},{_t(e)},T_{style},,0,0,0,,"
                  f"{_anim(c.get('animation', 'pop'), int((e - s) * 1000))}{_esc(text)}")
    d = chap.get("duration", 2.5)
    for t, title in chapters:
        ev.append(f"Dialogue: 3,{_t(t)},{_t(t + d)},Chapter,,0,0,0,,"
                  r"{\fad(250,300)\fscx92\fscy92\t(0,250,\fscx100\fscy100)}" + _esc(title))

    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
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
