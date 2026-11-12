"""字幕・テロップ・章カードを ASS 字幕ファイル（アニメーション付き）として作る。"""
from __future__ import annotations

import rc_graphics
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


# ---------------------------------------------------------------- テロップの見せ方（rc_direct が決めたもの）
SIDE_LINE_CHARS = 9   # 画面の端に置くテロップの1行の文字数の目安（長ければ2行にする）
_BREAK_AFTER = "、。！？!?・ 　のはがをにでともへや→：:"


def _kind(ch: str) -> str:
    """文字の種類（2行に分ける所を選ぶため）"""
    if ch.isascii() and (ch.isalnum() or ch in ".,%"):
        return "alnum"
    if "ァ" <= ch <= "ヿ":
        return "kata"
    if "ぁ" <= ch <= "ゟ":
        return "hira"
    return "kanji" if ch >= "一" else "other"


def _tag_color(hex_rgb: str, n: int = 1) -> str:
    h = str(hex_rgb).lstrip("#").upper()
    return f"\\{n}c&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _accent(c: dict, tel: dict) -> str:
    """強調する言葉の色。文字の色と同じになるなら、別の色にする"""
    acc = str(c.get("emphasis_color") or tel.get("emphasis_color") or "#FFE14D")
    if acc.lstrip("#").upper() == str(c.get("color", "#FFFFFF")).lstrip("#").upper():
        acc = str(tel.get("emphasis_color2") or "#FF7A59")
    return acc


def split_two(text: str, emphasis: str = "", limit: int = SIDE_LINE_CHARS) -> list[str]:
    """長いテロップを、自然な所で2行にする（強調する言葉の途中では切らない）"""
    if len(text) <= limit or "\n" in text:
        return text.split("\n")
    k0 = text.find(emphasis) if emphasis else -1
    inside = range(k0 + 1, k0 + len(emphasis)) if k0 >= 0 else range(0)
    mid, best = len(text) / 2, None
    for k in range(2, len(text) - 1):
        if k in inside:
            continue
        # 助詞・句読点のあとで切るのを強く優先する（言葉の途中で切らない）。数字の途中では切らない
        ca, cb = _kind(text[k - 1]), _kind(text[k])
        if ca == cb and ca in ("kata", "alnum"):   # カタカナ語・英数字のまとまりの途中
            continue
        if text[k - 1].isdigit() and text[k] in ".,":
            continue
        score = abs(k - mid) - (5 if text[k - 1] in _BREAK_AFTER else 2 if ca != cb else 0)
        if best is None or score < best[0]:
            best = (score, k)
    return [text[:best[1]], text[best[1]:]] if best else [text]


def telop_place(position: str, w: int, h: int, c: dict, cap: dict) -> tuple[int, float, float]:
    """置く場所 → (ASS の配置番号, x, y)。bottom-* は字幕（2行分）のすぐ上"""
    mv, mh = float(c.get("margin_v", 70)), float(c.get("margin_h", 70))
    above_cap = h - float(cap.get("margin_bottom", 56)) - float(cap.get("size", 66)) * 2.6
    return {
        "top": (8, w / 2, mv), "top-left": (7, mh, mv), "top-right": (9, w - mh, mv),
        "left": (4, mh, h * 0.44), "right": (6, w - mh, h * 0.44),
        "bottom-left": (1, mh, above_cap), "bottom-right": (3, w - mh, above_cap),
        "center": (5, w / 2, h * 0.45), "middle": (5, w / 2, h * 0.45), "bottom": (2, w / 2, h - mv),
    }.get(position, (8, w / 2, mv))


