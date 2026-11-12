"""演出のスタイル（にぎやか / 落ち着いた）の切り替え。

lively（にぎやか）: 今までどおり。大きなテロップ・効果音・ズーム・章カード・字幕のキーワードの色
calm（落ち着いた）: 映像の邪魔をしない編集。
  - テロップは、画面の左上の小さなラベルだけ（「重さ 30g」のような事実。測るたびに下へ積み上がって残る）
  - ズーム・効果音・章カード・字幕のキーワードの色は使わない
  - 字幕は小さめ・細い縁取り。文は AI が読みやすく整える（言い直し・「えー」を削り、単位をそろえる。rc_polish）
  - カットは言葉の間を少し残す

決め方: 演出プランに書いてあるもの（"look"）> 環境変数 ROUGHCUT_LOOK（GUI・--look）> テンプレートの [style] look > lively
"""
from __future__ import annotations

import os
import re

import rc_graphics

LOOKS = {
    "lively": ("にぎやか（大きなテロップ・効果音・ズーム）", "YouTubeらしい、にぎやかな演出。大きなテロップ・効果音・ズーム・章カードを使います"),
    "calm": ("落ち着いた（小さなラベルと控えめな字幕）", "映像の邪魔をしない編集。テロップは左上の小さなラベルだけ、字幕は小さめで、"
             "文をAIが読みやすく整えます。ズーム・効果音・章カードは使わず、言葉の間を少し残します"),
}
ENV = "ROUGHCUT_LOOK"
LABEL = "label"   # 小さなラベルのテロップの種類
# テンプレートの [style] に書いていない値は、ここの値を使う
DEFAULTS = {
    "look": "lively",
    "caption_scale": 0.7,        # 落ち着いた: 字幕の大きさ（テンプレートの字幕に対する倍率）
    "caption_outline": 3,        # 落ち着いた: 字幕の縁取りの太さ
    "polish_captions": True,     # 落ち着いた: 字幕の文をAIが整える
    "label_size": 34, "label_color": "#FFFFFF", "label_name_color": "#C9D6E2", "label_fill": "#14181D",
    "label_opacity": 0.78, "label_margin": 36, "label_gap": 8,
    "label_max": 4,              # 同時に出すラベルの数
    "label_hold": 45.0,          # ラベルを出しておく長さの上限（秒）。章が変わったら消える
    "pad_before": 0.1, "pad_after": 0.2, "merge_gap": 0.35,   # 落ち着いた: カットで残す間（今の値に足す秒）
}


def config(tpl=None) -> dict:
    c = dict(DEFAULTS)
    if tpl is not None:
        c.update(tpl.get("style", {}) or {})
    return c


def current(tpl=None, plan: dict | None = None) -> str:
    for v in ((plan or {}).get("look"), os.environ.get(ENV), config(tpl)["look"]):
        v = str(v or "").strip().lower()
        if v in LOOKS:
            return v
    return "lively"


def activate(look: str) -> None:
    """このあとの処理（演出プランのプロンプトなど）が同じスタイルを使うようにする"""
    os.environ[ENV] = look


def is_calm(tpl=None, plan: dict | None = None) -> bool:
    return current(tpl, plan) == "calm"


def cut_preset(preset: dict, tpl=None) -> dict:
    """荒編集のカットの強さ。落ち着いたスタイルでは、言葉の前後の間を少し残す"""
    p = dict(preset)
    if is_calm(tpl):
        c = config(tpl)
        for k in ("pad_before", "pad_after", "merge_gap"):
            p[k] = float(p.get(k, 0)) + float(c[k])
    return p


