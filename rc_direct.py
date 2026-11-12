"""演出のディレクター: テロップとズームの「見せ方」を、映像を見て決める。

演出プラン（rc_plan）は文字起こしの文字だけで作るので、画面のどこが空いているか・何が写っているかを知らない。
ここでは、テロップとズームの場面の静止画を Claude に見せて、次を決めてもらう（plan の各項目に書き足す）:
  テロップ: position（置く場所）/ emphasis（強調する言葉）/ size（大きさ）/ animation（出方）
  ズーム:   focus（寄る中心。画面の左上を 0,0・右下を 100,100 とした%）。寄ると大事な所が切れるなら、ズームをやめる

AIを使わないとき・古いプランでも、telop_opts() が安全な値にして返す（数字だけは自動で強調する）。
テンプレートの [direction] の vision = false でやめられる。
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

# 文字だけのテロップを置ける場所（字幕は画面の下に出るので、bottom-* は字幕のすぐ上）
POSITIONS = ("top", "top-left", "top-right", "left", "right", "bottom-left", "bottom-right", "center")
# カード型（吹き出し・スペック表など）を置ける場所
CARD_POSITIONS = ("right", "left", "top", "middle", "top-left", "top-right")
ANIMATIONS = ("pop", "slide", "fade", "drop", "zoom", "shake")
SIZES = {"small": 0.85, "normal": 1.0, "large": 1.22}
EMPH_SCALE = 1.22      # 強調する言葉の大きさ（まわりの字に対する倍率）
EMPH_SCALE_2LINES = 1.1   # 2行のテロップでの倍率（大きくすると上下の行に重なるので控えめに）
EMPH_MAX = 10          # 強調する言葉の長さの上限
MAX_FRAMES = 40        # AIに見せる静止画の数の上限
FRAME_W, FRAME_H = 640, 360
POSITION_JA = {"top": "上", "top-left": "左上", "top-right": "右上", "left": "左", "right": "右",
               "bottom-left": "左下", "bottom-right": "右下", "center": "中央", "middle": "中央"}
ANIMATION_JA = {"pop": "ポンッ", "slide": "横からスッ", "fade": "ふわっ", "drop": "上から落ちる", "zoom": "ドンと寄る",
                "shake": "揺れる"}
SIZE_JA = {"small": "小さめ", "normal": "", "large": "大きめ"}

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["telops", "zooms"],
    "properties": {
        "telops": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["no", "position", "emphasis", "size", "animation"],
            "properties": {
                "no": {"type": "integer", "description": "テロップの番号（画像の前に書いてある番号）"},
                "position": {"type": "string", "enum": sorted(set(POSITIONS) | set(CARD_POSITIONS)),
                             "description": "置く場所。写っている大事な物・顔・画面の中の文字に重ならない所"},
                "emphasis": {"type": "string", "description": "文字の中で、色と大きさを変えて目立たせる言葉（文字にそのまま含まれる2〜6字。なければ空）"},
                "size": {"type": "string", "enum": list(SIZES)},
                "animation": {"type": "string", "enum": list(ANIMATIONS)},
            }}},
        "zooms": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["no", "x", "y", "keep"],
            "properties": {
                "no": {"type": "integer", "description": "ズームの番号（画像の前に書いてある番号）"},
                "x": {"type": "integer", "description": "寄る中心の横の位置（左端0〜右端100）"},
                "y": {"type": "integer", "description": "寄る中心の縦の位置（上端0〜下端100）"},
                "keep": {"type": "boolean", "description": "寄ってよければ true。寄ると大事な所が切れる・寄る物が無いなら false"},
            }}},
    },
}

PROMPT = """あなたはYouTube動画の演出のディレクターです。編集者が決めたテロップとズームについて、その場面の静止画を見て「見せ方」を決めてください。
動画は横長（16:9）です。発言の字幕は、いつも画面の下の中央（下から約2割の帯）に出ています。

## テロップ（画像の前に「テロップN」と書いてあります）
- position: 置く場所。**その場面で、写っている大事な物（商品・手元・顔・はかりの表示・画面の中の文字）に重ならない、空いている所**を選ぶ
  - 文字だけのテロップ: top=上の中央 / top-left=左上 / top-right=右上 / left=左の中段 / right=右の中段 /
    bottom-left=左下（字幕の上）/ bottom-right=右下（字幕の上）/ center=画面の真ん中（オチ・結論を大きく見せたいときだけ。物を隠すなら使わない）
  - カード型（吹き出し・スペック表・比較表・工程・場所）: right / left / top / middle / top-left / top-right から。物が左にあれば右、のように反対側へ
  - 大事な物が上にあるのに top、のような置き方はしない。どこも空いていなければ、いちばん隠れてよい所