def _seg_anim(kind: str, sc: float) -> str:
    """文字のかたまり1つ分の出方（sc = できあがりの大きさ%）。行全体にかかるもの（\\fad・\\move）は telop_events が付ける"""
    s = f"\\fscx{sc:.0f}\\fscy{sc:.0f}"
    if kind == "pop":
        return (f"\\fscx{sc * 0.4:.0f}\\fscy{sc * 0.4:.0f}\\t(0,110,\\fscx{sc * 1.12:.0f}\\fscy{sc * 1.12:.0f})"
                f"\\t(110,190,{s})")
    if kind == "zoom":
        return f"\\fscx{sc * 1.9:.0f}\\fscy{sc * 1.9:.0f}\\t(0,170,{s})"
    if kind == "slide":
        return s + "\\fsp-8\\t(0,220,\\fsp0)"
    if kind == "drop":
        return s + f"\\t(180,250,\\fscy{sc * 0.9:.0f})\\t(250,330,\\fscy{sc:.0f})"
    if kind == "shake":
        return s + "\\frz-4\\t(0,60,\\frz4)\\t(60,120,\\frz-3)\\t(120,180,\\frz3)\\t(180,240,\\frz0)"
    return s


def telop_events(s: float, e: float, text: str, style: str, opts: dict, c: dict, tel: dict, cap: dict,
                 w: int, h: int) -> list[str]:
    """見せ方（置く場所・強調する言葉・大きさ・出方）つきのテロップ。二重の縁取りがあれば、後ろにもう1枚描く"""
    import rc_direct
    pos = opts.get("position") or {"middle": "center"}.get(c.get("position", "top"), c.get("position", "top"))
    an, x, y = telop_place(pos, w, h, c, cap)
    anim = opts.get("animation") or c.get("animation", "pop")
    base = 100.0 * rc_direct.SIZES.get(opts.get("size", "normal"), 1.0)
    em = opts.get("emphasis") or ""
    narrow = pos not in ("top", "center", "middle", "bottom")
    lines = split_two(text, em, SIDE_LINE_CHARS if narrow else 18)
    line_tags = f"\\an{an}"
    if anim == "drop":
        line_tags += f"\\move({x:.0f},{y - 70:.0f},{x:.0f},{y:.0f},0,180)\\fad(100,160)"
    elif anim == "slide":
        dx = -70 if an in (1, 4, 7) else 70 if an in (3, 6, 9) else 0
        line_tags += (f"\\move({x + dx:.0f},{y:.0f},{x:.0f},{y:.0f},0,220)" if dx else f"\\pos({x:.0f},{y:.0f})") \
            + "\\fad(180,180)"
    else:
        line_tags += f"\\pos({x:.0f},{y:.0f})" + {"fade": "\\fad(220,220)", "zoom": "\\fad(60,160)"}.get(anim, "\\fad(0,160)")
    box = "box_color" in c
    col, acc = str(c.get("color", "#FFFFFF")), _accent(c, tel)

    def body(front: bool, back_col: str = "") -> str:
        out = []
        for ln in lines:
            k = ln.find(em) if em else -1
            parts = [(ln, False)] if k < 0 else [(ln[:k], False), (em, True), (ln[k + len(em):], False)]
            row = ""
            for seg, hot in parts:
                if not seg:
                    continue
                # 2行のときは、大きくしすぎると上下の行に重なるので控えめにする
                sc = base * ((rc_direct.EMPH_SCALE if len(lines) == 1 else rc_direct.EMPH_SCALE_2LINES) if hot else 1.0)
                color = _tag_color(acc if hot else col) if front else _tag_color(back_col)
                row += "{" + color + _seg_anim(anim, sc) + "}" + _esc(seg)
            out.append(row)
        return "\\N".join(out)

    ev = []
    o2 = float(c.get("outline2", 0) or 0)
    if o2 > 0 and not box:   # 二重の縁取り: 太い外側の縁だけを、後ろに描く
        oc = str(c.get("outline2_color", "#FFFFFF"))
        ev.append(f"Dialogue: 1,{_t(s)},{_t(e)},T_{style},,0,0,0,,{{{line_tags}\\bord{float(c.get('outline', 4)) + o2:.0f}"
                  f"{_tag_color(oc, 3)}\\shad{float(c.get('shadow2', 4)):.0f}}}" + body(False, oc))
    ev.append(f"Dialogue: 2,{_t(s)},{_t(e)},T_{style},,0,0,0,,{{{line_tags}}}" + body(True))
    return ev