def apply_template(tpl, look: str) -> None:
    """落ち着いたスタイルのとき、テンプレートの字幕を小さめ・細い縁取りにする（tpl を書き換える。ファイルは変えない）"""
    if look != "calm":
        return
    c = config(tpl)
    cap = dict(tpl["caption"])
    cap["size"] = max(24, round(float(cap.get("size", 66)) * float(c["caption_scale"])))
    cap["outline"] = min(float(cap.get("outline", 4)), float(c["caption_outline"]))
    cap["keyword_color"] = ""
    cap["margin_bottom"] = round(float(cap.get("margin_bottom", 56)) * 0.8)
    cap["max_chars"] = min(32, round(int(cap.get("max_chars", 24)) * 1.25))   # 字が小さいぶん、1枚に入る字数を増やす
    tpl.cfg["caption"] = cap


# ---------------------------------------------------------------- 演出プラン
PLAN_TELOPS = """- telops: **小さなラベルだけ**を作ります（大きな強調テロップ・吹き出し・表は使いません）
  - 画面の左上に小さく出て、その話題のあいだ残ります。測った値は下へ積み上がります（太さ → 重さ → 全長）
  - style は必ず "label"、se は必ず "none"
  - text は「項目：値」の形（例: 「太さ：11.7mm」「重さ：30g」「価格：9,900円（収録時点）」「素材：ベニシタン」）。20字以内
  - 載せるのは、画面に残しておくと見る人の役に立つ**事実**だけ（測った数値・価格・型番・素材・評価の点数など）。
    感想・強調・ツッコミは載せない。発言に出てきた値だけを使う（推測しない）。単位は記号で（ミリ → mm、グラム → g）
  - 同じ項目は1回だけ。1本に4〜12個。start_id = end_id = その値を言っている発言
- zooms: 使いません。必ず空の配列 [] にする"""
PLAN_KEYWORDS = "- caption_keywords: 使いません。必ず空の配列 [] にする"


def plan_instructions(text: str) -> str:
    """rc_plan の指示文を、落ち着いたスタイル用に差し替える（テロップ・ズーム・キーワードの部分）"""
    a, b, c = text.find("- telops:"), text.find("- bgm:"), text.find("- caption_keywords:")
    if a < 0 or b < a or c < b:
        return text
    end = text.find("\n\n", c)
    end = len(text) if end < 0 else end
    head = text[:a].replace("字幕はすべての発言に自動で付くので、テロップは字幕とは別の「強調」です",
                            "字幕はすべての発言に自動で付きます")
    return (head + "この動画は**落ち着いた編集**です。映像と話を邪魔しないことを最優先にします。\n" + PLAN_TELOPS + "\n"
            + text[b:c] + PLAN_KEYWORDS + text[end:])


def clean_plan(plan: dict, look: str) -> dict:
    """落ち着いたスタイルの決まりを守らせる（AIが大きなテロップやズームを返しても使わない）"""
    plan["look"] = look
    if look != "calm":
        return plan
    out = []
    for t in plan.get("telops", []):
        if t.get("style") in ("info", LABEL):   # 補足情報は、小さなラベルとして出す
            out.append({"start_id": t["start_id"], "end_id": t.get("end_id", t["start_id"]),
                        "text": str(t.get("text", "")).strip(), "style": LABEL, "se": "none"})
    plan["telops"] = [t for t in out if t["text"]]
    plan["zooms"] = []
    plan.pop("caption_keywords", None)
    return plan


# ---------------------------------------------------------------- 小さなラベル
def schedule_labels(labels, chapter_times, end: float, cfg: dict) -> list[tuple[float, float, str, int]]:
    """labels: [(出す時刻, 文字)] → [(開始, 終了, 文字, 上から何番目)]

    その章が終わるまで（最長 label_hold 秒）出しておき、あとから出たものは下へ積む。数が多すぎたら古いものから消す。
    """
    hold, cap = float(cfg["label_hold"]), max(1, int(cfg["label_max"]))
    out: list[list] = []
    for s, text in sorted(labels, key=lambda x: x[0]):
        if s >= end - 0.5:
            continue
        nxt = min([t for t in chapter_times if t > s + 0.5] or [end])
        e = min(nxt - 0.3, s + hold, end)
        if e - s < 2.0:
            e = min(s + 4.0, end)
        live = [o for o in out if o[1] > s + 0.05]
        while len(live) >= cap:               # 多すぎるときは、いちばん古いものを消す
            old = min(live, key=lambda o: o[0])
            old[1] = s
            old[4] = None   # 途中で消したものは、あとで延ばさない
            live.remove(old)
        used = {o[3] for o in live}
        slot = next(k for k in range(cap + 1) if k not in used)
        out.append([s, e, text, slot, nxt])
    for o in out:
        if o[4] is None:
            continue
        # 同じ章のラベルは、いっしょに消す（積み上がった上のものだけ先に消えて、すき間ができないように）
        group = [x for x in out if x[4] == o[4] and x[1] > o[0] and abs(x[0] - o[0]) < hold]   # いっしょに消す
        o[1] = min(max(x[1] for x in group), o[0] + hold * 2, end)
    out = [o[:4] for o in out]
    return [tuple(o) for o in out if o[1] - o[0] > 0.3]


