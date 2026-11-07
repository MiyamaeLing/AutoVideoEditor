"""サムネイルの凝った表現のテスト（rc_thumbart.py と rc_thumb.compose / generate）。

- 強調する言葉: 最初の1か所だけ大きく・別の色に。行の中でベースラインがそろう
- 縦書き: 右の列から。「ー」は横に倒す・小さい字と句読点は右上・「30g」は横に並べて1字分
- バッジ: 上品=縦長の落款 / ポップ=丸いシール / シンプル=横長の札。文字と大事な所を避けて置く
- 主役の切り抜き: はっきりした物は切り抜ける。何もない・色の差が小さいときは切り抜かない。毎回同じ結果
- 効果: focus=まわりを暗く / sticker=白いふち（切り抜けないときは focus）/ depth=主役を文字の手前に（隠れすぎるなら回さない）
- AIにデザインも任せる: 答えの形・指示・考える深さ（動画とは別）・おかしな答えを直す・設定でやめられる
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
    check("デザイン: AIの答えをそのまま使う（バッジの空白は詰める）", d == {"emphasis": "30g", "badge": "検証", "effect": "focus"}, str(d))
    bad = A.clean_design({"emphasis": "40g", "badge": "とても長いバッジの言葉", "effect": "explode"}, "30gの重さ", "")
    check("デザイン: 文字に無い強調・長すぎるバッジ・知らない効果は使わない", bad == {"emphasis": "", "badge": "", "effect": "none"}, str(bad))
    same = A.clean_design({"emphasis": "30gの重さ", "badge": "野原工芸"}, "30gの重さ", "野原 工芸")
    check("デザイン: 文字全部の強調・見出しと同じバッジは使わない", same["emphasis"] == "" and same["badge"] == "", str(same))
    check("デザイン: 答えが壊れていても使える形にする", A.clean_design(None, "テスト") == {"emphasis": "", "badge": "", "effect": "none"})
    autos = {s: A.auto_emphasis(s) for s in ("30gの重さは\n正義か", "1.2kgの軽さ", "年2回だけの受注生産", "軽すぎて笑った", "2024")}
    check("デザイン: AIなしでは、数字（単位つき）を強調", autos == {"30gの重さは\n正義か": "30g", "1.2kgの軽さ": "1.2kg",
                                                        "年2回だけの受注生産": "2回", "軽すぎて笑った": "", "2024": ""}, str(autos))
    lays = A.pick_layouts(["vertical", "なにか", "vertical", "band"], ["bottom", "band", "side"])
    check("デザイン: 組み合わせは3つ。知らない名前・重なりは除き、足りなければテンプレートの順", lays == ["vertical", "band", "bottom"]
          and A.pick_layouts([], ["side", "person"]) == ["side", "bottom", "band"], str(lays))
    desc = A.describe({"emphasis": "30g", "badge": "限定", "effect": "depth"})
    check("デザイン: 画面に出す説明", desc == "強調「30g」・バッジ「限定」・文字を主役の後ろに" and A.describe({"effect": "none"}) == "", desc)

    plain, art = T.pick_schema(False), T.pick_schema(True)
    items = art["properties"]["picks"]["items"]
    check("AI: デザインを任せるときだけ、強調・バッジ・効果・組み合わせも答えてもらう",
          plain is T.PICK_SCHEMA and "emphasis" not in plain["properties"]["picks"]["items"]["properties"]
          and {"emphasis", "badge", "effect", "layouts"} <= set(items["required"]) and items["required"][-1] == "reason"
          and set(items["properties"]["effect"]["enum"]) == set(A.EFFECTS)
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
    check("効果: focus は主役以外を暗く、主役はそのまま", ratio(fo, *corner) < 0.75 and abs(ratio(fo, *inside) - 1) < 0.02,
          f"まわり {ratio(fo, *corner):.2f} 主役 {ratio(fo, *inside):.2f}")
    fb = A.apply_effect(img, "focus", None, SUB, st)
    check("効果: 切り抜けないときは、大事な所の枠の外を暗く", ratio(fb, *corner) < 0.75 and abs(ratio(fb, *inside) - 1) < 0.02)
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
    check("効果: depth は背景を少しだけ暗く（ぼかさない）", 0.8 < ratio(de, *corner) < 0.9, f"{ratio(de, *corner):.2f}")

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
        seen.update(system=system, schema=schema, effort=effort)
        item = {"label": "マキタ", "text_position": "any", "subject_box": {"x0": 20, "y0": 20, "x1": 70, "y1": 80},
                "reason": "テスト"}
        return {"picks": [
            {**item, "frame": nos[0], "text": "軽さ1.2kg\\n本当？", "emphasis": "1.2kg", "badge": "検証", "effect": "focus",
             "layouts": ["vertical", "band"]},
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
    check("デザインもAIに: AIが選んだ組み合わせで作る（縦書きも）", info[0]["layouts"] == ["vertical", "band", "bottom"]
          and {"thumb_1_vertical.jpg", "thumb_1_band.jpg", "thumb_3_side.jpg"} <= names
          and info[2]["layouts"] == ["side", "vertical", "bottom"], str([i["layouts"] for i in info]))
    check("デザインもAIに: 決めたデザインを thumbnails.json に残す",
          (info[0]["emphasis"], info[0]["badge"], info[0]["effect"]) == ("1.2kg", "検証", "focus")
          and info[2]["effect"] == "depth", str([(i.get("emphasis"), i.get("badge"), i.get("effect")) for i in info]))
    check("デザインもAIに: おかしな答えは使わない（知らない組み合わせはテンプレートの順）",
          (info[1]["emphasis"], info[1]["badge"], info[1]["effect"]) == ("", "", "none")
          and info[1]["layouts"] == ["bottom", "band", "side"], str(info[1]))

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
        ok = seen.get("effort") == "medium" and "emphasis" not in seen["schema"]["properties"]["picks"]["items"]["properties"] \
            and T.ART_PROMPT.splitlines()[0] not in seen["system"] and all("emphasis" not in i for i in info) \
            and all(i["layouts"] == ["bottom", "band", "side"] for i in info)
        check("デザインもAIに: art_director = false なら今までどおり（考える深さはテンプレートの値）", ok,
              f"{seen.get('effort')} {[i['layouts'] for i in info]}")
        T.generate(sd, Template(tdir), "api", "low")
        check("デザインもAIに: その回だけ考える深さを変えられる（thumbs.py --effort）", seen.get("effort") == "low")
    except BaseException as e:  # noqa: BLE001
        check("デザインもAIに: art_director = false なら今までどおり（考える深さはテンプレートの値）", False, repr(e))
    finally:
        rc_llm._client, rc_llm._call = orig

    # AIを使わないとき: 数字だけ強調する
    outs = T.generate(sd, Template(fx / "tpl"), "none", None, "重さ1.2kg\\n本当？")
    info = json.loads((td / "thumbnails.json").read_text(encoding="utf-8"))
    check("AIなし: 数字（単位つき）だけ強調し、バッジ・効果は付けない", all(
        (i.get("emphasis"), i.get("badge"), i.get("effect")) == ("1.2kg", "", "none") for i in info) and len(outs) >= 9,
        str([(i.get("emphasis"), i.get("badge"), i.get("effect")) for i in info]))


def run_all(check, out: Path, fx: Path):
    t_values(check)
    t_emphasis(check)
    t_vertical(check)
    t_badge(check)
    t_cutout(check)
    t_effects(check)
    t_compose_all(check)
    t_generate(check, out, fx)


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