def color_keywords(text: str, keywords, key_color: str, base_color: str) -> str:
    """字幕の中のキーワードに色を付ける（ASS のタグ入りの文字にして返す）"""
    spans = []
    for kw in sorted({str(k) for k in keywords or [] if str(k).strip()}, key=len, reverse=True):
        start = 0
        while (k := text.find(kw, start)) >= 0:
            if all(k >= b or k + len(kw) <= a for a, b in spans):
                spans.append((k, k + len(kw)))
            start = k + len(kw)
    if not spans:
        return _esc(text)
    out, cur = "", 0
    for a, b in sorted(spans):
        out += _esc(text[cur:a]) + "{" + _tag_color(key_color) + "}" + _esc(text[a:b]) + "{" + _tag_color(base_color) + "}"
        cur = b
    return out + _esc(text[cur:])


def build_ass(tpl: Template, w: int, h: int, captions, telops, chapters, progress=None,
              progress_force: bool = False, keywords=()) -> str:
    """captions: [(s, e, text)] / telops: [(s, e, text, style)] / chapters: [(t, title)] すべて完成動画の時刻

    telops の5つ目に見せ方（rc_direct.telop_opts の dict）があれば、置く場所・強調する言葉・大きさ・出方をそのとおりにする。
    keywords: 字幕の中で色を付ける言葉。
    progress: (全部の章 [(t, title)], 本編の開始, 本編の終了)。テンプレートの [progress] が有効なら、章の進み具合を出す
    """
    cap = tpl["caption"]
    tel = tpl.get("telop", {})
    chap = tpl.get("chapter_card", {})
    styles = [_style("Caption", cap, 2, cap.get("margin_bottom", 56))]
    for name in ("emphasis", "info", "warning", "funny"):
        c = tel.get(name, {})
        box = "box_color" in c
        # margin_v / margin_h はテンプレートに書いたときだけ使う（書いていなければ今までどおり 70）
        styles.append(_style(f"T_{name}", c, POS.get(c.get("position", "top"), 8), c.get("margin_v", 70),
                             c.get("margin_h", 70), box=box))
    styles.append(_style("Chapter", chap, 5, 0, box=True))
    styles.append(rc_graphics.card_style_line(tpl))   # 吹き出し・スペック表などのカード型テロップ用

    ev = []
    key_color = str(cap.get("keyword_color", "#FFE14D") or "")
    for s, e, text, *spk in captions:   # 4つ目があれば、その字幕だけの色（話者ごとの色分け）
        base = (spk[0] or {}).get("color") if spk and spk[0] else None
        body = color_keywords(text, keywords, key_color, base or cap.get("color", "#FFFFFF")) \
            if keywords and key_color else _esc(text)
        ev.append(f"Dialogue: 0,{_t(s)},{_t(e)},Caption,,0,0,0,,{_color_tag(spk[0]) if spk else ''}{body}")
    for s, e, text, style, *rest in telops:
        if style in rc_graphics.CARD_STYLES:
            continue   # カード型は下で図形として描く
        c = tel.get(style, {})
        if rest and isinstance(rest[0], dict):   # 見せ方つき（映像を見て決めた位置・強調・出方）
            ev += telop_events(s, e, text, style, rest[0], c, tel, cap, w, h)
            continue
        ev.append(f"Dialogue: 2,{_t(s)},{_t(e)},T_{style},,0,0,0,,"
                  f"{_anim(c.get('animation', 'pop'), int((e - s) * 1000))}{_esc(text)}")
    d = chap.get("duration", 2.5)
    for t, title in chapters:
        ev.append(f"Dialogue: 3,{_t(t)},{_t(t + d)},Chapter,,0,0,0,,"
                  r"{\fad(250,300)\fscx92\fscy92\t(0,250,\fscx100\fscy100)}" + _esc(title))
    ev += rc_graphics.card_events(tpl, w, h, telops)
    if progress:
        ev += rc_graphics.progress_events(tpl, w, h, *progress, force=progress_force)

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