_SPLIT = re.compile(r"[：:]")


def label_styles(tpl=None, h: int = 1080) -> list[str]:
    """ラベル用の ASS スタイル。Label = 地の帯（文字は透明）/ LabelTx = 上に重ねる文字"""
    from rc_template import ass_color
    c = config(tpl)
    k = h / 1080
    fs = round(float(c["label_size"]) * k)
    font = ((tpl.get("caption", {}) if tpl else {}) or {}).get("font", rc_graphics.FONT)
    fill = ass_color(c["label_fill"], round(255 * (1 - float(c["label_opacity"]))))
    pad = round(fs * 0.32)
    head = f"{font},{fs},&HFF000000,&HFF000000"
    return [f"Style: Label,{head},{fill},&HFF000000,-1,0,0,0,100,100,0,0,3,{pad},0,7,0,0,0,1",
            f"Style: LabelTx,{head},&HFF000000,&HFF000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"]


def label_events(labels, w: int, h: int, tpl=None, top: float = 0) -> list[str]:
    """schedule_labels の結果を、左上の小さなラベル（ASS のイベント）にする。「項目：値」は項目を少し薄い色に

    帯は、文字を透明にした1枚で描く（文字の幅を測らなくてよく、色の違う言葉ごとに帯が重なることもない）
    """
    c = config(tpl)
    k = h / 1080
    fs = float(c["label_size"]) * k
    pad, gap, m = round(fs * 0.32), float(c["label_gap"]) * k, float(c["label_margin"]) * k
    bh = fs + pad * 2
    t, col = rc_graphics._t, rc_graphics._col
    ev = []
    for s, e, text, slot in labels:
        parts = _SPLIT.split(str(text), maxsplit=1)
        name, value = (parts[0].strip(), parts[1].strip()) if len(parts) == 2 and parts[0].strip() else ("", str(text).strip())
        plain = rc_graphics._esc(f"{name}  {value}" if name else value)
        pos = f"BSan7BSpos({m + pad:.0f},{m + pad + top + slot * (bh + gap):.0f})BSfad(200,200)".replace("BS", chr(92))
        ev.append(f"Dialogue: 6,{t(s)},{t(e)},Label,,0,0,0,,{{{pos}}}{plain}")
        front = ("{BS1a&H00&BS1c" + col(c["label_name_color"]) + "}" + rc_graphics._esc(name) + "  " if name else "")             + "{BS1a&H00&BS1c" + col(c["label_color"]) + "}" + rc_graphics._esc(value)
        ev.append(f"Dialogue: 7,{t(s)},{t(e)},LabelTx,,0,0,0,,{{{pos}}}" + front.replace("BS", chr(92)))
    return ev


def add_events(ass: str, events: list[str], styles: list[str] | None = None) -> str:
    """できあがった ASS にイベント（と、そのためのスタイル）を足す"""
    if not events:
        return ass
    if styles and "[Events]" in ass:
        head, tail = ass.split("[Events]", 1)
        ass = head.rstrip("\n") + "\n" + "\n".join(styles) + "\n\n[Events]" + tail
    return ass.rstrip("\n") + "\n" + "\n".join(events) + "\n"