- emphasis: テロップの文字の中で、いちばん効く言葉（数字と単位・商品名・意外な言葉。2〜6字）。色と大きさを変えて目立たせます。
  文字にそのまま含まれる言葉にする。文字全部にはしない。目立たせる言葉が無ければ空。カード型は空
- size: large=結論・オチ・いちばん言いたいこと（1本に数個まで）/ normal=ふつう / small=補足・小さな注釈。写真のすき間がせまい所は small
- animation: 出方。内容に合わせて変化をつける（全部同じにしない）
  - pop=ポンッと弾む（強調・ポイント） / slide=横からスッと入る（補足情報・名前・数値）/ fade=ふわっと出る（落ち着いた説明・しみじみした所）
  - drop=上から落ちてくる（オチ・ツッコミ）/ zoom=大きい所からドンと収まる（驚き・結論のインパクト）/ shake=小さく揺れる（注意・びっくり）
  - カード型は pop / slide / fade のどれか

## ズーム（画像の前に「ズームN」と書いてあります）
- 映像をその発言のあいだ拡大します。x・y は、拡大の中心にしたい物（話題の商品・手元・顔・はかりの表示など）の位置。
  画面の左上が (0, 0)、右下が (100, 100)
- keep: 拡大すると大事な物が画面の外に切れてしまう、または寄って見せる物が特に無い場面なら false（ズームをやめます）

