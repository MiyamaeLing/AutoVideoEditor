"""サムネイルの凝った表現のテスト（rc_thumbart.py と rc_thumb.compose / generate）。

- 強調する言葉: 最初の1か所だけ大きく・別の色に。行の中でベースラインがそろう
- 縦書き: 右の列から。「ー」は横に倒す・小さい字と句読点は右上・「30g」は横に並べて1字分
- バッジ: 上品=縦長の落款 / ポップ=丸いシール / シンプル=横長の札。文字と大事な所を避けて置く
- 主役の切り抜き: はっきりした物は切り抜ける。何もない・色の差が小さいときは切り抜かない。毎回同じ結果
- 効果: focus=まわりを暗く / sticker=白いふち（切り抜けないときは focus）/ depth=主役を文字の手前に（隠れすぎるなら回さない）
  まわりを暗くしすぎない（文字の下のグラデーション・四隅も控えめに）
- AIにデザインも任せる: 答えの形・指示・考える深さ（動画とは別）・おかしな答えを直す・設定でやめられる
- 右下の再生時間の表示（YouTube の「12:34」）に、文字・見出し・バッジを置かない
- 隠してはいけない所: 主役の枠の中の物（ペン・表示など）・AIが囲んだ枠。文字は物に触れない所へ（小さくする・写真の寄せ方を変える）。
  物を大きく隠すデザイン（ペンを覆う帯など）は、AIに任せたとき別のデザインに替える。帯の文字は見た目で真ん中に
- 行の間: 大きくした強調の言葉の g・y などが、次の行の字に重ならない
- 影: 文字のある側（下半分・右半分など）を端までムラなく。濃さは写真の明るさとAIが決めた強さ（明るく・ふつう・くっきり）で
- AIへの指示: 前回の案の画像（画面と同じ並び）・内容・これまでの指示をAIに見せて作り直す。指示は積み重なり、指示なしで戻る
- 場面: 候補は見どころなど8つまで＋動画の最後まで均等に。選んだ場面（完成動画の時刻）は必ず案1から使う。
  別の場面から選び直す（今の場面を外し、取る位置もずらす）・場面を増やす・指示のときは同じ候補を使い回す
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from itertools import product
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_thumbstyle import FakeTpl  # noqa: E402

SUB = (360, 150, 920, 570)   # 大事な所の枠（物のまわりに少し余裕をもたせる）


def _photo(obj=(150, 40, 30), bg=(200, 196, 188)):
    """紙のような背景（少しざらつき）に、角の丸い赤い物"""
    from PIL import Image, ImageDraw, ImageFilter
    rng = np.random.default_rng(1)
    a = np.clip(np.array(bg, float) + rng.normal(0, 6, (720, 1280, 1)), 0, 255).astype(np.uint8)
    img = Image.fromarray(a)
    if obj:
        ImageDraw.Draw(img).rounded_rectangle([420, 200, 860, 520], radius=40, fill=obj)
    return img.filter(ImageFilter.GaussianBlur(1))


def _st(style):
    import rc_thumb as T
    tpl = FakeTpl({"style": style})
    return T.text_style(tpl), T.find_font(tpl)


# ---------------------------------------------------------------- デザインの値
def t_values(check):
    import rc_thumb as T
    import rc_thumbart as A
    d = A.clean_design({"emphasis": "30g", "badge": "検 証", "effect": "focus"}, "30gの重さは\\n正義か", "野原工芸")
    check("デザイン: AIの答えをそのまま使う（バッジの空白は詰める）", d == {"emphasis": "30g", "badge": "検証", "effect": "focus", "shade": "normal"}, str(d))
    bad = A.clean_design({"emphasis": "40g", "badge": "とても長いバッジの言葉", "effect": "explode", "shade": "dark"}, "30gの重さ", "")
    check("デザイン: 文字に無い強調・長すぎるバッジ・知らない効果・影の強さは使わない",
          bad == {"emphasis": "", "badge": "", "effect": "none", "shade": "normal"}, str(bad))
    same = A.clean_design({"emphasis": "30gの重さ", "badge": "野原工芸"}, "30gの重さ", "野原 工芸")
    check("デザイン: 文字全部の強調・見出しと同じバッジは使わない", same["emphasis"] == "" and same["badge"] == "", str(same))
    check("デザイン: 答えが壊れていても使える形にする", A.clean_design(None, "テスト") == {"emphasis": "", "badge": "", "effect": "none", "shade": "normal"})
    autos = {s: A.auto_emphasis(s) for s in ("30gの重さは\n正義か", "1.2kgの軽さ", "年2回だけの受注生産", "軽すぎて笑った", "2024")}
    check("デザイン: AIなしでは、数字（単位つき）を強調", autos == {"30gの重さは\n正義か": "30g", "1.2kgの軽さ": "1.2kg",
                                                        "年2回だけの受注生産": "2回", "軽すぎて笑った": "", "2024": ""}, str(autos))
    lays = A.pick_layouts(["vertical", "なにか", "vertical", "band"], ["bottom", "band", "side"])
    check("デザイン: 組み合わせは3つ。知らない名前・重なりは除き、足りなければテンプレートの順", lays == ["vertical", "band", "bottom"]
          and A.pick_layouts([], ["side", "person"]) == ["side", "bottom", "band"], str(lays))
    desc = A.describe({"emphasis": "30g", "badge": "限定", "effect": "depth"})
    check("デザイン: 画面に出す説明", desc == "強調「30g」・バッジ「限定」・文字を主役の後ろに" and A.describe({"effect": "none"}) == "", desc)
    light = A.clean_design({"shade": "light"}, "テスト")["shade"], A.describe({"shade": "light"}), A.describe({"shade": "normal"})
    check("デザイン: 影の強さ（明るく・ふつう・くっきり）もAIが決め、ふつう以外は画面に出す",
          light == ("light", A.SHADES["light"], "") and set(A.SHADES) == {"light", "normal", "strong"}, str(light))

    plain, art = T.pick_schema(False), T.pick_schema(True)
    items = art["properties"]["picks"]["items"]
    check("AI: デザインを任せるときだけ、強調・バッジ・効果・組み合わせも答えてもらう",
          plain is T.PICK_SCHEMA and "emphasis" not in plain["properties"]["picks"]["items"]["properties"]
          and {"emphasis", "badge", "effect", "layouts"} <= set(items["required"]) and items["required"][-1] == "reason"
          and set(items["properties"]["effect"]["enum"]) == set(A.EFFECTS)
          and set(items["properties"]["shade"]["enum"]) == set(A.SHADES) == set(T.SHADE_TONES)
          and set(items["properties"]["layouts"]["items"]["enum"]) == set(A.LAYOUTS))
    eff = [T.thumb_effort(FakeTpl(x)) for x in ({}, {"effort": "low"}, {"effort": "MEDIUM"}, {"effort": "max"})]
    check("AI: サムネイルの考える深さは、決めていなければ high（動画とは別）", eff == ["high", "low", "medium", "high"], str(eff))
    ad = [T.art_director(FakeTpl(x)) for x in ({}, {"art_director": False}, {"art_director": "false"}, {"art_director": True})]
    check("AI: デザインも任せるのが初期設定。false でやめられる", ad == [True, False, False, True], str(ad))
    import tomllib
    th = tomllib.loads((ROOT / "templates" / "default" / "template.toml").read_text(encoding="utf-8-sig"))["thumbnail"]
    check("テンプレート: デザインも任せる・考える深さ high・強調とバッジの色", th.get("art_director") is True
          and th.get("effort") == "high" and "emphasis_color" in th and "badge_color" in th, str(th))
    try:
        import gui_ext_design as G
        keys = {s[0]: s for s in G.SPEC_THUMB}
        ok = {"art_director", "effort", "emphasis_color", "badge_color"} <= set(keys) \
            and {v for v, _ in keys["effort"][3]} == set(T.EFFORTS) and keys["art_director"][2] == "bool"
        check("編集画面: サムネイルのタブで、デザインを任せる・考える深さ・強調とバッジの色を変えられる", ok, str(list(keys)))
    except ImportError as e:   # PySide6 が無い環境
        check("編集画面: サムネイルのタブで、デザインを任せる・考える深さ・強調とバッジの色を変えられる", False, repr(e))


# ---------------------------------------------------------------- 強調する言葉
def t_emphasis(check):
    import rc_thumb as T
    runs = T._emph_runs(["年2回だけの", "受注生産は2回"], "2回")
    check("強調: 最初の1か所だけ", runs == [[("年", False), ("2回", True), ("だけの", False)], [("受注生産は2回", False)]], str(runs))
    st, (fp, idx) = _st("pop")
    f = T._font(fp, idx, 100)
    pos = T._text_pos(["30gの重さは", "正義か"], f, 100, 64, 300, "left", "30g")
    row0 = [g for g in pos if g.li == 0]
    base = {round(g.y + g.font.getmetrics()[0]) for g in row0}
    check("強調: 強調する言葉は大きく、行の中でベースラインがそろう",
          next(g for g in pos if g.emph).font.size == int(100 * T.EMPH_SCALE) and len(base) == 1
          and all(g.font.size == 100 for g in pos if not g.emph), str([(g.s, g.font.size) for g in pos]))
    _f1, plain = T._fit(fp, idx, ["30gの重さは"], 900, 400)
    _f2, emph = T._fit(fp, idx, ["30gの重さは"], 900, 400, emphasis="30g")
    check("強調: 大きくした分も入れて枠に収める", emph < plain, f"{plain}px → {emph}px")

    def red(design):
        img = T.compose(_photo(None), "30gの重さは\n正義か", "bottom", FakeTpl({"style": "pop"}), (fp, idx), design=design)
        a = np.asarray(img).astype(int)
        return int(((a[..., 0] > 210) & (a[..., 1] < 120) & (a[..., 2] < 110)).sum())
    on, off = red({"emphasis": "30g"}), red(None)
    check("強調: 強調する言葉だけ見せ方の強調色（ポップ=赤）", on > 3000 and off < 50, f"赤い画素 {on} / 強調なし {off}")


# ---------------------------------------------------------------- 縦書き
def t_vertical(check):
    import rc_thumb as T
    import rc_thumbart as A
    u = {s: A._units(s) for s in ("30gの重さ", "ABCDの", "シャーペン", "はい、")}
    check("縦書き: 字の分け方（短い英数字は1字分・長い英数字と「ー」は倒す・小さい字・句読点）",
          u["30gの重さ"][0] == ("30g", "tcy") and u["ABCDの"][0] == ("ABCD", "rot") and ("ー", "rot") in u["シャーペン"]
          and ("ャ", "small") in u["シャーペン"] and u["はい、"][-1] == ("、", "punct"), str(u))
    st, (fp, idx) = _st("elegant")
    f = T._font(fp, idx, 100)
    pos, (w, h) = A.vpos(["総合8点の", "理由"], f, 100, 1000, 50)
    c0, c1 = [g for g in pos if g.li == 0], [g for g in pos if g.li == 1]
    check("縦書き: 1行目が右の列、上から下へ", min(g.x for g in c0) > max(g.x for g in c1)
          and all(a.y < b.y for a, b in zip(c0, c0[1:])) and h > w, f"幅{w:.0f} 高さ{h:.0f}")
    pos, _ = A.vpos(["シャーペン、30g"], f, 100, 1000, 50)
    cx = 1000 - 50
    g = {p.s: p for p in pos}
    tcy = g["30g"]
    check("縦書き: 「ー」は縦向きに回す", g["ー"].rot == 90 and all(p.rot == 0 for p in pos if p.s != "ー"))
    check("縦書き: 小さい字・句読点は右上に寄せる", g["ャ"].x > cx + 5 and g["、"].x > cx + 40 and g["シ"].x == cx,
          f"ャ {g['ャ'].x - cx:+.0f} / 、 {g['、'].x - cx:+.0f}")
    check("縦書き: 「30g」は横に並べて1字分の幅に収める", tcy.font.getlength("30g") <= 100 * 0.92 + 1, f"{tcy.font.size}px")
    gl = T._glyphs(pos, f)
    check("縦書き: 1字ずつの枠（主役の後ろに回すときに使う）", len(gl) == len(pos) and all(b[2] > b[0] and b[3] > b[1] for b in gl))
    fv, sv = A.vfit(fp, idx, ["年2回だけの", "受注生産"], 500, 600, "2回")
    ww, hh = A.vpos(["年2回だけの", "受注生産"], fv, sv, 0, 0, "2回")[1]
    check("縦書き: 枠に収まる大きさ", ww <= 500 and hh <= 600 and sv >= 60, f"{sv}px 幅{ww:.0f} 高さ{hh:.0f}")
    vl = A.vlabel_fit("とても長い縦書きの見出しテストです", fp, idx, st, 400)
    check("縦書き: 長い見出しは縮めて、入らない分は「…」", vl and A.vlabel_h(vl) <= 400 and vl[1].endswith("…")
          and vl[0].size >= T.LABEL_FLOOR, str(vl and (vl[0].size, vl[1])))


# ---------------------------------------------------------------- バッジ
def t_badge(check):
    import rc_thumbart as A
    shapes = {}
    for style in ("elegant", "pop", "simple"):
        st, (fp, idx) = _st(style)
        b = A.badge_image("限定", st, (181, 55, 43), fp, idx)
        shapes[style] = b.size
    (ew, eh), (pw, ph), (sw, sh) = shapes["elegant"], shapes["pop"], shapes["simple"]
    check("バッジ: 上品=縦長の落款 / ポップ=丸いシール / シンプル=横長の札", eh > ew * 1.3 and abs(pw - ph) < pw * 0.1
          and sw > sh * 1.3, str(shapes))
    st, (fp, idx) = _st("elegant")
    b4 = A.badge_image("入手困難", st, (181, 55, 43), fp, idx).size
    check("バッジ: 落款は4字なら2列", abs(b4[0] - b4[1]) < b4[0] * 0.25, str(b4))
    bar = np.asarray(A.badge_image("ー", st, (181, 55, 43), fp, idx).convert("RGBA")).astype(int)
    hh, ww = bar.shape[:2]
    mid = bar[int(hh * 0.3):int(hh * 0.7), int(ww * 0.3):int(ww * 0.7)]
    ys, xs = np.where((mid[..., :3].min(axis=2) > 200) & (mid[..., 3] > 200))
    tall = len(ys) and (ys.max() - ys.min()) > 2 * (xs.max() - xs.min())
    check("バッジ: 落款は縦書きなので「ー」は縦向き", bool(tall),
          f"高さ{ys.max() - ys.min() if len(ys) else 0} 幅{xs.max() - xs.min() if len(xs) else 0}")
    st, (fp, idx) = _st("simple")
    check("バッジ: 色が auto なら帯の色（写真から）", A._badge_color({**st, "badge_color": "auto"}, (10, 20, 30)) == (10, 20, 30)
          and A._badge_color({**st, "badge_color": "#123456"}, (0, 0, 0)) == (0x12, 0x34, 0x56))
    tbox, subj = (64, 420, 700, 680), (760, 120, 1200, 600)
    bad = []
    for style in ("elegant", "pop", "simple"):
        st, (fp, idx) = _st(style)
        img = _photo(None)
        box = A.draw_badge(img, "検証", st, (181, 55, 43), tbox, subj, fp, idx)
        if not box or A._overlap(box, tbox) > 0.12 or A._overlap(box, subj) > 0.25:
            bad.append((style, box))
    check("バッジ: 文字にも大事な所にも重ねずに置く", not bad, str(bad))
    img = _photo(None)
    before = np.asarray(img).copy()
    none = A.draw_badge(img, "検証", st, (181, 55, 43), tbox, (0, 0, 1280, 720), fp, idx)
    check("バッジ: どこに置いても大事な所を隠すなら付けない", none is None and np.array_equal(before, np.asarray(img)))


# ---------------------------------------------------------------- 切り抜き
def t_cutout(check):
    import rc_thumbart as A
    A._CUT_CACHE.clear()
    t0 = time.time()
    a = A.cutout(_photo(), SUB)
    sec = time.time() - t0
    ok = a is not None
    check("切り抜き: 背景とはっきり違う物は切り抜ける（ダウンロード不要）", ok and a.shape == (720, 1280)
          and a[260:460, 480:800].mean() > 0.95 and a[160:190, 370:910].mean() < 0.05 and sec < 5,
          f"{sec:.1f}秒" + (f" 中 {a[260:460, 480:800].mean():.2f} 外 {a[160:190, 370:910].mean():.2f}" if ok else ""))
    A._CUT_CACHE.clear()
    b = A.cutout(_photo(), SUB)
    check("切り抜き: 毎回同じ結果", ok and b is not None and np.array_equal(a, b))
    check("切り抜き: 何も無い・色の差が小さい・枠が無いなら切り抜かない（効果は枠で代わりにする）",
          A.cutout(_photo(None), SUB) is None and A.cutout(_photo((192, 188, 180)), SUB) is None and A.cutout(_photo(), None) is None)
    crop = A.crop_alpha(a, (0, 0, 640, 360)) if ok else None
    check("切り抜き: 写真と同じように切り出して広げられる（横に文字・縦書き）", crop is not None and crop.shape == (720, 1280)
          and crop[400:700, 900:1270].mean() > 0.9)


# ---------------------------------------------------------------- 効果
def t_effects(check):
    import rc_thumbart as A
    img = _photo()
    a = np.asarray(img).astype(float)
    cut = A.cutout(img, SUB)
    st, _ = _st("pop")
    el, _ = _st("elegant")

    def ratio(out, ys, xs):
        return float(np.asarray(out).astype(float)[ys, xs].mean() / a[ys, xs].mean())
    corner, inside = (slice(20, 120), slice(20, 200)), (slice(300, 420), slice(500, 780))
    check("効果: none は写真をそのまま", A.apply_effect(img, "none", cut, SUB, st) is img)
    fo = A.apply_effect(img, "focus", cut, SUB, st)
    check("効果: focus は主役以外を暗く（暗くしすぎない）、主役はそのまま",
          0.72 < ratio(fo, *corner) < 0.88 and abs(ratio(fo, *inside) - 1) < 0.02,
          f"まわり {ratio(fo, *corner):.2f} 主役 {ratio(fo, *inside):.2f}")
    fb = A.apply_effect(img, "focus", None, SUB, st)
    check("効果: 切り抜けないときは、大事な所の枠の外を少し暗く",
          0.8 < ratio(fb, *corner) < 0.93 and abs(ratio(fb, *inside) - 1) < 0.02, f"{ratio(fb, *corner):.2f}")
    sk = np.asarray(A.apply_effect(img, "sticker", cut, SUB, st)).astype(float)
    ring = sk[300:420, 410:416].mean()
    check("効果: sticker は主役に白いふち", ring > 235 and sk[300:420, 600:700].mean() == a[300:420, 600:700].mean(),
          f"ふち {ring:.0f}")
    se = np.asarray(A.apply_effect(img, "sticker", cut, SUB, el)).astype(float)
    glow = se[300:420, 408:416]
    check("効果: 上品の sticker は白いふちではなく、やわらかい光", glow.mean() > a[300:420, 408:416].mean() * 0.95
          and glow.mean() < ring - 5 and glow[..., 0].mean() > glow[..., 2].mean() + 10, f"光 {glow.mean():.0f}")
    check("効果: 切り抜けないなら、白いふちの代わりに focus", np.array_equal(
        np.asarray(A.apply_effect(img, "sticker", None, SUB, st)), np.asarray(A.apply_effect(img, "focus", None, SUB, st))))
    de = A.apply_effect(img, "depth", cut, SUB, st)
    check("効果: depth は背景を少しだけ暗く（ぼかさない）", 0.85 < ratio(de, *corner) < 0.95, f"{ratio(de, *corner):.2f}")

    # 文字を主役の後ろに回す
    from PIL import ImageDraw

    def finish(glyphs, label=None):
        out = img.copy()
        ImageDraw.Draw(out).rectangle([400, 380, 900, 560], fill=(255, 255, 255))   # 文字のつもり
        A.finish(out, img, cut, SUB, {"effect": "depth"}, st, (40, 40, 40), (400, 380, 900, 560), None, 0, glyphs, label)
        return np.asarray(out)[450, 600]
    red = tuple(np.asarray(img)[450, 600])
    front = finish([(400, 500, 500, 560)])
    hide = finish([(500, 300, 700, 500)])
    lab = finish([(400, 500, 500, 560)], (450, 480, 700, 530))
    check("効果: depth は主役を文字の手前に", tuple(front) == red, f"{tuple(front)} / 主役 {red}")
    check("効果: 1字でも大きく隠れるなら、文字は手前のまま", tuple(hide) == (255, 255, 255), str(tuple(hide)))
    check("効果: 小さな見出しが少しでも隠れるなら、文字は手前のまま", tuple(lab) == (255, 255, 255), str(tuple(lab)))


# ---------------------------------------------------------------- 全部の組み合わせで作れる
def t_compose_all(check):
    import rc_thumb as T
    import rc_thumbart as A
    bad, t0 = [], time.time()
    frame = _photo()
    effects = list(A.EFFECTS)
    for i, (style, lay) in enumerate(product(T.STYLES, A.LAYOUTS)):
        tpl = FakeTpl({"style": style})
        design = {"emphasis": "30g", "badge": "検証", "effect": effects[i % len(effects)]}
        try:
            img = T.compose(frame, "30gの重さは\n正義か", lay, tpl, T.find_font(tpl), "野原工芸", "auto", SUB, design)
            if img.size != (T.W, T.H) or img.mode != "RGB":
                bad.append((style, lay, img.size, img.mode))
        except Exception as e:  # noqa: BLE001
            bad.append((style, lay, repr(e)))
    check("組み合わせ: 3つの見せ方 × 4つのデザイン × 効果で作れる", not bad, f"{time.time() - t0:.1f}秒 {bad[:3]}")
    tpl = FakeTpl({"style": "pop"})
    plain = np.asarray(T.compose(frame, "重さは\n正義か", "vertical", tpl, T.find_font(tpl), "", "auto", SUB))
    again = np.asarray(T.compose(frame, "重さは\n正義か", "vertical", tpl, T.find_font(tpl), "", "auto", SUB,
                                 {"emphasis": "", "badge": "", "effect": "none"}))
    check("組み合わせ: デザインを付けないときは、今までと同じ見た目", np.array_equal(plain, again))


# ---------------------------------------------------------------- 右下の再生時間の表示
def t_time(check):
    """YouTube は右下に動画の長さ（12:34 など）を重ねて表示する。文字・見出し・バッジをそこに置かない"""
    import rc_thumb as T
    import rc_thumbart as A
    drawn = []
    orig = T._draw_styled, T._draw_label, A.draw_vlabel, A.draw_badge

    def styled(img, lines, font, size, x, y, align, st, emphasis="", pos=None):
        p = pos if pos is not None else T._text_pos(lines, font, size, x, y, align, emphasis)
        drawn.extend(("文字", b) for b in T._glyphs(p, font))
        return orig[0](img, lines, font, size, x, y, align, st, emphasis, pos)

    def label(img, lab, x, y, align, st, accent, avoid=None):
        drawn.append(("見出し", T._label_box(lab, x, y, align)))
        return orig[1](img, lab, x, y, align, st, accent, avoid)

    def vlabel(img, vl, x_right, y, st, accent, avoid=None):
        if vl:
            drawn.extend(("見出し", b) for b in T._glyphs(A.vpos([vl[1]], vl[0], vl[0].size, x_right, y)[0], vl[0]))
        return orig[2](img, vl, x_right, y, st, accent, avoid)

    def badge(img, text, st, accent, tbox, subject, fp, idx):
        box = orig[3](img, text, st, accent, tbox, subject, fp, idx)
        drawn.append(("バッジ", box))
        return box
    T._draw_styled, T._draw_label, A.draw_vlabel, A.draw_badge = styled, label, vlabel, badge
    bad, n = [], 0
    try:
        frame = _photo(None)
        texts = ["右下まで届く長い文字\nテストです", "30gの重さは\n正義か"]
        for style, lay, text, pos in product(T.STYLES, A.LAYOUTS, texts, ("right", "bottom")):
            tpl = FakeTpl({"style": style})
            drawn.clear()
            T.compose(frame, text, lay, tpl, T.find_font(tpl), "野原工芸 シャーペン", pos, (0, 0, 520, 300),
                      {"emphasis": "30g" if "30g" in text else "", "badge": "検証", "effect": "none"})
            n += 1
            hits = [k for k, b in drawn if T.time_hit(b)]
            if hits:
                bad.append((style, lay, pos, text[:4], hits[:3]))
    finally:
        T._draw_styled, T._draw_label, A.draw_vlabel, A.draw_badge = orig
    check("再生時間: 文字・見出し・バッジを右下（動画の長さの表示）に置かない", not bad and n == 48, f"{len(bad)}/{n} {bad[:3]}")
    check("再生時間: 右下の範囲は横の右から2割弱・縦の下から15%", T.TIME_BOX == (T.W - 240, T.H - 110, T.W, T.H)
          and T.time_hit((1100, 650, 1150, 700)) and not T.time_hit((600, 650, 1000, 700)) and not T.time_hit((1100, 300, 1270, 590)))


# ---------------------------------------------------------------- 隠してはいけない所
def t_keep(check):
    import rc_thumb as T
    from PIL import ImageDraw
    big = (200, 100, 1080, 620)     # 大事な所の枠（物のまわりに平らな所が広い）
    k = T.Keep.build(_photo(), big)
    edge, flat = (395, 300, 455, 420), (220, 120, 300, 200)
    check("隠してはいけない所: 枠の中の物（輪郭の多い所）は物、平らな所は物ではない",
          k.obj[45, 52] == 1 and k.obj[18, 32] == 0 and k.base[18, 32] == 1)
    check("隠してはいけない所: 物を隠すほうが、枠の中の平らな所を隠すより重い",
          k.hide([edge]) > 2 * k.hide([flat]) > 0, f"{k.hide([edge]):.3f} / {k.hide([flat]):.3f}")
    check("隠してはいけない所: 物に触れると減点、離れていれば減点なし", k.touch([edge]) > 0.2 and k.touch([flat]) == 0,
          f"{k.touch([edge]):.2f} / {k.touch([flat]):.2f}")
    dust = _photo(None)
    ImageDraw.Draw(dust).ellipse([600, 340, 606, 346], fill=(40, 30, 30))
    check("隠してはいけない所: 小さな汚れは物と見なさない", T.Keep.build(dust, big).touch([(560, 300, 660, 400)]) == 0)
    kk = T.Keep.build(_photo(), big, [(1100, 100, 1200, 200)])
    inside = (1120, 120, 1180, 180)
    one, two = kk.touch([inside]), kk.touch([inside, edge])
    check("隠してはいけない所: AIが囲んだ枠も物。2つの物に触れると、1つより減点が大きい",
          one > 0.15 and two > one + 0.15 and kk.hide([inside]) > 0, f"{one:.2f} / {two:.2f}")
    whole = k.most([(380, 180, 900, 540)])
    check("隠してはいけない所: 1つの物をどれだけ隠すか", whole > 0.6 and k.most([flat]) == 0, f"{whole:.2f}")
    check("隠してはいけない所: 写真を切り出すと、物が切れる割合がわかる",
          k.lost(0, 0, 640, 360) > 0.5 and k.lost(0, 0, T.W, T.H) == 0, f"{k.lost(0, 0, 640, 360):.2f}")
    m = k.moved(0, 0, 640, 360)
    check("隠してはいけない所: 写真を切り出して広げたときの地図", m.part[50, 105] > 0 and m.obj[50, 105] > 0)
    items = T.pick_schema(True)["properties"]["picks"]["items"]
    check("隠してはいけない所: AIに枠を聞く（主役の枠とは別に、物を1つずつ）",
          "keep_boxes" in items["required"] and items["properties"]["keep_boxes"]["type"] == "array"
          and "keep_boxes" in T.PICK_SCHEMA["properties"]["picks"]["items"]["required"])


def _pen_scene():
    """机の上のペンとはかり（細長いペンと、表示のある台）の写真と、ペンの形"""
    from PIL import Image, ImageDraw, ImageFilter
    rng = np.random.default_rng(2)
    a = np.clip(np.array((214, 208, 198), float) + rng.normal(0, 5, (720, 1280, 1)), 0, 255).astype(np.uint8)
    img = Image.fromarray(a)
    d = ImageDraw.Draw(img)
    d.line([(200, 250), (1150, 110)], fill=(110, 30, 25), width=44)
    d.rounded_rectangle([300, 480, 1000, 700], radius=24, fill=(230, 230, 226), outline=(60, 60, 60), width=6)
    d.rectangle([420, 520, 880, 660], fill=(40, 60, 45))
    for i in range(3):
        d.rectangle([450 + i * 150, 550, 550 + i * 150, 630], fill=(190, 210, 180))
    pen = Image.new("L", (1280, 720), 0)
    ImageDraw.Draw(pen).line([(200, 250), (1150, 110)], fill=255, width=44)
    return img.filter(ImageFilter.GaussianBlur(1)), pen


PEN_SUBJECT = (150, 60, 1180, 710)
PEN_TEXT, PEN_LABEL = "重さ30gは\n正義か", "紅紫檀 シャーペン"


def t_avoid(check):
    """ペンとはかりの写真で、文字・見出しがペンにかからない"""
    import rc_thumb as T
    import rc_thumbart as A
    img, pen = _pen_scene()
    drawn, crops = [], []
    orig = T._draw_styled, T._draw_label, A.draw_vlabel, T._moved

    def styled(im, lines, font, size, x, y, align, st, emphasis="", pos=None):
        p = pos if pos is not None else T._text_pos(lines, font, size, x, y, align, emphasis)
        drawn.extend(T._glyphs(p, font))
        return orig[0](im, lines, font, size, x, y, align, st, emphasis, pos)

    def label(im, lab, x, y, align, st, accent, avoid=None):
        drawn.append(T._label_box(lab, x, y, align))
        return orig[1](im, lab, x, y, align, st, accent, avoid)

    def vlabel(im, vl, x_right, y, st, accent, avoid=None):
        if vl:
            drawn.extend(T._glyphs(A.vpos([vl[1]], vl[0], vl[0].size, x_right, y)[0], vl[0]))
        return orig[2](im, vl, x_right, y, st, accent, avoid)

    def moved(subject, *crop):
        crops.append(crop)
        return orig[3](subject, *crop)
    T._draw_styled, T._draw_label, A.draw_vlabel, T._moved = styled, label, vlabel, moved
    bad, side_bad, band = [], [], []
    try:
        for style, lay in product(T.STYLES, A.LAYOUTS):
            tpl = FakeTpl({"style": style})
            drawn.clear()
            crops.clear()
            out = T.compose(img, PEN_TEXT, lay, tpl, T.find_font(tpl), PEN_LABEL, "right", PEN_SUBJECT,
                            {"emphasis": "30g", "effect": "focus"})
            p = pen
            if crops:   # 横に文字・縦書きは写真を切り出して広げる。ペンも同じように動かす
                x0, y0, cw, ch = crops[-1]
                p = pen.crop((x0, y0, x0 + cw, y0 + ch)).resize((T.W, T.H))
            mask = np.asarray(p) > 127
            cover = np.zeros(mask.shape, bool)
            for b in drawn:
                if b:
                    cover[max(0, int(b[1])):max(0, int(b[3])), max(0, int(b[0])):max(0, int(b[2]))] = True
            share = float((mask & cover).sum() / max(mask.sum(), 1))
            swaps = out.info["hide"] > T.POOR_HIDE or out.info["most"] > T.POOR_MOST
            if lay == "band":
                band.append(swaps)
            elif lay in ("side", "vertical") and share > 0:
                side_bad.append((style, lay, round(share, 3)))
            elif share > 0.08 and not swaps:
                bad.append((style, lay, round(share, 3), out.info["most"]))
    finally:
        T._draw_styled, T._draw_label, A.draw_vlabel, T._moved = orig
    check("隠してはいけない所: 横に文字・縦書きは、写真の寄せ方と大きさを変えてペンを避ける", not side_bad, str(side_bad))
    check("隠してはいけない所: 大きな文字もペンにほとんどかからない（避けきれないなら、AIに任せたとき別のデザインに替わる）",
          not bad, str(bad))
    check("隠してはいけない所: ペンを覆う帯は、別のデザインに替える", band and all(band), str(band))

    # AIにデザインを任せるとき: 帯は替わる。替えるものが無ければ、AIが選んだ順のまま
    tpl = FakeTpl({"style": "elegant"})
    font = T.find_font(tpl)
    x0, y0, x1, y1 = PEN_SUBJECT
    p = {"text": PEN_TEXT, "label": PEN_LABEL, "text_position": "right",
         "subject_box": {"x0": x0 / T.W * 100, "y0": y0 / T.H * 100, "x1": x1 / T.W * 100, "y1": y1 / T.H * 100}}
    design = {"emphasis": "30g", "effect": "focus"}
    made = [lay for lay, _ in T._compose_set(img, p, ["band", "side", "bottom"], tpl, font, design, [], True)]
    check("デザインを替える: ペンを覆う帯は、使っていないデザインに替える（AIが選んだ順の後ろに）",
          made == ["side", "bottom", "vertical"], str(made))
    kept = [lay for lay, _ in T._compose_set(img, p, ["band", "side", "bottom"], tpl, font, design, [], False)]
    check("デザインを替える: AIに任せないときは替えない", kept == ["band", "side", "bottom"], str(kept))
    same = [lay for lay, _ in T._compose_set(img, p, ["vertical", "side", "bottom"], tpl, font, design, [], True)]
    check("デザインを替える: 替えるものが無ければ、AIが選んだ順のまま", same == ["vertical", "side", "bottom"], str(same))

    # 写真を寄せる向き: 大事な所が分からなくても、写真の中身を文字の反対側へ
    def red_x(pos):
        out = np.asarray(T.compose(_photo(), "重さは\n正義か", "side", tpl, font, "", pos, None)).astype(int)
        return float(np.nonzero(out[..., 0] > out[..., 1] + 60)[1].mean())
    right, left = red_x("right"), red_x("left")
    check("横に文字: 写真の中身を文字の反対側へ寄せる（右に文字なら左へ）", right < 600 and left > 680,
          f"右に文字 {right:.0f} / 左に文字 {left:.0f}")

    # 写真の効果で、サムネイル全体が暗くなりすぎない（文字の下のグラデーション・四隅も控えめにする）
    fr = _photo()
    dark = {}
    for style in T.STYLES:
        tpl = FakeTpl({"style": style})
        f = T.find_font(tpl)
        base = np.asarray(T.compose(fr, "30gの重さは\n正義か", "bottom", tpl, f, "野原工芸", "auto", SUB,
                                    {"effect": "none"})).astype(float).mean()
        dark[style] = round(np.asarray(T.compose(fr, "30gの重さは\n正義か", "bottom", tpl, f, "野原工芸", "auto", SUB,
                                                 {"effect": "focus"})).astype(float).mean() / base, 3)
    # （文字の後ろの影は写真の明るさに合わせるので、効果で暗くなった分は影が薄くなる。効果が効くことは t_effects で見る）
    check("効果: 写真の効果を付けても、全体が暗くなりすぎない", all(0.82 < v <= 1.0 for v in dark.values()), str(dark))


def t_lines(check):
    """大きくした強調の言葉の字（g・y など、ベースラインより下に出る字）が、次の行の字に重ならない"""
    import rc_thumb as T
    fp = T.find_font(FakeTpl({"style": "elegant"}))
    gaps = {}
    for text, emph in (("重さ30gは\n正義か", "30g"), ("軽さ1.2kg\n本当？", "1.2kg"), ("ygの重さ\n正義", "yg")):
        f = T._font(fp[0], fp[1], 120)
        rows = {}
        for g in T._text_pos(text.split("\n"), f, 120, 100, 100, "left", emph):
            for b in T._glyphs([g], f):
                lo, hi = rows.get(g.li, (1e9, -1e9))
                rows[g.li] = (min(lo, b[1]), max(hi, b[3]))
        gaps[text.replace("\n", "／")] = round(rows[1][0] - rows[0][1], 1)
    check("行の間: 大きくした「30g」の g なども、次の行の字に重ならない（字の大きさの12%以上空ける）",
          all(v >= T.LINE_GAP * 120 - 1 for v in gaps.values()), str(gaps))


def t_shade(check):
    """文字の後ろの影は、画面の下半分・右半分など端までいっぱいに（中途半端な明るい所を残さない）。
    濃さは写真の明るさに合わせる（もともと暗い所は薄く）。AIが決めた強さ（明るく・ふつう・くっきり）で変わる"""
    from PIL import Image
    import rc_thumb as T
    u = Image.new("RGB", (T.W, T.H), (200, 200, 200))
    a = u.copy()
    T._shade(a, "y", 400, T.H, 150, "bottom")
    A = np.asarray(a.convert("L")).astype(float)
    b = u.copy()
    T._shade(b, "x", 700, T.W, 150, "right")
    B = np.asarray(b.convert("L")).astype(float)
    check("影: 下半分・右半分は、端までムラなく暗くする（帯の外はそのまま）",
          A[400:].std(axis=1).max() < 1 and A[400:].mean(axis=1).max() < 110 and abs(A[:200].mean() - 200) < 1
          and B[:, 700:].std(axis=0).max() < 1 and B[:, 700:].mean(axis=0).max() < 110 and abs(B[:, :500].mean() - 200) < 1,
          f"下 {A[400:].mean(axis=1).max():.0f} / 右 {B[:, 700:].mean(axis=0).max():.0f}")

    box = (100, 450, 1100, 700)
    bright, dark = _photo(None), _photo(None, bg=(60, 58, 55))
    need = [T._shade_need(bright, box, 150, t) for t in ("light", "normal", "strong")]
    check("影: 明るい写真には濃く、もともと暗い写真にはごく薄く", T._shade_need(dark, box, 150) <= 150 * T.SHADE_MIN + 1
          and need[1] > 2 * T._shade_need(dark, box, 150), f"{need[1]} / 暗い写真 {T._shade_need(dark, box, 150)}")
    check("影: AIが決めた強さで濃さが変わる（明るく < ふつう < くっきり）", need[0] < need[1] < need[2], str(need))

    tpl = FakeTpl({"style": "elegant"})
    fp = T.find_font(tpl)
    res = {}
    for tone in ("light", "normal", "strong"):
        im = np.asarray(T.compose(_photo(None), "30gの重さは\n正義か", "bottom", tpl, fp, "野原工芸", "bottom", SUB,
                                  {"effect": "none", "shade": tone}).convert("L")).astype(float)
        res[tone] = (round(im.mean(), 1), round(im[660:700, :60].mean(), 1), round(im[660:700, -60:].mean(), 1),
                     round(im[30:80, 560:720].mean(), 1))
    ok = res["light"][0] > res["normal"][0] > res["strong"][0]
    ok &= all(abs(v[1] - v[2]) < 8 and v[1] < v[3] - 40 for v in res.values())
    check("影: 大きな文字を下に置くと、下の左右の端まで同じように暗くなる（上は明るいまま）。明るくの指定で全体も明るい",
          ok, str(res))


def t_band_center(check):
    """帯の文字は、見た目（見出しの字の上から大きな文字の下まで）が帯の真ん中に来る"""
    import rc_thumb as T
    rec = {}
    orig = T._draw_styled, T._draw_label

    def styled(img, lines, font, size, x, y, align, st, emphasis="", pos=None):
        p = pos if pos is not None else T._text_pos(lines, font, size, x, y, align, emphasis)
        rec["glyphs"] = T._glyphs(p, font)
        return orig[0](img, lines, font, size, x, y, align, st, emphasis, pos)

    def label(img, lab, x, y, align, st, accent, avoid=None):
        rec["label"] = (lab, y)
        return orig[1](img, lab, x, y, align, st, accent, avoid)
    T._draw_styled, T._draw_label = styled, label
    bad, n = [], 0
    try:
        for style, lab_text, pos in product(("elegant", "simple"), ("野原工芸", ""), ("top", "bottom")):
            tpl = FakeTpl({"style": style})
            rec.clear()
            T.compose(_photo(None), "紅紫檀の美しさ", "band", tpl, T.find_font(tpl), lab_text, pos, None)
            gl = rec["glyphs"]
            lab, ly = rec.get("label", (None, 0))
            top = ly + lab[0].getbbox(lab[1])[1] if lab else min(b[1] for b in gl)
            bottom = max(b[3] for b in gl)
            band_h = int(T.H * (0.38 if lab else 0.34))
            y0 = 0 if pos == "top" else T.H - band_h
            off = (top + bottom) / 2 - (y0 + band_h / 2)
            n += 1
            if abs(off) > 3:
                bad.append((style, lab_text, pos, round(off, 1)))
    finally:
        T._draw_styled, T._draw_label = orig
    check("帯: 文字の見た目が帯の真ん中に来る（明朝体でも下に寄らない）", not bad and n == 8, str(bad))


# ---------------------------------------------------------------- 動画から作る（AIの答えを使う）
def t_generate(check, out: Path, fx: Path):
    import rc_llm
    import rc_thumb as T
    from rc_template import Template
    if not (out / "session.json").exists():
        check("デザインもAIに: 動画からのサムネイル案（荒編集の結果が必要）", False, "先に tests/smoke_test.py を実行してください")
        return
    work = fx / "work_thumbart"
    if work.exists():
        shutil.rmtree(work)
    sd = work / "sess"
    shutil.copytree(out, sd, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "*.png"))
    seen = {}

    def fake_call(client, anthropic, system, content, schema, effort, usage):
        nos = [int(b["text"][2:b["text"].index("（")]) for b in content
               if b.get("type") == "text" and b["text"].startswith("候補")]
        seen.update(system=system, schema=schema, effort=effort, content=content, nos=nos)
        # 大事な所は左上の小さな所（どのデザインでも避けられる。隠しすぎてデザインが替わることはない）
        item = {"label": "マキタ", "text_position": "any", "subject_box": {"x0": 2, "y0": 2, "x1": 12, "y1": 12},
                "reason": "テスト"}
        return {"picks": [
            {**item, "frame": nos[0], "text": "軽さ1.2kg\\n本当？", "emphasis": "1.2kg", "badge": "検証", "effect": "focus",
             "layouts": ["vertical", "band"], "keep_boxes": [{"x0": 3, "y0": 3, "x1": 8, "y1": 8}]},
            {**item, "frame": nos[1], "text": "穴あけ一瞬", "emphasis": "存在しない", "badge": "とても長すぎるバッジ",
             "effect": "explode", "layouts": ["なにか"]},
            {**item, "frame": nos[2], "text": "買って正解", "emphasis": "正解", "badge": "", "effect": "depth",
             "layouts": ["side", "vertical", "bottom"]}]}
    orig = rc_llm._client, rc_llm._call
    rc_llm._client, rc_llm._call = (lambda: (None, None)), fake_call
    try:
        outs = T.generate(sd, Template(fx / "tpl"), "api")
    except BaseException as e:  # noqa: BLE001
        check("デザインもAIに: 動画からのサムネイル案（偽のAI）", False, repr(e))
        return
    finally:
        rc_llm._client, rc_llm._call = orig
    td = sd / "thumbnails"
    info = json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))
    names = {p.name for p in outs}
    check("デザインもAIに: 考える深さはテンプレートの high（動画の設定とは別）", seen.get("effort") == "high", str(seen.get("effort")))
    check("デザインもAIに: 強調・バッジ・効果・組み合わせも聞く",
          "emphasis" in seen.get("schema", {}).get("properties", {}).get("picks", {}).get("items", {}).get("required", [])
          and T.ART_PROMPT.splitlines()[0] in seen.get("system", ""))
    check("隠してはいけない枠: AIへの指示に、物を1つずつ囲む枠（keep_boxes）と右下の再生時間の説明がある",
          "keep_boxes" in seen.get("system", "") and "動画の長さ" in seen.get("system", ""))
    check("デザインもAIに: AIが選んだ組み合わせで作る（縦書きも）", info[0]["layouts"] == ["vertical", "band", "bottom"]
          and {"thumb_1_vertical.jpg", "thumb_1_band.jpg", "thumb_3_side.jpg"} <= names
          and info[2]["layouts"] == ["side", "vertical", "bottom"], str([i["layouts"] for i in info]))
    check("デザインもAIに: 決めたデザインを thumbnails.json に残す",
          (info[0]["emphasis"], info[0]["badge"], info[0]["effect"]) == ("1.2kg", "検証", "focus")
          and info[2]["effect"] == "depth", str([(i.get("emphasis"), i.get("badge"), i.get("effect")) for i in info]))
    check("隠してはいけない枠: AIが囲んだ枠を thumbnails.json に残す（無ければ書かない）",
          info[0].get("keep_boxes") == [{"x0": 3, "y0": 3, "x1": 8, "y1": 8}] and "keep_boxes" not in info[1],
          str([i.get("keep_boxes") for i in info]))
    check("デザインもAIに: おかしな答えは使わない（知らない組み合わせはテンプレートの順）",
          (info[1]["emphasis"], info[1]["badge"], info[1]["effect"]) == ("", "", "none")
          and info[1]["layouts"] == ["bottom", "band", "side"], str(info[1]))

    # サムネイル案の画面から、AIに指示して作り直す（前回の案の画像・内容と、これまでの指示もAIに見せる）
    first_no = (seen.get("nos") or [None])[0]
    grid = T.grid_files(td)
    order = [T.LAY_NAMES.get(f.stem.split("_", 2)[-1]) for fs in grid.values() for f in fs]
    rc_llm._client, rc_llm._call = (lambda: (None, None)), fake_call
    try:
        seen.clear()
        T.generate(sd, Template(fx / "tpl"), "api", None, None, "案2は文字を左に")
        c1, s1, hist1 = seen.get("content", []), seen.get("system", ""), T.requests_made(td)
        seen.clear()
        T.generate(sd, Template(fx / "tpl"), "api", None, None, "もっと明るく")
        c2, hist2 = seen.get("content", []), T.requests_made(td)
    except BaseException as e:  # noqa: BLE001
        check("AIへの指示: 指示して作り直す（偽のAI）", False, repr(e))
        return
    finally:
        rc_llm._client, rc_llm._call = orig
    t1 = [b["text"] for b in c1 if b.get("type") == "text"]
    t2 = [b["text"] for b in c2 if b.get("type") == "text"]
    labels = [b["text"] for k, b in enumerate(c1[:-1]) if b.get("type") == "text"
              and b["text"].startswith("前回の案") and c1[k + 1].get("type") == "image"]
    check("AIへの指示: 指示と前回の案の内容（どの場面・文字・デザインか）がAIに届く。作り直しの決まりも伝える",
          t1[-1:] == ["今回の指示: 案2は文字を左に"] and T.REQUEST_PROMPT.splitlines()[0] in s1
          and T.REQUEST_ART.splitlines()[0].strip() in s1
          and any(t.startswith(f"前回の案1（上の段）: 場面: 候補{first_no}／") for t in t1), str(t1[-6:])[:400])
    check("AIへの指示: 前回の画像を、画面と同じ並び（案1が上の段、各段は左から）で見せる",
          len(labels) == len(order) > 0 and labels[0].startswith("前回の案1・左端")
          and [x.rsplit("・", 1)[-1] for x in labels] == order, f"{labels} / {order}")
    check("AIへの指示: 反映した指示を残し、次の指示のときも見せる（指示は積み重なる）",
          hist1 == ["案2は文字を左に"] and hist2 == ["案2は文字を左に", "もっと明るく"] and t2[-1:] == ["今回の指示: もっと明るく"]
          and any(t.startswith("これまでの指示") and "- 案2は文字を左に" in t for t in t2), f"{hist1} {hist2}")

    # 設定でやめる（今までどおり）・考える深さを変える
    tdir = work / "tpl_noart"
    shutil.copytree(fx / "tpl", tdir, ignore=shutil.ignore_patterns("bgm"))
    s = (tdir / "template.toml").read_text(encoding="utf-8")
    s = s.replace("\nart_director = true", "\nart_director = false").replace('\neffort = "high"', '\neffort = "medium"')
    (tdir / "template.toml").write_text(s, encoding="utf-8")
    seen.clear()
    rc_llm._client, rc_llm._call = (lambda: (None, None)), fake_call
    try:
        T.generate(sd, Template(tdir), "api")
        info = json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))
        check("AIへの指示: 指示なしで作り直すと、指示はまっさらに戻る", not (td / "requests.json").exists())
        ok = seen.get("effort") == "medium" and "emphasis" not in seen["schema"]["properties"]["picks"]["items"]["properties"] \
            and T.ART_PROMPT.splitlines()[0] not in seen["system"] and all("emphasis" not in i for i in info) \
            and all(i["layouts"] == ["bottom", "band", "side"] for i in info)
        check("デザインもAIに: art_director = false なら今までどおり（考える深さはテンプレートの値）", ok,
              f"{seen.get('effort')} {[i['layouts'] for i in info]}")
        T.generate(sd, Template(tdir), "api", "low")
        check("デザインもAIに: その回だけ考える深さを変えられる（thumbs.py --effort）", seen.get("effort") == "low")
        T.generate(sd, Template(tdir), "api", None, None, "もっと明るく")
        check("AIへの指示: デザインを任せないときも使える（決められる項目だけで反映）",
              T.REQUEST_PROMPT.splitlines()[0] in seen["system"] and T.REQUEST_ART.splitlines()[0].strip() not in seen["system"]
              and T.requests_made(td) == ["もっと明るく"], str(T.requests_made(td)))
    except BaseException as e:  # noqa: BLE001
        check("デザインもAIに: art_director = false なら今までどおり（考える深さはテンプレートの値）", False, repr(e))
    finally:
        rc_llm._client, rc_llm._call = orig

    # AIを使わないとき: 数字だけ強調する
    outs = T.generate(sd, Template(fx / "tpl"), "none", None, "重さ1.2kg\\n本当？", "もっと明るく")
    info = json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))
    check("AIなし: 数字（単位つき）だけ強調し、バッジ・効果は付けない", all(
        (i.get("emphasis"), i.get("badge"), i.get("effect")) == ("1.2kg", "", "none") for i in info) and len(outs) >= 9,
        str([(i.get("emphasis"), i.get("badge"), i.get("effect")) for i in info]))
    check("AIへの指示: AIを使わないときは指示を使わない（止まらずに作る）", not (td / "requests.json").exists())


def t_scene_units(check):
    """場面の候補の取り方・時刻の読み書き・選んだ場面を必ず使う（動画なしで確かめられる所）"""
    import rc_thumb as T
    from rc_timeline import Piece

    class TL:   # 完成 20秒（2つ目の区間は ×8 の早送り）。元素材 0〜90秒
        fps = 10
        duration = 20.0
        pieces = [Piece(0, 0, 0, 0, 100, 0.0, 10.0), Piece(0, 0, 0, 100, 200, 10.0, 90.0)]

        def map_time(self, t):
            return 0.0 if 0 <= t < 90 else None
    src = [T.source_at(TL(), x) for x in (5.0, 15.0, 30.0)]
    check("場面: 完成動画の時刻 → 元素材の時刻（早送りの区間は倍率をかける・範囲の外は端に）",
          src == [5.0, 50.0, 89.5], str(src))

    class Long(TL):   # 見どころ・テロップがたくさんある長い動画（完成 600秒）
        duration = 600.0
        pieces = [Piece(0, 0, 0, 0, 6000, 0.0, 600.0)]

        def map_time(self, t):
            return t if 0 <= t < 600 else None
    chunks = {i: {"start": i * 10.0, "end": i * 10.0 + 4} for i in range(60)}
    dec = {"highlights": [{"id": i, "label": f"見どころ{i}"} for i in range(0, 30, 2)]}
    c0 = T.candidate_times({}, Long(), chunks, dec, {})
    pri = [t for t, why in c0 if why != "全体から"]
    even = sorted(t for t, why in c0 if why == "全体から")
    check("場面: 候補は20か所。見どころなどは8つまで、残りは動画の最後まで均等に（話の途中の場面ばかりにしない）",
          len(c0) == T.N_CANDIDATES and len(pri) == T.N_PRIORITY and even[-1] > 540 and even[0] < 60,
          f"{len(c0)} / 見どころ {len(pri)} / 全体 {[round(t) for t in even]}")
    used = [pri[0], even[3]]
    c1 = T.candidate_times({}, Long(), chunks, dec, {}, phase=T.PHASES[1], avoid=used)
    check("場面: 別の場面から選び直すときは、今の案の場面から離し、均等に取る位置もずらす",
          all(abs(t - u) > T.AVOID_GAP for t, _ in c1 for u in used)
          and len({round(t) for t, w in c1 if w == "全体から"} & {round(t) for t in even}) < 3, str([round(t) for t, _ in c1]))
    tt = T.parse_times("1:23, 4:56、83.5 1:02:03")
    bad = []
    for s in ("1:xx", "-3", "1::2"):
        try:
            T.parse_times(s)
            bad.append(s)
        except ValueError:
            pass
    check("場面: 時刻の書き方（1:23・83.5・1:02:03）を読み、読めないものは知らせる",
          tt == [83.0, 296.0, 83.5, 3723.0] and not bad and T.fmt_time(83.4) == "1:23" and T.fmt_time(3723) == "1:02:03",
          f"{tt} {bad}")
    picks = [{"frame": 1, "text": "A"}, {"frame": 5, "text": "B"}, {"frame": 7, "text": "C"}]
    f1 = [p["frame"] for p in T._force(picks, [7], ["x"])]
    f2 = T._force(picks, [9, 5], ["x"])
    check("場面: 選んだ場面は、この順で必ず先頭に（AIが使わなかった場面も入れる）。全部で3つまで",
          f1 == [7, 1, 5] and [p["frame"] for p in f2] == [9, 5, 1] and f2[0]["text"] == "A"
          and f2[0]["subject_box"] is None, f"{f1} {[p['frame'] for p in f2]}")
    must = T._must_prompt([3, 7])
    check("場面: 選んだ場面はAIに必ず使うよう伝える", "候補3・候補7" in must and "残りの1枚" in must and T._must_prompt([]) == "",
          must)


def t_scenes(check, out: Path, fx: Path):
    """場面を選んで作る・別の場面から選び直す・場面を増やす（動画から。偽のAI）"""
    import rc_llm
    import rc_thumb as T
    from rc_template import Template
    sd = fx / "work_thumbart" / "sess"
    if not (sd / "session.json").exists():
        check("場面: 動画から（先に t_generate が必要）", False, "work_thumbart がありません")
        return
    td = sd / "thumbnails"
    tpl = Template(fx / "tpl")
    seen = []

    def fake_call(client, anthropic, system, content, schema, effort, usage):
        nos = [int(b["text"][2:b["text"].index("（")]) for b in content
               if b.get("type") == "text" and b["text"].startswith("候補")]
        seen.append({"system": system, "nos": nos, "texts": [b["text"] for b in content if b.get("type") == "text"]})
        item = {"label": "", "text_position": "any", "subject_box": {"x0": 2, "y0": 2, "x1": 12, "y1": 12}, "reason": "テスト",
                "emphasis": "", "badge": "", "effect": "none", "layouts": ["bottom", "band", "side"]}
        return {"picks": [{**item, "frame": n, "text": f"案{k}"} for k, n in enumerate(nos[-3:], 1)]}   # 選んだ場面は使わない
    orig = rc_llm._client, rc_llm._call
    rc_llm._client, rc_llm._call = (lambda: (None, None)), fake_call
    try:
        T.generate(sd, tpl, "api")
        pool0 = T.load_pool(td)
        used0 = [i["source_time"] for i in json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))]
        check("場面: 候補を candidates.json に残す（完成動画の時刻つき。今のカットのもの）",
              0 < len(pool0["cands"]) <= T.N_CANDIDATES and all("ot" in c and (td / "_candidates" / c["file"]).exists()
                                                                for c in pool0["cands"]) and T.pool_valid(sd),
              str(len(pool0.get("cands", []))))
        # 選んだ場面: 候補の1つ（完成動画の時刻）と、候補に無い時刻
        c = sorted(pool0["cands"], key=lambda c: c["ot"])[-1]
        dur = max(x["ot"] for x in pool0["cands"])
        other = round(min(x["ot"] for x in pool0["cands"]) + 1.3, 2)
        seen.clear()
        T.generate(sd, tpl, "api", scenes=[c["ot"], other])
        info = json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))
        pool1 = T.load_pool(td)
        sys_ = seen[-1]["system"] if seen else ""
        check("場面: 選んだ場面は、AIが使わなくても案1・案2に（この順・完成動画の時刻も残す）",
              len(info) == 3 and [i.get("chosen", False) for i in info] == [True, True, False]
              and abs(info[0]["video_time"] - c["ot"]) < 0.05 and abs(info[1]["video_time"] - other) < 0.6,
              str([(i.get("video_time"), i.get("chosen")) for i in info]))
        check("場面: 候補に無い時刻は、その場面を取り出して候補に足す。AIには選んだ場面を必ず使うよう伝える",
              len(pool1["cands"]) == len(pool0["cands"]) + 1 and "使う人が選んだ場面" in sys_
              and sum("使う人が選んだ場面" in t for t in seen[-1]["texts"]) == 2, f"{len(pool1['cands'])} {dur}")
        three = sorted(pool1["cands"], key=lambda c: c["ot"])[:3]
        seen.clear()
        T.generate(sd, tpl, "api", scenes=[x["ot"] for x in three])
        check("場面: 3つ選んだら、AIにはその3つだけ見せる", seen and sorted(seen[-1]["nos"]) == sorted(x["no"] for x in three),
              str(seen[-1]["nos"] if seen else None))
        # 別の場面から選び直す
        used = [i["source_time"] for i in json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))]
        T.generate(sd, tpl, "api", fresh=True)
        pool2 = T.load_pool(td)
        info2 = json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))
        check("場面: 別の場面から選び直すと、今の案の場面を外した新しい候補から選ぶ",
              pool2["round"] == pool1["round"] + 1 and pool2["cands"]
              and all(abs(x["t"] - u) > T.AVOID_GAP for x in pool2["cands"] for u in used)
              and not any(i.get("chosen") for i in info2), f"round {pool2.get('round')} / {used0}")
        # 場面を増やす（サムネイル案は作り直さない）
        before = sorted((td).glob("thumb_*.jpg"))
        mtimes = [f.stat().st_mtime for f in before]
        new = T.more_scenes(sd, tpl, 6)
        pool3 = T.load_pool(td)
        ts = sorted(x["t"] for x in pool3["cands"])
        check("場面: 場面を増やす（今の候補と重ならない所を足す。サムネイル案はそのまま）",
              new and len(pool3["cands"]) == len(pool2["cands"]) + len(new)
              and all(b - a > T.CAND_GAP - 0.5 for a, b in zip(ts, ts[1:]))
              and [f.stat().st_mtime for f in before] == mtimes, f"+{len(new)} / {[round(t, 1) for t in ts]}")
        # 指示で作り直すときは、同じ候補を使い回す（番号も同じ）
        T.generate(sd, tpl, "api", request="もっと明るく")
        check("場面: 指示で作り直すときは、同じ候補を使い回す", T.load_pool(td)["cands"] == pool3["cands"])
    except BaseException as e:  # noqa: BLE001
        import traceback
        check("場面: 動画から（偽のAI）", False, traceback.format_exc()[-800:])
    finally:
        rc_llm._client, rc_llm._call = orig


def run_all(check, out: Path, fx: Path):
    t_values(check)
    t_emphasis(check)
    t_vertical(check)
    t_badge(check)
    t_cutout(check)
    t_effects(check)
    t_compose_all(check)
    t_time(check)
    t_keep(check)
    t_avoid(check)
    t_band_center(check)
    t_lines(check)
    t_shade(check)
    t_scene_units(check)
    t_generate(check, out, fx)
    t_scenes(check, out, fx)


if __name__ == "__main__":
    import make_fixtures
    res = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    print(f"\n{sum(res)}/{len(res)} 件 成功")
    sys.exit(0 if all(res) else 1)
