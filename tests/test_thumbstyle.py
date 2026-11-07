"""サムネイルの見せ方（上品・ポップ・シンプル）と文字の置き方のテスト（rc_thumb.py）。

- 二重のふちの文字で、画数の多い漢字（難・軸）のすき間や2行の間に、白い点・白いかたまり・写真ののぞく穴が出ない
- 「横に文字」で、端に真っ黒な帯ができない
- 文字は写真の空いている側（AIの希望があればそちら）に置く
- 見せ方の色・フォント、前の版のテンプレート（style なし）は今までの見せ方
- AI に頼む形（見出し・文字の位置）とテンプレートの編集画面
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


class FakeTpl:
    def __init__(self, thumb: dict):
        self.cfg = {"thumbnail": thumb}
        self.dir = ROOT / "templates" / "default"
        self.fonts_dir = None

    def get(self, k, default=None):
        return self.cfg.get(k, default)


def _photo(kind: str = "plain"):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1280, 720), (200, 196, 188))
    d = ImageDraw.Draw(img)
    if kind == "busy_left":   # 左半分に細かい模様（商品や表示のつもり）、右半分は無地
        for x in range(0, 640, 16):
            for y in range(0, 720, 16):
                if (x // 16 + y // 16) % 2:
                    d.rectangle([x, y, x + 15, y + 15], fill=(60, 50, 40))
    elif kind == "object":
        d.rectangle([420, 200, 860, 520], fill=(150, 40, 30))
    return img


def t_style(check):
    import rc_thumb as T
    old = T.text_style(FakeTpl({"text_color": "#FFE14D", "outline_color": "#111111"}))
    check("見せ方: style の無い前の版のテンプレートは、今までの見せ方（ポップ）", old["name"] == "pop" and old["effect"] == "outline")
    el = T.text_style(FakeTpl({"style": "elegant", "text_color": "", "accent_color": ""}))
    check("見せ方: 色が空なら見せ方の色", el["text_color"] == "#FFFFFF" and el["accent_color"] == T.STYLES["elegant"]["accent_color"]
          and el["effect"] == "shadow", str({k: el[k] for k in ("text_color", "accent_color", "effect")}))
    own = T.text_style(FakeTpl({"style": "simple", "text_color": "#FFEE00"}))
    check("見せ方: テンプレートで決めた色を使う", own["text_color"] == "#FFEE00" and own["effect"] == "panel")
    check("見せ方: 知らない見せ方の名前はポップ", T.text_style(FakeTpl({"style": "なにか"}))["name"] == "pop")
    for name in T.STYLES:
        f = T.find_font(FakeTpl({"style": name}))
        ok = Path(f[0]).exists()
        if "VF" in Path(f[0]).name:   # 太さを変えられるフォントは、見せ方の太さで使う
            want = next(w for n, _i, w in T.STYLES[name]["fonts"] if n == Path(f[0]).name)
            ok = ok and T._VF.get(f[0]) == want
        check(f"見せ方: {name} のフォント", ok, f"{Path(f[0]).name} {T._VF.get(f[0], '')}")


def _bad_white(img, bg) -> tuple[int, int]:
    """(外につながらない白いかたまりの数, 文字に囲まれて背景がのぞく穴の数)"""
    import cv2
    a = np.array(img).astype(int)
    white = a.min(axis=2) > 225
    dark = a.max(axis=2) < 60
    yellow = (a[..., 0] > 200) & (a[..., 1] > 170) & (a[..., 2] < 150)
    # 白・黒・黄色のどれでもない所 = 写真（と影）。文字の縁の1画素の中間色は、ならして除く
    back = cv2.morphologyEx((~(white | dark | yellow)).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    flood = np.pad(back, 1, constant_values=1)   # 画面の端の外も「写真」として、そこから外側をたどる
    cv2.floodFill(flood, None, (0, 0), 2)
    outside = (flood[1:-1, 1:-1] == 2).astype(np.uint8)
    near = cv2.dilate(outside, np.ones((9, 9), np.uint8))
    wm = white.astype(np.uint8)
    n, lab = cv2.connectedComponents(wm, connectivity=8)
    touching = set(np.unique(lab[(near > 0) & (wm > 0)]))
    bad_white = sum(1 for i in range(1, n) if i not in touching and (lab == i).sum() > 3)
    holes = ((back > 0) & (outside == 0)).astype(np.uint8)
    nh, hl = cv2.connectedComponents(holes, connectivity=8)
    return bad_white, sum(1 for i in range(1, nh) if (hl == i).sum() > 15)


def t_outline(check):
    import rc_thumb as T
    from PIL import Image
    try:
        import cv2  # noqa: F401
    except ImportError:
        check("ふち: 白い点のテスト（OpenCV が必要）", False, "OpenCV がありません")
        return
    tpl = FakeTpl({"style": "pop"})
    fp, idx = T.find_font(tpl)
    res = []
    for text in ("入手困難な\n木軸", "紅紫檀の\n美しさ", "重さ30gは\n正義か？", "驚異の鬱蒼\n麗しき鷹"):
        bg = (128, 128, 128)
        img = Image.new("RGB", (1280, 720), bg)
        lines = T._lines(text)
        f, size = T._fit(fp, idx, lines, 1100, 560)
        T._draw_outline(img, T._line_pos(lines, f, size, 640, 80, "center"), f, size,
                        (255, 225, 77), (17, 17, 17), (255, 255, 255))
        res.append((text.replace("\n", ""), *_bad_white(img, bg)))
    check("ふち: 漢字のすき間・2行の間に、白い点やかたまりが出ない", all(r[1] == 0 for r in res), str(res))
    check("ふち: 文字に囲まれて写真がのぞく穴が出ない", all(r[2] == 0 for r in res), str(res))


def t_layout(check):
    import rc_thumb as T
    from PIL import Image
    for name in T.STYLES:
        tpl = FakeTpl({"style": name})
        font = T.find_font(tpl)
        errs = []
        for lay in ("bottom", "band", "side"):
            for label, pos in (("", "auto"), ("野原工芸 紅紫檀", "top"), ("とても長い見出しの文字列テスト", "left")):
                try:
                    img = T.compose(_photo("object"), "入手困難な\n木軸", lay, tpl, font, label, pos)
                    if img.size != (1280, 720) or img.mode != "RGB":
                        errs.append((lay, img.size, img.mode))
                except Exception as e:  # noqa: BLE001
                    errs.append((lay, label, repr(e)))
        check(f"合成: {name} の3つのデザイン（見出しあり・なし）", not errs, str(errs[:3]))
    tpl = FakeTpl({"style": "elegant"})
    font = T.find_font(tpl)
    img = np.array(T.compose(Image.new("RGB", (1280, 720), (235, 235, 230)), "入手困難な\n木軸", "side", tpl, font).convert("L"))
    cols = img.mean(axis=0)
    check("横に文字: 端に真っ黒な帯ができない", cols[:24].min() > 25 and cols[-24:].min() > 25,
          f"左端 {cols[:24].min():.0f} / 右端 {cols[-24:].min():.0f}")
    # 左に細かい模様（大事な所）、右は無地 → 文字は右（文字の側を暗くするので、右の方が暗くなる）
    out = np.array(T.compose(_photo("busy_left"), "入手困難な\n木軸", "side", tpl, font).convert("L")).astype(float)
    base = np.array(_photo("busy_left").convert("L")).astype(float)
    dr = base[:, 960:].mean() - out[:, 960:].mean()
    dl = base[:, :320].mean() - out[:, :320].mean()
    check("置き場所: 文字は写真の空いている側に置く（横に文字）", dr > dl + 15, f"右の暗さ {dr:.0f} / 左の暗さ {dl:.0f}")
    out2 = np.array(T.compose(_photo("plain"), "入手困難な\n木軸", "side", tpl, font, "", "left").convert("L")).astype(float)
    check("置き場所: AIが選んだ側（文字の位置）に置く", out2[:, :320].mean() < out2[:, 960:].mean() - 15,
          f"左 {out2[:, :320].mean():.0f} / 右 {out2[:, 960:].mean():.0f}")
    acc = T._accent(_photo("object"), {**T.STYLES["pop"], "name": "pop"})
    check("帯の色: 写真から取る（赤い物の写真なら赤っぽい色）", acc[0] > acc[1] + 40 and acc[0] > acc[2] + 40, str(acc))


def t_subject(check):
    """AIが答えた「大事な所の枠」を、どのデザインでも隠さない"""
    import rc_thumb as T
    sp = T.subject_px({"x0": 55, "y0": 25, "x1": 95, "y1": 75})
    check("大事な所: 枠（%）を画面の座標にする", sp == (704.0, 180.0, 1216.0, 540.0), str(sp))
    bad = [T.subject_px(b) for b in (None, {}, {"x0": 1, "y0": 1, "x1": 2, "y1": 2}, {"x0": 0, "y0": 0, "x1": 100, "y1": 100},
                                     {"x0": "a", "y0": 0, "x1": 50, "y1": 50})]
    check("大事な所: 小さすぎる・ほぼ全部・読めない枠は使わない", bad == [None] * 5, str(bad))
    swapped = T.subject_px({"x0": 90, "y0": 80, "x1": 10, "y1": 20})
    check("大事な所: 逆向きの枠も直して使う", swapped == (128.0, 144.0, 1152.0, 576.0), str(swapped))
    tpl = FakeTpl({"style": "elegant"})
    font = T.find_font(tpl)
    plain = _photo("plain")
    base = np.array(plain.convert("L")).astype(float)

    def gray(img):
        return np.array(img.convert("L")).astype(float)
    # 横に文字: 右に大事な所 → 文字は左（文字の側を暗くするので、左の方が暗くなる）
    g = gray(T.compose(plain, "入手困難な\n木軸", "side", tpl, font, "", "auto", T.subject_px({"x0": 55, "y0": 25, "x1": 95, "y1": 75})))
    g2 = gray(T.compose(plain, "入手困難な\n木軸", "side", tpl, font, "", "auto", T.subject_px({"x0": 5, "y0": 25, "x1": 45, "y1": 75})))
    check("大事な所: 横に文字は、大事な所の反対側に置く",
          g[:, :320].mean() < g[:, 960:].mean() - 15 and g2[:, 960:].mean() < g2[:, :320].mean() - 15,
          f"右に大事な所: 左 {g[:, :320].mean():.0f} 右 {g[:, 960:].mean():.0f} / 左に大事な所: 左 {g2[:, :320].mean():.0f} 右 {g2[:, 960:].mean():.0f}")
    # 大きな文字: 下に大事な所 → 文字は上（白い文字は上半分に多い）
    b = np.array(T.compose(plain, "入手困難な\n木軸", "bottom", tpl, font, "", "auto",
                           T.subject_px({"x0": 10, "y0": 60, "x1": 90, "y1": 95})))
    white = b.min(axis=2) > 245
    check("大事な所: 大きな文字は、大事な所を隠さない上下に置く", white[:360].sum() > white[360:].sum() * 3,
          f"上 {white[:360].sum()} / 下 {white[360:].sum()}")
    # 帯: 上に大事な所 → 帯は下（帯の所が暗くなる）
    bd = gray(T.compose(plain, "入手困難な木軸", "band", tpl, font, "", "auto", T.subject_px({"x0": 0, "y0": 0, "x1": 100, "y1": 35})))
    check("大事な所: 帯は、大事な所の反対側に置く", bd[600:].mean() < bd[:120].mean() - 30,
          f"上 {bd[:120].mean():.0f} / 下 {bd[600:].mean():.0f}（元 {base.mean():.0f}）")


def _ink_height(img, bg) -> int:
    """背景（bg）より明るい所（白・金の文字）の、上下の幅（画素）"""
    a = np.array(img.convert("L")).astype(int)
    rows = np.where((a > sum(bg) / 3 + 50).any(axis=1))[0]
    return int(rows[-1] - rows[0] + 1) if len(rows) else 0


def t_label(check):
    """見出し（商品名など）は、YouTube のスマホ一覧の小さな表示でも読める大きさ"""
    import rc_thumb as T
    from PIL import Image
    small = []
    for name in T.STYLES:
        tpl = FakeTpl({"style": name})
        st = T.text_style(tpl)
        fp, idx = T.find_font(tpl)
        for size in (90, 120, 160):
            f, s = T._label_fit("野原工芸", fp, idx, size, 1152, st)
            if f.size < T.LABEL_MIN or f.size < size * 0.4:
                small.append((name, size, f.size))
    check("見出し: 大きな文字の4割以上・54px以上（前は3割・約36px）", not small, str(small[:3]))

    tpl = FakeTpl({"style": "elegant"})
    st = T.text_style(tpl)
    fp, idx = T.find_font(tpl)
    long = "とても長い見出しの文字列テストです本当に長い"
    f, s = T._label_fit(long, fp, idx, 140, 768, st)
    check("見出し: 長い見出しは幅に収め、入らない分は「…」", f.getbbox(s)[2] <= 768 and s.endswith("…")
          and f.size >= T.LABEL_FLOOR, f"{f.size}px {s}")
    check("見出し: 見出しを付けない設定・空なら付けない",
          T._label_fit("", fp, idx, 140, 768, st) is None and T._label_fit("野原工芸", fp, idx, 140, 768, {**st, "show_label": False}) is None)

    # 実際に描いた字の高さ。幅360（スマホの一覧くらい）に縮めても12px以上
    hs = {}
    for name in T.STYLES:
        tpl = FakeTpl({"style": name})
        st = {**T.text_style(tpl), "_accent": (201, 168, 106)}
        fp, idx = T.find_font(tpl)
        bg = (60, 60, 60)
        img = Image.new("RGB", (1280, 720), bg)
        lab = T._label_fit("野原工芸", fp, idx, 120, 1152, st)
        T._draw_label(img, lab, 640, 300, "center", {**st, "effect": "shadow" if name != "pop" else "outline"},
                      (201, 168, 106) if name != "pop" else (40, 40, 40))
        hs[name] = round(_ink_height(img, bg) * 202 / 720, 1)
    check("見出し: スマホの一覧（幅360）でも字の高さ12px以上", all(h >= 12 for h in hs.values()), str(hs))

    # 上品の飾りの線は、大事な所（ロゴなど）に重なるなら引かない
    st = {**T.text_style(FakeTpl({"style": "elegant"})), "_accent": (201, 168, 106)}
    lab = T._label_fit("野原工芸", fp, idx, 120, 1152, st)
    w = lab[0].getbbox(lab[1])[2]

    def line_px(avoid):
        img = Image.new("RGB", (1280, 720), (60, 60, 60))
        T._draw_label(img, lab, 64, 300, "left", st, (201, 168, 106), avoid)
        a = np.array(img)[:, int(64 + w + 20):]
        return int(((a[..., 0] > 150) & (a[..., 2] < 140)).sum())
    free, blocked = line_px(None), line_px((64 + w + 40, 250, 900, 420))
    check("見出し: 飾りの線は大事な所（ロゴなど）に刺さらない", free > 100 and blocked == 0, f"線あり {free} / 大事な所あり {blocked}")


def t_ai_and_editor(check):
    import rc_design
    import rc_thumb as T
    items = T.PICK_SCHEMA["properties"]["picks"]["items"]
    check("AI: 見出し・文字の位置・大事な所の枠も答えてもらう", {"label", "text_position", "subject_box"} <= set(items["required"])
          and set(items["properties"]["text_position"]["enum"]) == {"top", "bottom", "left", "right", "any"}
          and items["properties"]["subject_box"]["required"] == ["x0", "y0", "x1", "y1"])
    simple = T.pick_simple([{"no": 1, "t": 0.0, "why": "全体から", "sharp": 1.0}], ["テスト"], "")
    check("AIなし: 見出しなし・文字の位置はおまかせ", simple[0]["label"] == "" and simple[0]["text_position"] == "any")
    base = {"caption": {"font": "BIZ UDPGothic"}, "telop": {}, "chapter_card": {}, "thumbnail": {"style": "pop"}}
    preset = next(p for p in rc_design.PRESETS if p["name"] == "落ち着いたネイビー")
    cfg = rc_design.apply_proposal(copy.deepcopy(base), preset)
    check("デザイン案: 案の見せ方をテンプレートに入れる", cfg["thumbnail"].get("style") == "elegant", str(cfg["thumbnail"]))
    sw = dict(rc_design.swatches({"thumbnail": {"style": "elegant"}})) if hasattr(rc_design, "swatches") else None
    if sw is not None:
        check("デザイン案: 色の一覧は見せ方の色", sw.get("サムネ") == "#FFFFFF", str(sw.get("サムネ")))
    import tomllib
    th = tomllib.loads((ROOT / "templates" / "default" / "template.toml").read_text(encoding="utf-8-sig"))["thumbnail"]
    check("テンプレート: いつもの見せ方は上品", th.get("style") == "elegant" and not th.get("text_color"), str(th.get("style")))
    try:
        import gui_ext_design as G
        spec = [s for s in G.SPEC_THUMB if s[0] == "style"]
        check("編集画面: サムネイルのタブで見せ方を選べる", bool(spec) and {v for v, _ in spec[0][3]} == set(T.STYLES))
    except ImportError as e:   # PySide6 が無い環境
        check("編集画面: サムネイルのタブで見せ方を選べる", False, repr(e))


def run_all(check, out: Path, fx: Path):
    t_style(check)
    t_outline(check)
    t_layout(check)
    t_subject(check)
    t_label(check)
    t_ai_and_editor(check)


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