全部のテロップ・ズームについて答えてください。出力はJSONのみです。"""


def enabled(tpl) -> bool:
    v = (tpl.get("direction", {}) or {}).get("vision", True) if tpl is not None else True
    return str(v).strip().lower() not in ("false", "0", "no", "off")


# ---------------------------------------------------------------- 値を整える
_NUM = re.compile(r"[0-9０-９]+(?:[.,．，][0-9０-９]+)?\s*(?:kg|mm|cm|km|mAh|GB|TB|g|m|W|V|%|％|万円|円|万|倍|年|日|時間|分|秒|"
                  r"回|個|本|枚|点|位|人|台|種類|歳|度)")


def auto_emphasis(text: str) -> str:
    """AIを使わないとき: 数字（単位つき）だけ強調する"""
    m = _NUM.search(text or "")
    return m.group(0) if m and m.group(0) != (text or "").strip() else ""


def telop_opts(t: dict, card: bool = False) -> dict:
    """テロップ1つの見せ方（position / emphasis / size / animation）。おかしな値は使わない（空 = テンプレートのとおり）"""
    text = str(t.get("text", ""))
    pos = str(t.get("position") or "")
    if pos not in (CARD_POSITIONS if card else POSITIONS):
        pos = ""
    if card:
        anim = str(t.get("animation") or "")
        return {"position": pos, "emphasis": "", "size": "normal", "animation": anim if anim in ("pop", "slide", "fade") else ""}
    if "emphasis" in t:
        em = str(t.get("emphasis") or "").strip()
        if not em or em not in text or em == text.strip() or len(em) > EMPH_MAX:
            em = ""
    else:
        em = auto_emphasis(text)
    size = str(t.get("size") or "normal")
    anim = str(t.get("animation") or "")
    return {"position": pos, "emphasis": em, "size": size if size in SIZES else "normal",
            "animation": anim if anim in ANIMATIONS else ""}


def zoom_focus(z: dict) -> tuple[float, float] | None:
    """ズームの中心（0〜1）。決まっていなければ None（画面の真ん中）"""
    f = z.get("focus")
    try:
        x, y = float(f[0]), float(f[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return min(max(x, 0.0), 100.0) / 100, min(max(y, 0.0), 100.0) / 100


def describe(t: dict, card: bool = False) -> str:
    """画面の表に出す短い説明（例: 右上・強調「30g」・大きめ・ポンッ）"""
    o = telop_opts(t, card)
    parts = [POSITION_JA.get(o["position"], "")]
    if o["emphasis"] and "emphasis" in t:
        parts.append(f"強調「{o['emphasis']}」")
    parts += [SIZE_JA.get(o["size"], ""), ANIMATION_JA.get(o["animation"], "")]
    return "・".join(p for p in parts if p)


# ---------------------------------------------------------------- 静止画
def _grab(path: Path, t: float, out: Path) -> bool:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,"
                               f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2", "-q:v", "4", str(out)], capture_output=True)
    return p.returncode == 0 and out.exists()


def _frame(src, t: float, out: Path) -> bool:
    parts = src.split(t, t + 0.05)
    if not parts:
        return False
    fi, local, _ = parts[0]
    return _grab(src.files[fi].path, local, out)


def _image_block(path: Path) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(path.read_bytes()).decode()}}


def build_content(plan: dict, by_id: dict, src, work: Path, cards=()) -> tuple[list[dict], dict, dict]:
    """AIに見せる内容（番号つきのテロップ・ズームと、その場面の静止画）。(content, {番号: テロップ}, {番号: ズーム})"""
    work.mkdir(parents=True, exist_ok=True)
    content, telops, zooms, n = [], {}, {}, 0
    for k, t in enumerate(plan.get("telops", []), 1):
        c = by_id.get(t.get("start_id"))
        if c is None or n >= MAX_FRAMES:
            continue
        f = work / f"telop_{k:03d}.jpg"
        if not _frame(src, c.start + min(1.0, max(0.1, (c.end - c.start) / 2)), f):
            continue
        n += 1
        kind = "カード型・" if t.get("style") in cards else ""
        text = str(t.get("text", "")).replace("\n", "／")
        content.append({"type": "text", "text": f"テロップ{k}（{kind}{t.get('style', 'emphasis')}）: 「{text}」"})
        content.append(_image_block(f))
        telops[k] = t
    for k, z in enumerate(plan.get("zooms", []), 1):
        c = by_id.get(z.get("id"))
        if c is None or n >= MAX_FRAMES:
            continue
        f = work / f"zoom_{k:03d}.jpg"
        if not _frame(src, (c.start + c.end) / 2, f):
            continue
        n += 1
        content.append({"type": "text", "text": f"ズーム{k}（発言: 「{str(c.text)[:40]}」）"})
        content.append(_image_block(f))
        zooms[k] = z
    return content, telops, zooms


def apply(plan: dict, res: dict, telops: dict, zooms: dict, cards=()) -> tuple[int, int, int]:
    """AIの答えを plan に書き足す。(見せ方を決めたテロップの数, 中心を決めたズームの数, やめたズームの数)"""
    nt = nz = 0
    for a in res.get("telops", []) or []:
        t = telops.get(a.get("no"))
        if t is None:
            continue
        t.update({k: a.get(k, "") for k in ("position", "emphasis", "size", "animation")})
        t.update(telop_opts(t, t.get("style") in cards))   # おかしな値はここで落とす
        nt += 1
    drop = []
    for a in res.get("zooms", []) or []:
        z = zooms.get(a.get("no"))
        if z is None:
            continue
        if a.get("keep") is False:
            drop.append(z)
            continue
        try:
            z["focus"] = [int(min(max(float(a.get("x", 50)), 0), 100)), int(min(max(float(a.get("y", 50)), 0), 100))]
            nz += 1
        except (TypeError, ValueError):
            pass
    if drop:
        plan["zooms"] = [z for z in plan.get("zooms", []) if not any(z is d for d in drop)]
    return nt, nz, len(drop)


def direct(out_dir: Path, plan: dict, by_id: dict, src, tpl, llm: str, effort: str, usage: dict, cards=()) -> bool:
    """テロップとズームの見せ方を、映像を見て決める（plan を書き換える）。AIを使わない・設定でやめているときは何もしない"""
    if llm != "api" or not enabled(tpl) or not (plan.get("telops") or plan.get("zooms")):
        return False
    import rc_llm
    work = Path(out_dir) / "work_direct"
    try:
        print("  テロップとズームの場面を取り出しています…", flush=True)
        content, telops, zooms = build_content(plan, by_id, src, work, cards)
        if not telops and not zooms:
            return False
        print(f"  Claudeが映像を見て、テロップの位置・強調・出方とズームの中心を決めています"
              f"（テロップ{len(telops)}・ズーム{len(zooms)}）…", flush=True)
        anthropic, client = rc_llm._client()
        res = rc_llm._call(client, anthropic, PROMPT, content, SCHEMA, effort, usage)
        nt, nz, nd = apply(plan, res, telops, zooms, cards)
        print(f"  見せ方を決めました: テロップ{nt}・ズームの中心{nz}" + (f"・やめたズーム{nd}" if nd else ""))
        return True
    except (Exception, SystemExit) as e:  # noqa: BLE001  決められなくても、テンプレートのとおりの見せ方で作れる
        print(f"  [注意] 映像を見た演出の調整はできませんでした（テンプレートのとおりに出します）: {e}")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)
