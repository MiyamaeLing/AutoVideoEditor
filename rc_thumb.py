"""サムネイル案の自動生成。

1. 見どころ・強調テロップ・ズームの場面を優先して、候補の静止画を約12枚取り出す（ブレた画像は除く）
2. Claude に候補画像を見せて、サムネイル向きの3枚と文字を選んでもらう（AIなしならシャープさで選ぶ）。
   デザインもおまかせ（[thumbnail] の art_director）なら、強調する言葉・バッジ・写真の効果・デザインの組み合わせも決めてもらう
3. 3種類のデザインで合成して、1280x720 の JPEG を書き出す（凝った表現は rc_thumbart.py）
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from rc_media import SourceSet, probe
from rc_template import Template
from rc_timeline import Timeline

W, H = 1280, 720
N_CANDIDATES = 12
# YouTube の一覧では、サムネイルの右下に動画の長さ（「12:34」など）が重ねて表示される。
# 文字・見出し・バッジはここ（と、まわりに TIME_GAP の余白）に置かない
TIME_BOX = (W - 240, H - 110, W, H)
TIME_GAP = 10

_BOX_SMALL = {
    "type": "object", "additionalProperties": False, "required": ["x0", "y0", "x1", "y1"],
    "description": "画面の左上を0、右下を100とした%の枠",
    "properties": {k: {"type": "integer", "description": d} for k, d in (
        ("x0", "左端（%）"), ("y0", "上端（%）"), ("x1", "右端（%）"), ("y1", "下端（%）"))}}
_BOX = {**_BOX_SMALL, "description": "文字で隠してはいけない大事な所（顔・商品・ロゴ・表示など）をまとめて囲む枠。"
                                     "画面の左上を0、右下を100とした%"}
PICK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["picks"],
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["frame", "text", "label", "text_position", "subject_box", "keep_boxes", "reason"],
                "properties": {
                    "frame": {"type": "integer", "description": "候補画像の番号"},
                    "text": {"type": "string", "description": "サムネイルの文字。12字以内。改行は\\n（2行まで）"},
                    "label": {"type": "string", "description": "大きな文字の上に添える見出し（商品名・ブランド名など。10字以内。なければ空）"},
                    "text_position": {"type": "string", "enum": ["top", "bottom", "left", "right", "any"],
                                      "description": "大きな文字を置いても、顔・商品・表示などの大事な所が隠れない側"},
                    "subject_box": _BOX,
                    "keep_boxes": {"type": "array", "items": _BOX_SMALL,
                                   "description": "主役のほかに、文字で隠してはいけない所（はかりの表示・ロゴ・顔・ペンなど）を"
                                                  "1つずつぴったり囲む枠。0〜3個"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

# AIにデザインも任せるとき（[thumbnail] の art_director）に、1枚ごとに足して答えてもらう項目
ART_FIELDS = {
    "emphasis": {"type": "string", "description": "文字の中でいちばん目を引かせる言葉（文字にそのまま含まれる2〜5字。なければ空）"},
    "badge": {"type": "string", "description": "角に添えるバッジの言葉（「限定」「入手困難」など6字以内。なければ空）"},
    "effect": {"type": "string", "enum": ["none", "focus", "sticker", "depth"],
               "description": "写真の加工: focus=主役以外を暗く・ぼかす / sticker=主役に白いふち / depth=文字を主役の後ろに / none"},
    "layouts": {"type": "array", "items": {"type": "string", "enum": ["bottom", "band", "side", "vertical"]},
                "description": "この写真に合うデザインを合う順に3つ（bottom=大きな文字 / band=帯 / side=横に文字 / vertical=縦書き）"},
    "shade": {"type": "string", "enum": ["light", "normal", "strong"],
              "description": "文字の後ろを暗くする強さ: light=写真の明るさを活かす / normal / strong=文字をくっきり"},
}
EFFORTS = ("low", "medium", "high")


def pick_schema(art: bool = False) -> dict:
    """AIに答えてもらう形。art=True ならデザイン（強調・バッジ・効果・組み合わせ）も"""
    if not art:
        return PICK_SCHEMA
    import copy
    s = copy.deepcopy(PICK_SCHEMA)
    item = s["properties"]["picks"]["items"]
    item["properties"].update(copy.deepcopy(ART_FIELDS))
    item["required"] = item["required"][:-1] + list(ART_FIELDS) + ["reason"]
    return s


def thumb_effort(tpl) -> str:
    """サムネイルを選ぶAIの考える深さ（テンプレートの [thumbnail] の effort。動画作成の設定とは別。初期値は high）"""
    e = str((tpl.get("thumbnail", {}) or {}).get("effort") or "high").strip().lower()
    return e if e in EFFORTS else "high"


def art_director(tpl) -> bool:
    """AIにデザイン（強調する言葉・バッジ・効果・組み合わせ）も任せるか（[thumbnail] の art_director。初期値は true）"""
    v = (tpl.get("thumbnail", {}) or {}).get("art_director", True)
    return v if isinstance(v, bool) else str(v).strip().lower() not in ("false", "0", "no", "off", "")


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

ART_PROMPT = """デザイン（あなたはアートディレクターも兼ねます。写真ごとに、使う表現を選んでください）:
- emphasis: 文字の中でいちばん目を引かせる言葉（数字・素材名・意外な言葉など2〜5字）。大きく・色を変えて見せます。文字にそのまま含まれる言葉にする。なければ空
- badge: 画面の角に添える短いバッジ（「限定」「入手困難」「検証」「新作」など6字以内）。動画の内容として本当に言えるときだけ。文字・見出しとくり返さない。3枚すべてには付けない。なければ空
- effect: 写真の加工
  focus = 主役以外を暗く・少しぼかして、主役を際立たせる（背景がごちゃごちゃしているとき）
  sticker = 主役を切り抜いて白いふちと影を付ける（主役の色が背景とはっきり違うとき。上品ではやわらかい光になる）
  depth = 大きな文字を主役の後ろに回して奥行きを出す（主役が大きく、背景とはっきり違う色のとき）
  none = 加工しない（写真がそのままで美しいとき）
  切り抜きは大事な所の枠（subject_box）をもとにするので、sticker・depth のときは枠に主役だけを入れる（ほかの物はなるべく含めない）
- shade: 文字を読みやすくするために、文字の後ろ（画面の下半分・右半分など）を暗くする強さ。ふつうは normal。
  明るく爽やかに見せたい写真は light（文字が読める範囲で薄く）、文字をくっきり目立たせたいときは strong
- layouts: 4つのデザインから、この写真に合う3つを合う順に。bottom=大きな文字（上か下） / band=帯（上か下） / side=横に文字（左か右） / vertical=縦書き（上品な見せ方・工芸品・和の題材に合う。縦に空いた所があるとき）
- 3枚の案が同じ組み合わせにならないよう、変化をつける
"""


# ---------------- 作り直し（サムネイル案の画面から、AIに指示する） ----------------

REQ_SHOW = 5   # AIに見せる、これまでの指示の数（新しい順に）
LAY_NAMES = {"bottom": "大きな文字", "band": "帯", "side": "横に文字", "vertical": "縦書き", "person": "人物の切り抜き"}

REQUEST_PROMPT = """作り直し:
- 前回のサムネイル案（画像と内容）と、それを見た人の指示があります。指示を最優先で反映してください
- 指示に関係ない所（場面・文字・見出し・デザイン）は、前回の案をなるべくそのまま保つ（同じ場面なら、その候補番号を選ぶ）
- 3つの案の順番も前回に合わせる（前回の案1を直したものを1番目に）
- 「案2」「真ん中の段」「右端」などは、前回の案の画面での位置です（案1が上の段。各段の画像は、書いてある順に左から並ぶ）
- これまでの指示も守り続ける。今回の指示と食い違うときは今回を優先
- 指示は、決められる項目で反映する。例: 「文字を左に」→ text_position、「別の場面で」→ frame、「言葉を変えて」→ text・label
"""
REQUEST_ART = """  「もっと明るく」→ shade を light（effect は focus 以外に）、「文字をくっきり」→ shade を strong、
  「この並べ方は要らない」→ layouts から外す、「バッジは要らない」→ badge を空に、「切り抜きはやめて」→ effect を none
"""


def _image_block(path_or_img, size=(512, 288)) -> dict:
    im = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
    buf = io.BytesIO()
    im.convert("RGB").resize(size).save(buf, "JPEG", quality=80)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def grid_files(tdir: Path) -> dict[str, list[Path]]:
    """サムネイル案の画像を、サムネイル案の画面に並べる順で {案の番号: [画像]}（段は案の番号順、各段は左から）"""
    rows: dict[str, list[Path]] = {}
    for f in sorted(tdir.glob("thumb_*.jpg")):
        rows.setdefault(f.stem.split("_")[1], []).append(f)
    return dict(sorted(rows.items(), key=lambda kv: (len(kv[0]), kv[0])))


def _row_name(r: int, n: int) -> str:
    names = {2: ["上の段", "下の段"], 3: ["上の段", "真ん中の段", "下の段"]}
    return "" if n < 2 else names.get(n, [f"上から{k + 1}段目" for k in range(n)])[r]


def _col_name(c: int, n: int) -> str:
    if n < 2:
        return ""
    return "左端" if c == 0 else "右端" if c == n - 1 else "真ん中" if n == 3 else f"左から{c + 1}枚目"


def requests_made(tdir: Path) -> list[str]:
    """今のサムネイル案に反映ずみの、AIへの指示（古い順）"""
    try:
        return [str(x) for x in json.loads((tdir / "requests.json").read_text(encoding="utf-8")) if str(x).strip()]
    except (OSError, ValueError, TypeError):
        return []


def _previous(tdir: Path) -> dict:
    """作り直す前のサムネイル案（AIに見せる）。画像は、消す前に読み込んでおく"""
    try:
        info = json.loads((tdir / "thumbnails.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = []
    rows = grid_files(tdir)
    out = []
    for r, (no, files) in enumerate(rows.items()):
        meta = next((i for i in info if str(i.get("image")) == no), {})
        ims = []
        for c, f in enumerate(files):
            lay = f.stem.split("_", 2)[-1]
            where = "・".join(x for x in (_col_name(c, len(files)), LAY_NAMES.get(lay, lay)) if x)
            try:
                ims.append((where, _image_block(f)))
            except OSError:
                pass
        out.append({"no": no, "where": _row_name(r, len(rows)), "meta": meta, "images": ims})
    return {"rows": out, "history": requests_made(tdir)}


def _request_content(request: str, prev: dict | None, cands) -> list[dict]:
    """AIに見せる、前回の案と指示"""
    blocks = []
    rows = (prev or {}).get("rows") or []
    if rows:
        blocks.append({"type": "text", "text": "ここからは前回のサムネイル案です（この画面を見て指示しています）。"})
    for row in rows:
        m = row["meta"]
        t = m.get("source_time")
        no = next((c["no"] for c in cands if t is not None and abs(c.get("t", -99) - float(t)) < 0.05), None)
        text = str(m.get("text", "")).replace("\n", "\\n")   # 答えと同じ書き方（2行は \n で区切る）
        d = [f"場面: 候補{no}" if no else "場面: 今回の候補にない場面", f"文字「{text}」"]
        if m.get("label"):
            d.append(f"見出し「{m['label']}」")
        d.append(f"text_position: {m.get('text_position', 'any')}")
        d += [f"{k}: {m[k] or '（なし）'}" for k in ("emphasis", "badge", "effect", "shade") if k in m]
        if row["images"]:
            d.append("左から " + "、".join(w for w, _b in row["images"]))
        name = f"前回の案{row['no']}" + (f"（{row['where']}）" if row["where"] else "")
        blocks.append({"type": "text", "text": f"{name}: " + "／".join(d)})
        for where, block in row["images"]:
            blocks.append({"type": "text", "text": f"前回の案{row['no']}" + (f"・{where}" if where else "")})
            blocks.append(block)
    hist = ((prev or {}).get("history") or [])[-REQ_SHOW:]
    if hist:
        blocks.append({"type": "text", "text": "これまでの指示（古い順。前回の案はこれを反映ずみ。引き続き守る）:\n"
                       + "\n".join(f"- {h}" for h in hist)})
    blocks.append({"type": "text", "text": f"今回の指示: {request}"})
    return blocks


def pick_with_ai(cands, title: str, text_ideas: list[str], mode: str, effort: str, style: str = "", art: bool = False,
                 request: str = "", prev: dict | None = None):
    """Claude に候補画像を見せて3枚を選んでもらう。art=True なら、デザイン（強調・バッジ・効果・組み合わせ）も決めてもらう。
    request: 作り直すときの指示（prev: 前回の案。_previous）"""
    import rc_llm
    anthropic, client = rc_llm._client()
    content = []
    for c in cands:
        note = f"／{c['note']}" if c.get("note") else ""   # 顔の様子（rc_thumbspro）
        content.append({"type": "text", "text": f"候補{c['no']}（{c['why']}{note}）"})
        content.append(_image_block(c["path"]))
    content.append({"type": "text", "text": f"動画のタイトル案: {title}\n文字の案: {'、'.join(text_ideas) or '（なし）'}"})
    if request:
        content += _request_content(request, prev, cands)
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
隠してはいけない所の枠（keep_boxes）:
- 大事な所の枠の中で、特に隠してはいけない物（はかりの表示・ロゴ・顔・ペンなど）を1つずつ、ぴったり囲む（0〜3個）。
  斜めに置いた細長い物は、物の向きに沿って2つに分けて囲んでもよい。文字はこれを避け、枠の中の平らな所（台・箱の面など）には置いてよい
- YouTube では画面の右下（横の右から2割・縦の下から15%くらい）に動画の長さが重ねて表示されて隠れる。大事な所がそこに来る画像は避ける
{ART_PROMPT if art else ""}{(REQUEST_PROMPT + (REQUEST_ART if art else "")) if request else ""}出力はJSONのみです。"""
    usage = {"in": 0, "out": 0}
    res = rc_llm._call(client, anthropic, system, content, pick_schema(art), effort, usage)
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


EMPH_SCALE = 1.3   # 強調する言葉の大きさ（ほかの文字に対する倍率）


class G(NamedTuple):
    """文字を置く1か所（_mask で描く）。今までの (x, y, 文字) の形のままでも使える"""
    x: float
    y: float
    s: str
    font: object = None    # None なら _mask に渡したフォント
    li: int = 0            # 何行目（縦書きは何列目）か。ふちの穴埋めを行ごとにするため
    emph: bool = False     # 強調する言葉
    anchor: str = "la"     # Pillow の anchor（縦書きは "mm" = 字の真ん中）
    rot: int = 0           # 時計回りに回す角度（縦書きの「ー」など）


def _emph_runs(lines, emphasis: str = "") -> list[list[tuple[str, bool]]]:
    """各行を [(文字, 強調か)] に分ける。強調する言葉は、最初に見つかった1か所だけ（見つからなければ強調なし）"""
    e = (emphasis or "").strip()
    out, done = [], not e or e == "".join(lines)
    for s in lines:
        k = -1 if done else s.find(e)
        if k < 0:
            out.append([(s, False)])
            continue
        done = True
        out.append([(t, em) for t, em in ((s[:k], False), (e, True), (s[k + len(e):], False)) if t])
    return out


LINE_GAP = 0.12   # 行と行の、見た目の字（ベースラインより下に出る g・y なども含む）の間の最小のすき間（文字の大きさに対する割合）


def _rich(lines, font, size, emphasis: str = ""):
    """強調する言葉がある行の並び [(行の幅, 行の高さ, いちばん高い字の上からベースラインまで, [(文字, フォント, 強調か, 行頭からの位置)])]。
    強調が無ければ None（今までどおりの並べ方）。
    大きくした強調の言葉の「g」などが次の行の字に重ならないよう、見た目の字の上下で行の間を空ける"""
    runs = _emph_runs(lines, emphasis)
    if not any(em for r in runs for _, em in r):
        return None
    ef = _font(font.path, font.index, int(size * EMPH_SCALE))
    rows, inks = [], []
    for r in runs:
        fs = [ef if em else font for _, em in r]
        asc = max(f.getmetrics()[0] for f in fs)
        items, x = [], 0.0
        for (s, em), f in zip(r, fs):
            items.append((s, f, em, x))
            x += f.getlength(s)
        rows.append([items[-1][3] + fs[-1].getbbox(r[-1][0])[2], int(max(f.size for f in fs) * 1.15), asc, items])
        boxes = [f.getbbox(s, anchor="ls") for (s, _em), f in zip(r, fs) if s.strip()]   # ベースラインからの上下
        inks.append((min(b[1] for b in boxes), max(b[3] for b in boxes)) if boxes else (0, 0))
    gap = size * LINE_GAP
    for i in range(len(rows) - 1):   # 次の行の字の上が、この行の字の下より gap 以上下になるように
        need = rows[i][2] + inks[i][1] + gap - (rows[i + 1][2] + inks[i + 1][0])
        rows[i][1] = max(rows[i][1], int(np.ceil(need)))
    return [tuple(r) for r in rows]


def _text_pos(lines, font, size, x, y, align, emphasis: str = ""):
    """文字の置き場所。強調する言葉は大きくし、行の中でベースラインをそろえる"""
    rows = _rich(lines, font, size, emphasis)
    if rows is None:
        return _line_pos(lines, font, size, x, y, align)
    out, top = [], y
    for li, (w, lh, asc, items) in enumerate(rows):
        x0 = x - w / 2 if align == "center" else (x - w if align == "right" else x)
        for s, f, em, dx in items:
            out.append(G(x0 + dx, top + asc - f.getmetrics()[0], s, f, li, em))
        top += lh
    return out


def _text_wh(lines, font, size, emphasis: str = "") -> tuple[float, float]:
    """文字のまとまりの幅と高さ（見出しを除く）"""
    rows = _rich(lines, font, size, emphasis)
    if rows is None:
        return max(font.getbbox(s)[2] for s in lines), int(size * 1.15) * len(lines)
    return max(r[0] for r in rows), sum(r[1] for r in rows)


def _fit(font_path, idx, lines, max_w, max_h, start=170, pad=0.2, emphasis: str = ""):
    """枠（max_w × max_h）に収まる、いちばん大きい文字。pad: ふち・影の分の余白（文字の大きさに対する割合）。
    emphasis: 大きくする言葉（その分も入れて収める）"""
    size = start
    while size > 30:
        f = _font(font_path, idx, size)
        rows = _rich(lines, f, size, emphasis) if emphasis else None
        if rows is None:
            widths = [f.getbbox(s)[2] + 2 * int(size * pad) for s in lines]
            height = size * 1.15 * len(lines)
        else:
            widths = [r[0] + 2 * int(size * pad) for r in rows]
            height = sum(r[1] for r in rows)
        if max(widths) <= max_w and height <= max_h:
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


def _mask(pos, font, stroke: int = 0, only: bool | None = None) -> Image.Image:
    """文字の形（白）。only=True なら強調する言葉だけ、False なら強調以外だけ"""
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    for p in pos:
        g = p if isinstance(p, G) else G(*p)
        if only is not None and g.emph != only:
            continue
        f = g.font or font
        if g.rot:
            _rotated(m, g, f, stroke)
        else:
            d.text((g.x, g.y), g.s, font=f, fill=255, stroke_width=stroke, stroke_fill=255, anchor=g.anchor)
    return m


def _rotated(m: Image.Image, g: G, f, stroke: int):
    """回した字（縦書きの「ー」や括弧など）を、(g.x, g.y) を真ん中にして描く"""
    b = f.getbbox(g.s, anchor="mm", stroke_width=stroke)
    r = int(max(abs(v) for v in b)) + 4
    t = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(t).text((r, r), g.s, font=f, fill=255, stroke_width=stroke, stroke_fill=255, anchor="mm")
    t = t.rotate(-g.rot, resample=Image.BICUBIC)   # Pillow の rotate は反時計回り
    m.paste(255, (int(round(g.x - r)), int(round(g.y - r))), t)


def _glyphs(pos, font) -> list[tuple[float, float, float, float]]:
    """文字1字ずつの枠（文字を主役の後ろに回すとき、隠れすぎる字がないかを調べる）"""
    out = []
    for p in pos:
        g = p if isinstance(p, G) else G(*p)
        f = g.font or font
        if g.anchor == "mm":   # 縦書き: 1つの置き場所が1字分
            b = f.getbbox(g.s, anchor="mm")
            if g.rot:
                b = (b[1], b[0], b[3], b[2])
            out.append((g.x + b[0], g.y + b[1], g.x + b[2], g.y + b[3]))
            continue
        for k, ch in enumerate(g.s):
            if ch.strip():
                b = f.getbbox(ch)
                dx = g.x + f.getlength(g.s[:k])
                out.append((dx + b[0], g.y + b[1], dx + b[2], g.y + b[3]))
    return out


def time_hit(box) -> bool:
    """枠が、右下の再生時間の表示（と余白）にかかるか"""
    return bool(box) and box[2] > TIME_BOX[0] - TIME_GAP and box[3] > TIME_BOX[1] - TIME_GAP


def _ink(st: dict) -> float:
    """字の外にはみ出すふち・影の幅（文字の大きさに対する割合）"""
    return 0.2 if st.get("effect") == "outline" else 0.08


def _time_boxes(pos, font, grow: float):
    return [(b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow) for b in _glyphs(pos, font)]


def _time_lift(pos, font, grow: float = 0.0) -> float:
    """文字（ふち・影の幅 grow を含む）が右下の再生時間の表示にかかるとき、上へずらす量（かからなければ 0）"""
    hit = [b for b in _time_boxes(pos, font, grow) if time_hit(b)]
    return max(b[3] for b in hit) - (TIME_BOX[1] - TIME_GAP) if hit else 0.0


def _time_shift(pos, font, grow: float = 0.0) -> float:
    """文字（ふち・影の幅 grow を含む）が右下の再生時間の表示にかかるとき、左へずらす量（かからなければ 0）"""
    hit = [b for b in _time_boxes(pos, font, grow) if time_hit(b)]
    return max(b[2] for b in hit) - (TIME_BOX[0] - TIME_GAP) if hit else 0.0


def _rows(pos) -> list[list]:
    """行（縦書きは列）ごとにまとめる"""
    if not pos or not isinstance(pos[0], G):
        return [[p] for p in pos]
    out: dict[int, list] = {}
    for p in pos:
        out.setdefault(p.li, []).append(p)
    return list(out.values())


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


def _draw_outline(img, pos, font, size, fill, inner, outer, emph=None):
    """太い文字に二重のふち（内側 inner・外側 outer）。外側の白いふちが漢字のすき間にはみ出して白い点に見えないよう、
    内側のふちの中の穴はふちの色で埋める。emph: 強調する言葉の文字の色"""
    ri, ro = max(2, int(size * 0.11)), max(3, int(size * 0.2))
    m = _mask(pos, font)
    s = Image.new("L", (W, H), 0)
    k = max(3, int(size * 0.05)) | 1
    for p in _rows(pos):   # 行ごとに埋める（2行の間のすき間まで穴として埋めないように）
        # 文字と文字の間の細いすき間を先にふさぐ（ふさがないと、外側の白いふちが小さな白い欠けとして見える）
        line = _mask(p, font, ri).filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))
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
    _paint_emph(img, pos, font, emph)


def _paint_emph(img, pos, font, emph):
    """強調する言葉だけ、強調の色で塗り直す"""
    if emph and any(isinstance(p, G) and p.emph for p in pos):
        _paint(img, emph, _mask(pos, font, only=True))


def _draw_shadow(img, pos, font, size, fill, inner, emph=None):
    """ふちを付けず、柔らかい影で読ませる（上品）。明るい所でも読めるよう、ごく細い縁も付ける"""
    m = _mask(pos, font)
    sh = _mask(pos, font, max(1, int(size * 0.04))).filter(ImageFilter.GaussianBlur(max(3, size * 0.12)))
    _paint(img, (0, 0, 0), ImageChops.offset(sh, int(size * 0.03), int(size * 0.06)), 0.85)
    _paint(img, inner, _mask(pos, font, max(1, int(size * 0.025))), 0.55)
    _paint(img, fill, m)
    _paint_emph(img, pos, font, emph)


def _draw_text(img, lines, font, size, x, y, align, colors):
    """今までの見せ方（太い文字に二重のふち）。colors = (文字, 内側のふち, 外側のふち)"""
    _draw_outline(img, _line_pos(lines, font, size, x, y, align), font, size, *colors)


def _draw_styled(img, lines, font, size, x, y, align, st: dict, emphasis: str = "", pos=None):
    """見せ方（st = text_style()）に合わせて文字を描く。panel（シンプル）は文字の後ろに半透明の板を敷く。
    emphasis: 大きく・強調の色にする言葉 / pos: 置き場所が決まっているとき（縦書き）"""
    if pos is None:
        pos = _text_pos(lines, font, size, x, y, align, emphasis)
    fill, inner, outer = _hex(st["text_color"]), _hex(st["outline_color"], (0, 0, 0)), st.get("outer_color")
    emph = _hex(st.get("emphasis_color"), (255, 210, 74)) if any(isinstance(p, G) and p.emph for p in pos) else None
    if st["effect"] == "outline":
        _draw_outline(img, pos, font, size, fill, inner, _hex(outer) if outer else None, emph)
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
    _draw_shadow(img, pos, font, size, fill, inner, emph)


# ---------------- 見せ方（style） ----------------
_VF: dict[str, str] = {}   # 太さを変えられるフォントのファイル → 使う太さ（find_font で決める）

# 見せ方ごとの初期値。テンプレートの [thumbnail] に値があれば、そちらを使う（空なら見せ方の初期値）
STYLES = {
    "elegant": {   # 上品: 明朝体の白い文字に柔らかい影。暗いグラデーションの上に置き、細い金の線と小さな見出しを添える
        "label": "上品（明朝体の白い文字に影）", "effect": "shadow", "align": "left",
        "fonts": [("NotoSerifJP-VF.ttf", 0, "Bold"), ("yumindb.ttf", 0, ""), ("HGRME.TTC", 0, ""),
                  ("BIZ-UDMinchoM.ttc", 0, ""), ("msmincho.ttc", 0, "")],
        "text_color": "#FFFFFF", "outline_color": "#000000", "outer_color": "", "accent_color": "#D9B76E",
        "emphasis_color": "#E8C67E", "badge_color": "#B5372B",   # 強調は金、バッジは朱色の落款（はんこ）
        "contrast": 1.05, "saturation": 1.0, "vignette": 0.45, "shade": 215,
    },
    "pop": {       # ポップ: 太い文字に二重のふち（今までの見せ方を整えたもの）
        "label": "ポップ（太い文字に二重のふち）", "effect": "outline", "align": "center",
        "fonts": [("NotoSansJP-VF.ttf", 0, "Black"), ("HGRSGU.TTC", 0, ""), ("YuGothB.ttc", 0, ""),
                  ("meiryob.ttc", 0, ""), ("BIZ-UDGothicB.ttc", 1, "")],
        "text_color": "#FFE14D", "outline_color": "#111111", "outer_color": "#FFFFFF", "accent_color": "auto",
        "emphasis_color": "#FF4436", "badge_color": "#E53935",   # 強調は赤、バッジは赤いギザギザのシール
        "contrast": 1.08, "saturation": 1.1, "vignette": 0.15, "shade": 150,
    },
    "simple": {    # シンプル: 半透明の板の上に白い文字
        "label": "シンプル（半透明の板に白い文字）", "effect": "panel", "align": "left",
        "fonts": [("NotoSansJP-VF.ttf", 0, "Bold"), ("YuGothB.ttc", 0, ""), ("meiryob.ttc", 0, ""),
                  ("BIZ-UDGothicB.ttc", 1, "")],
        "text_color": "#FFFFFF", "outline_color": "#101418", "outer_color": "", "accent_color": "auto",
        "emphasis_color": "#FFD54A", "badge_color": "auto",       # 強調は黄色、バッジは写真の色の角丸の札
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
    for k in ("text_color", "outline_color", "outer_color", "accent_color", "emphasis_color", "badge_color", "align"):
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


SHADE_FADE = 170     # 暗くした所から、元の明るさに戻るまでの長さ（ピクセル）
SHADE_TARGET = 120   # 文字の後ろの明るさ（0〜255）をここまで下げれば、白い文字が読める
SHADE_MIN = 0.25     # 写真がもともと暗くても、見せ方の影の濃さのこの割合は付ける（見た目をそろえる）


SHADE_TONES = {"light": (0.6, 150), "normal": (1.0, SHADE_TARGET), "strong": (1.3, 95)}   # (濃さの上限の倍率, 目標の明るさ)


def _shade_need(img: Image.Image, box, cap: float, tone: str = "normal") -> int:
    """文字の後ろ（box）を、白い文字が読める明るさまで下げるのに要る影の濃さ（0〜255）。
    明るい紙の上なら濃く、紺の箱の上などもともと暗い所ならごく薄く。見せ方の濃さ cap を超えない。
    tone: AIが決めた強さ（light=写真の明るさを活かす / normal / strong=文字をくっきり）"""
    mult, target = SHADE_TONES.get(tone, SHADE_TONES["normal"])
    cap = min(255.0, cap * mult)
    if cap <= 0:
        return 0
    x0, y0, x1, y1 = (int(max(0, min(v, lim))) for v, lim in zip(box, (W, H, W, H)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return int(cap * SHADE_MIN)
    lum = float(np.percentile(np.asarray(img.convert("L").crop((x0, y0, x1, y1))), 70))
    need = 255 * (1 - target / lum) if lum > target else 0.0
    return int(min(cap, max(cap * SHADE_MIN, need)))


def _shade(img: Image.Image, axis: str, lo: float, hi: float, strength: int, edge: str | None = None):
    """文字の後ろを、画面の幅（または高さ）いっぱいの帯で暗くする。帯の外はなめらかに元の明るさに戻す。
    axis="y": 横いっぱいの帯（高さ lo〜hi。画面の下半分など） / axis="x": 縦いっぱいの帯（幅 lo〜hi。画面の右半分など）。
    strength: 文字の後ろの影の濃さ（0〜255。_shade_need）。
    edge（top/bottom/left/right）: 帯を画面の端から始めるとき、その端に近いほど少し濃くする。
    文字のまわりだけを暗くすると、まわりに半端な明るい所が残るので、端から端までまとめて暗くする"""
    if strength <= 0:
        return
    n = H if axis == "y" else W
    d = np.arange(n, dtype=np.float32)
    lo, hi = max(0.0, lo), min(float(n), hi)
    if edge in ("bottom", "right"):     # 端からの距離で考える
        d, lo, hi = n - 1 - d, n - hi, n - lo
    inside = 1.0 if edge is None else 1.1 - 0.2 * np.clip(d / max(hi, 1.0), 0, 1)
    out = np.clip(1 - np.maximum(lo - d, d - hi) / SHADE_FADE, 0, 1)
    out = out * out * (3 - 2 * out)    # なめらかに戻す
    ramp = np.where((d >= lo) & (d <= hi), inside, (0.9 if edge else 1.0) * out) * strength
    g = ramp.clip(0, 255).astype(np.uint8)
    g = Image.fromarray(g[:, None] if axis == "y" else g[None, :])
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


def _label_box(lab, x, y, align):
    """見出しの枠（文字を主役の後ろに回すとき、見出しが隠れないかを調べる）"""
    if not lab:
        return None
    f, s = lab
    w = f.getbbox(s)[2]
    lx = x - w / 2 if align == "center" else (x - w if align == "right" else x)
    return (lx, y, lx + w, y + f.size * 1.2)


def _block(lines, f, size, lab, st, emphasis: str = ""):
    """文字のまとまりの幅と高さ（見出しを含む。lab は _label_fit の結果）"""
    w, h = _text_wh(lines, f, size, emphasis)
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
    """文字の枠（box）が、大事な所（subject。枠か Keep）をどれだけ隠すか（隠す割合 × 重み）。大事な所が無ければ 0"""
    if isinstance(subject, Keep):
        return subject.hide([box])
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


def _cell(box):
    """1280×720 の枠を、地図（160×90）のマス目の範囲にする"""
    x0, y0, x1, y1 = [int(round(v / 8)) for v in box]
    x0, y0 = min(max(0, x0), 159), min(max(0, y0), 89)
    return x0, y0, min(160, max(x1, x0 + 1)), min(90, max(y1, y0 + 1))


def _grow(m: np.ndarray) -> np.ndarray:
    """地図の印を、上下左右に1マス広げる"""
    g = m.copy()
    g[1:] |= m[:-1]
    g[:-1] |= m[1:]
    g[:, 1:] |= m[:, :-1]
    g[:, :-1] |= m[:, 1:]
    return g


def _parts(m: np.ndarray, min_cells: int) -> np.ndarray:
    """つながった印の塊に番号を付けた地図（0 = 何も無い）。min_cells マスより小さな塊（汚れ・ゴミ）は物と見なさない"""
    h, w = m.shape
    seen = np.zeros(m.shape, bool)
    out = np.zeros(m.shape, np.int32)
    n = 0
    for y, x in zip(*np.nonzero(m)):
        if seen[y, x]:
            continue
        seen[y, x] = True
        stack, part = [(y, x)], []
        while stack:
            cy, cx = stack.pop()
            part.append((cy, cx))
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(part) >= min_cells:
            n += 1
            ys, xs = zip(*part)
            out[list(ys), list(xs)] = n
    return out


def _grow_parts(lab: np.ndarray) -> np.ndarray:
    """番号を付けた塊を、まわりの1マスに広げる（文字と物の間に少し隙間を空けるため）"""
    g = lab.copy()
    for dst, src in (((slice(1, None),), (slice(None, -1),)), ((slice(None, -1),), (slice(1, None),)),
                     ((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                     ((slice(None), slice(None, -1)), (slice(None), slice(1, None)))):
        d, s = g[dst], lab[src]
        m = (d == 0) & (s > 0)
        d[m] = s[m]
    return g


MIN_PART = 30      # 物と見なす塊の大きさ（地図のマス。1マス = 8×8 ピクセル）
TOUCH_W = 0.3      # 文字が物に触れる（重なる・近すぎる）ときの、物1つあたりの減点の上限
TOUCH_N = 100      # 1つの物に触れるマスがこれだけあると、減点が上限になる。少しでも触れると大きめに減点する（平方根）


class Keep:
    """文字で隠してはいけない所の地図（160×90）。3つの層で持つ:
    obj  = 物そのもの（主役の枠の中の、輪郭の多い所: ペン・表示・ロゴ。隠してはいけない枠 keeps と、切り抜けた主役の形も）
    base = 主役の枠全体（平らな所: 箱の面・はかりの台なども含む）
    part = 物ごとに番号を付けた地図（物のまわり1マスも含む。小さな汚れは除く）。文字が少しでも触れたら、触れた物ごとに減点する
           （ペンの端に見出しがかかる、などを避ける。ペンとはかりの両方に触れるより、片方だけの方がよい）
    枠が大きくても、細いペンなどの物を隠すことを重く見る（枠の大部分が平らな所でも、物の上には文字を置かない）"""

    def __init__(self, obj, base, part=None):
        self.obj = np.asarray(obj, np.float32)
        self.base = np.asarray(base, np.float32)
        self.part = np.asarray(part, np.int32) if part is not None else np.zeros(self.base.shape, np.int32)
        self.t_obj, self.t_base = float(self.obj.sum()), float(self.base.sum())

    def __bool__(self) -> bool:
        return self.t_base > 1e-6 or self.t_obj > 1e-6

    @classmethod
    def build(cls, img: Image.Image, subject, keeps=(), cut=None) -> "Keep":
        obj = np.zeros((90, 160), np.float32)
        base = np.zeros((90, 160), np.float32)
        if subject:
            det = _detail(img)
            x0, y0, x1, y1 = _cell(subject)
            base[y0:y1, x0:x1] = 1.0
            obj[y0:y1, x0:x1] = _grow(det[y0:y1, x0:x1] > 0.3)   # 1マス広げて、物の輪郭の内側（平らな面）も物に含める
        part = _parts(obj > 0.5, MIN_PART)
        n = int(part.max())
        for b in keeps or ():   # 隠してはいけない枠は、1つずつ別の物
            if b:
                x0, y0, x1, y1 = _cell(b)
                obj[y0:y1, x0:x1] = 1.0
                base[y0:y1, x0:x1] = 1.0
                n += 1
                part[y0:y1, x0:x1] = n
        if cut is not None:     # 切り抜けた主役の形（ほかの物に入っていない所）も、1つの物
            c = Image.fromarray(np.clip(np.asarray(cut) * 255, 0, 255).astype(np.uint8)).resize((160, 90), Image.BILINEAR)
            c = np.asarray(c, np.float32) / 255
            obj, base = np.maximum(obj, c), np.maximum(base, c)
            part[(c > 0.5) & (part == 0)] = n + 1
        return cls(obj, base, _grow_parts(part))

    def hide(self, boxes) -> float:
        """文字（字ごとの枠のリスト）が、大事な所をどれだけ隠すか（0〜1.5）。
        物を隠す割合を重く（3/4）、枠全体を隠す割合を軽く（1/4）見る。物が見つからなければ、枠を隠す割合だけ"""
        if not self:
            return 0.0
        cover = _cover(self.base.shape, boxes)
        fb = float(self.base[cover].sum()) / self.t_base if self.t_base > 1e-6 else 0.0
        if self.t_obj <= 1e-6:
            return 1.5 * fb
        return 1.5 * (0.75 * float(self.obj[cover].sum()) / self.t_obj + 0.25 * fb)

    def touch(self, boxes) -> float:
        """文字（字ごとの枠のリスト）が物に触れる度合いの減点。触れた物ごとに 0〜TOUCH_W を足す。置き場所を選ぶときだけ使う。
        隠す割合（hide）は物全体に対する割合なので、ペンの端に少しかかるくらいでは小さい。それも避けるための減点"""
        labs = self.part[_cover(self.part.shape, boxes)]
        labs = labs[labs > 0]
        if not labs.size:
            return 0.0
        counts = np.bincount(labs)
        counts = counts[counts > 0]
        return TOUCH_W * float(np.sqrt(np.minimum(1.0, counts / TOUCH_N)).sum())

    def most(self, boxes) -> float:
        """文字（字ごとの枠のリスト）が、1つの物をいちばん多く隠す割合（0〜1。物ごとに、隠すマス ÷ 物のマス）"""
        if not self.part.any():
            return 0.0
        n = int(self.part.max()) + 1
        whole = np.bincount(self.part.ravel(), minlength=n)
        hid = np.bincount(self.part[_cover(self.part.shape, boxes)], minlength=n)
        return float((hid[1:] / np.maximum(whole[1:], 1)).max())

    def lost(self, x0: float, y0: float, cw: float, ch: float) -> float:
        """写真を (x0, y0, cw, ch) で切り出すと、物（無ければ主役の枠）のどれだけが切れて見えなくなるか（0〜1）"""
        m, t = (self.obj, self.t_obj) if self.t_obj > 1e-6 else (self.base, self.t_base)
        if t <= 1e-6:
            return 0.0
        a, b, c, d = int(x0 / 8), int(y0 / 8), int((x0 + cw) / 8), int((y0 + ch) / 8)
        return max(0.0, 1.0 - float(m[b:d, a:c].sum()) / t)

    def moved(self, x0: float, y0: float, cw: float, ch: float) -> "Keep":
        """写真を (x0, y0, cw, ch) で切り出して広げたときの地図"""
        box = (int(x0 / 8), int(y0 / 8), int((x0 + cw) / 8), int((y0 + ch) / 8))
        obj, base = (np.asarray(Image.fromarray(m).crop(box).resize((160, 90), Image.BILINEAR)) for m in (self.obj, self.base))
        part = np.asarray(Image.fromarray(self.part).crop(box).resize((160, 90), Image.NEAREST))
        return Keep(obj, base, part)


def _cover(shape, boxes) -> np.ndarray:
    """字ごとの枠のリストが覆う、地図のマス"""
    cover = np.zeros(shape, bool)
    for b in boxes:
        if b:
            x0, y0, x1, y1 = _cell(b)
            cover[y0:y1, x0:x1] = True
    return cover


def _busy_boxes(det: np.ndarray, boxes) -> float:
    """字ごとの枠の下の、写真の細かさ（面積で平均）"""
    cover = _cover(det.shape, boxes)
    return float(det[cover].mean()) if cover.any() else 0.0


def _ink_boxes(pos, font, st: dict, extra=()) -> list:
    """写真を隠す枠: 字ごとの枠（ふち・影の幅を足す）と、見出しなどの枠。
    シンプル（文字の後ろに半透明の板を敷く）は、字ごとではなく板全体（_draw_styled と同じ大きさ）"""
    size = font.size
    gl = _glyphs(pos, font)
    if st.get("effect") == "panel" and gl:
        px, py = size * 0.35, size * 0.22
        boxes = [(min(b[0] for b in gl) - px, min(b[1] for b in gl) - py, max(b[2] for b in gl) + px,
                  max(b[3] for b in gl) + py)]
    else:
        g = size * _ink(st)
        boxes = [(b[0] - g, b[1] - g, b[2] + g, b[3] + g) for b in gl]
    return boxes + [e for e in extra if e]


SCALES = (1.0, 0.86, 0.74)   # 大事な所を隠さないために、文字を小さくしてみる倍率
SHRINK_COST = 1.0            # 小さくした分の減点（1 - 倍率 に掛ける）。隠す度合いがこれより大きく減るときだけ小さくする
POOR_HIDE = 0.4              # これより大事な所を隠すデザインは、ほかのデザインに替える（AIにデザインを任せるとき）
POOR_MOST = 0.3              # 1つの物（ペン・表示など）をこれより多く隠すデザインも、ほかのデザインに替える


def _side_place(img, keep, blocks, position, margin, hide_w=1.0):
    """横に文字・縦書き: 文字を置く側（左右）・上下の位置・大きさと、写真の切り出し (x0, y0, 幅, 高さ)。
    blocks: 大きさごとの (幅, 高さ, 字の枠を返す関数 f(側, tx, ty), 小さくした分の減点)。
    (側, y, 切り出し, 何番目の大きさか, 隠す度合い, 1つの物を隠す割合) を返す。
    写真は少し大きくして切り出す（左右の端に黒い帯を作らない）。切り出す所は、左右・上下の寄せ方を何通りか試す。
    基本は写真の中身を文字の反対側へ寄せる（右に文字なら、写真の右寄りを切り出すと中身が左へ動く）。
    大事な所を隠さない・物に触れない・物が切れない置き方を優先し、次に写真の細かさの少ない所。
    右に置くときは、右下の再生時間の表示にかからない高さにする"""
    cw, ch = int(W * 0.86), int(H * 0.86)
    views = []
    for x0 in (0, W - cw):
        for y0 in (0, (H - ch) // 2, H - ch):
            det = _detail(img.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H)))
            kp = keep.moved(x0, y0, cw, ch) if keep else None
            views.append(((x0, y0, cw, ch), det, kp, 1.5 * keep.lost(x0, y0, cw, ch) if keep else 0.0))
    cands = []
    for side in ("left", "right"):
        for crop, det, kp, lost in views:
            usual = crop[0] == (W - cw if side == "right" else 0) and crop[1] == 0
            extra = lost + (0.0 if usual else 0.03)
            for k, (bw, bh, boxes_at, cost) in enumerate(blocks):
                tx = margin if side == "left" else W - margin - bw
                top, bottom = 48.0, H - bh - 48.0
                if side == "right":
                    bottom = min(bottom, TIME_BOX[1] - TIME_GAP - bh)
                bottom = max(bottom, top)
                for j in range(7):   # 上から下まで7か所。真ん中を少しひいきする
                    ty = top + (bottom - top) * j / 6
                    boxes = boxes_at(side, tx, ty)
                    hide = hide_w * kp.hide(boxes) if kp else 0.0
                    near = hide_w * kp.touch(boxes) if kp else 0.0
                    sc = _busy_boxes(det, boxes) + hide + near + cost + extra + 0.03 * abs(j - 3) / 3 \
                        + 5.0 * any(time_hit(b) for b in boxes)
                    if position == side:
                        sc *= 0.7
                    cands.append((sc, side, k, ty, hide, crop, kp, boxes))
    _, side, k, y, hide, crop, kp, boxes = min(cands, key=lambda c: c[0])
    return side, y, crop, k, hide, (hide_w * kp.most(boxes) if kp else 0.0)


def compose(frame: Image.Image, text: str, layout: str, tpl: Template, font: tuple[str, int],
            label: str = "", position: str = "auto", subject=None, design: dict | None = None,
            keeps=None) -> Image.Image:
    """1枚のサムネイル（1280×720）。

    layout: bottom=大きな文字（上か下）/ band=帯（上か下）/ side=横に文字（左か右・上中下）/ vertical=縦書き（左か右）
    position: 文字を置く側の希望（AIが選んだ top/bottom/left/right。auto なら写真の空いている側）
    label: 大きな文字に添える小さな見出し（商品名など。空なら付けない）
    subject: 主役・大事な所をまとめて囲む枠（1280×720 の座標。subject_px で作る）。枠の中の細かい所ほど隠さない
    design: AIが決めたデザイン {emphasis: 強調する言葉, badge: バッジの言葉, effect: none/focus/sticker/depth,
            shade: 文字の後ろを暗くする強さ light/normal/strong}（rc_thumbart）
    keeps: ほかに隠してはいけない小さな所の枠のリスト（はかりの表示・ロゴ・顔など）
    返す画像の info["hide"] に、文字（帯）が大事な所を隠す度合い（0〜1.5）、
    info["most"] に、1つの物（ペン・表示など）をいちばん多く隠す割合（0〜1）を入れる
    """
    import rc_thumbart as A
    st = text_style(tpl)
    fp, idx = font
    img = frame.convert("RGB").resize((W, H), Image.LANCZOS)
    margin = 64
    pad = _pad(st)
    dz = A.clean_design(design, text, label)
    emph, effect = dz["emphasis"], dz["effect"]
    cut = A.cutout(img, subject) if effect != "none" else None   # 主役の切り抜き（切れなければ None）
    hide_w = 0.5 if effect == "depth" and cut is not None else 1.0   # 文字を主役の後ろに回すなら、少し重なってよい
    keep = Keep.build(img, subject, keeps, cut)
    if effect != "none":   # 写真の効果でまわりを暗くするときは、文字の下のグラデーションと四隅の暗さを控えめに（暗くなりすぎない）
        st = {**st, "vignette": float(st.get("vignette", 0)) * 0.5, "shade": int(st.get("shade", 150) * 0.7)}
    if dz["shade"] == "light":   # 明るく: 四隅の暗さも弱める
        st = {**st, "vignette": float(st.get("vignette", 0)) * 0.5}

    def prepare(im, crop=None):
        """写真の補正と効果。crop = 切り出して広げるとき。(写真, 主役の切り抜き, 大事な所の枠) を返す"""
        c, s = cut, subject
        if crop:
            im = im.crop((crop[0], crop[1], crop[0] + crop[2], crop[1] + crop[3])).resize((W, H), Image.LANCZOS)
            c, s = A.crop_alpha(cut, crop), _moved(subject, *crop)
        return A.apply_effect(_grade(im, st), effect, c, s, st), c, s

    def sized(f0, size0, s):
        return f0 if s == 1 else _font(fp, idx, max(36, int(size0 * s)))

    if layout in ("side", "vertical"):
        blocks = []
        if layout == "side":
            lines = _lines(text, one_line_max=4)
            room = H - 160 - (int(LABEL_MIN * 1.75) if label and st.get("show_label", True) else 0)
            f0, size0 = _fit(fp, idx, lines, int(W * 0.5), room, start=200, pad=pad, emphasis=emph)
            for s in SCALES:
                f = sized(f0, size0, s)
                lab = _label_fit(label, fp, idx, f.size, int(W * 0.6), st)
                bw, bh = _block(lines, f, f.size, lab, st, emph)

                def boxes_at(side, tx, ty, f=f, lab=lab, bw=bw):
                    x, a = (tx, "left") if side == "left" else (tx + bw, "right")
                    pos = _text_pos(lines, f, f.size, x, ty + _label_h(lab, st), a, emph)
                    return _ink_boxes(pos, f, st, [_label_box(lab, x, ty, a)])
                blocks.append((bw, bh, boxes_at, SHRINK_COST * (1 - s), (f, lab, None, 0)))
        else:   # 縦書き: 右の列から。見出しは大きな文字の右に、縦に小さく。右に置いても再生時間の表示にかからない長さ
            lines = _lines(text, one_line_max=6)
            vmax = TIME_BOX[1] - TIME_GAP - 44
            vl = A.vlabel_fit(label, fp, idx, st, vmax)
            lw = vl[0].size * 1.6 if vl else 0
            f0, size0 = A.vfit(fp, idx, lines, int(W * 0.44) - lw, vmax, emph, pad=pad)
            for s in SCALES:
                f = sized(f0, size0, s)
                tw, th = A.vpos(lines, f, f.size, 0, 0, emph)[1]
                bw, bh = tw + lw, max(th, A.vlabel_h(vl))

                def boxes_at(side, tx, ty, f=f, bw=bw):
                    pos = A.vpos(lines, f, f.size, tx + bw - lw, ty, emph)[0]
                    lb = (tx + bw - lw, ty, tx + bw, ty + A.vlabel_h(vl)) if vl else None
                    return _ink_boxes(pos, f, st, [lb])
                blocks.append((bw, bh, boxes_at, SHRINK_COST * (1 - s), (f, None, vl, lw)))
        side, y, crop, k, hide, most = _side_place(img, keep, [b[:4] for b in blocks], position, margin, hide_w)
        bw, bh, _, _, (f, lab, vl, lw) = blocks[k]
        size = f.size
        img, cut, subj = prepare(img, crop)
        base = img.copy()
        accent = _accent(img, st)
        st = {**st, "_accent": accent}
        tx = margin if side == "left" else W - margin - bw
        tbox = (tx, y, tx + bw, y + bh)
        # 文字を置いた側の端から、文字の向こう側までを縦いっぱいに暗くする（画面の右半分など）
        strength = _shade_need(img, tbox, max(st.get("shade", 150), 120 if effect == "none" else 90), dz["shade"])
        if side == "left":
            _shade(img, "x", 0, tx + bw + 30, strength, "left")
        else:
            _shade(img, "x", tx - 30, W, strength, "right")
        if layout == "side":
            x, align = (margin, "left") if side == "left" else (W - margin, "right")
            lbox = _label_box(lab, x, y, align)
            y += _draw_label(img, lab, x, y, align, st, accent, subj)
            tpos = _text_pos(lines, f, size, x, y, align, emph)
        else:
            A.draw_vlabel(img, vl, tx + bw, y, st, accent, subj)
            lbox = (tx + bw - lw, y, tx + bw, y + A.vlabel_h(vl)) if vl else None
            tpos = A.vpos(lines, f, size, tx + bw - lw, y, emph)[0]
        _draw_styled(img, lines, f, size, 0, 0, "left", st, emph, pos=tpos)

    elif layout == "band":
        # 帯を、大事な所・写真の細かい所を隠しにくい側（上か下）に置く。帯の色は写真から取る（上品・シンプルは深い色）
        img, cut, subj = prepare(img)
        base = img.copy()
        det = _detail(img)
        # 見出しは帯の中で読める大きさ（LABEL_MIN）にし、その分だけ帯を少し太くする
        lab = _label_fit(label, fp, idx, int(LABEL_MIN / 0.42) + 1, W - 2 * margin, st)
        band_h = int(H * (0.38 if lab else 0.34))
        hides = {"top": hide_w * keep.hide([(0, 0, W, band_h)]), "bottom": hide_w * keep.hide([(0, H - band_h, W, H)])}
        score = {"top": _busy(det, (0, 0, W, band_h)) + hides["top"],
                 "bottom": _busy(det, (0, H - band_h, W, H)) + hides["bottom"]}
        if position in score:
            score[position] *= 0.7
        side = min(score, key=score.get)
        hide = hides[side]
        y0 = 0 if side == "top" else H - band_h
        most = hide_w * keep.most([(0, y0, W, y0 + band_h)])
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
        # 1行に収まるなら1行で大きく。収まらなければ2行。
        # 帯が下なら、右下の再生時間の表示にかからないようにする（少し左に寄せる。寄せきれなければ、左の空きに収まる大きさ）
        two = _lines(text)
        one = ["".join(two)]
        free = TIME_BOX[0] - TIME_GAP - margin
        tries = [(one, W - 2 * margin, W / 2), (one, free, margin + free / 2), (two, W - 2 * margin, W / 2),
                 (two, free, margin + free / 2)]
        if side == "top":
            tries = [tries[0], tries[2]]
        for k, (lines, max_w, cx) in enumerate(tries):
            f, size = _fit(fp, idx, lines, max_w, room, start=150, pad=pad, emphasis=emph)
            last = k == len(tries) - 1
            if lines is one and size < 84 and len(two) > 1 and not last:
                continue
            bw, bh = _block(lines, f, size, lab, st, emph)
            # 見た目の字（見出しの字の上から、大きな文字の下まで）が帯の真ん中に来るようにする
            # （明朝体などは字の上の余白が大きく、計算上の高さで真ん中にすると下に寄って見える）
            y = y0 + (band_h - bh) / 2
            gl = _glyphs(_text_pos(lines, f, size, cx, y + _label_h(lab, st), "center", emph), f)
            ink_top = y + lab[0].getbbox(lab[1])[1] if lab else min(b[1] for b in gl)
            ink_bottom = max(b[3] for b in gl)
            mid = y0 + band_h / 2 + (-6 if (pop and side == "bottom") else 6 if pop else 0)   # ポップは斜めの端の分
            y += mid - (ink_top + ink_bottom) / 2
            if side == "bottom":
                cx -= _time_shift(_text_pos(lines, f, size, cx, y + _label_h(lab, st), "center", emph), f,
                                  size * _ink(st))
                if cx - bw / 2 < margin * 0.5 and not last:
                    continue
            break
        if pop:   # ポップ: 白い文字に濃いふち1本（帯の上では外側の白いふちは付けない）。見出しは濃い色の札。強調は黄色
            bst = {**st, "text_color": "#FFFFFF", "outer_color": "", "_accent": None, "emphasis_color": st["text_color"]}
            tag = (17, 17, 17)
        else:     # 上品・シンプル: 帯の上では板は使わず、影で読ませる。見出しは金（上品）か白
            bst = {**st, "effect": "shadow", "_accent": None}
            tag = gold or _hex(st["text_color"])
        accent = gold or color
        tbox = (cx - bw / 2, y, cx + bw / 2, y + bh)
        lbox = _label_box(lab, cx, y, "center")
        y += _draw_label(img, lab, cx, y, "center", bst, tag)
        tpos = _text_pos(lines, f, size, cx, y, "center", emph)
        _draw_styled(img, lines, f, size, cx, y, "center", bst, emph, pos=tpos)

    else:  # bottom: 大きな文字を、写真の空いている側（上か下）に。大事な所を隠すなら、少し小さくしてみる
        img, cut, subj = prepare(img)
        base = img.copy()
        accent = _accent(img, st)
        det = _detail(img)
        lines = _lines(text)
        f0, size0 = _fit(fp, idx, lines, W - 2 * margin, int(H * 0.5), pad=pad, emphasis=emph)
        align = st.get("align", "center")
        cands = []
        for s in SCALES:
            f = sized(f0, size0, s)
            size = f.size
            lab = _label_fit(label, fp, idx, size, W - 2 * margin, st)
            bw, bh = _block(lines, f, size, lab, st, emph)
            grow = size * _ink(st)
            # 上か下に置くのが基本。上も下も大事な所にかかるときのために、間の高さ（middle）も少し不利にして比べる
            heights = [("top", 44.0)] + [("middle", 44 + (H - bh - 92) * j / 8) for j in range(1, 8)] \
                + [("bottom", H - bh - 48.0)]
            for v, y_base in heights:
                for a in (("center", "left", "right") if align == "center" else ("left", "right")):
                    x0 = (W - bw) / 2 if a == "center" else (margin if a == "left" else W - margin - bw)
                    x = W / 2 if a == "center" else (margin if a == "left" else W - margin)
                    y = y_base
                    if v != "top":   # 右下の再生時間の表示にかかるなら、その分だけ上げる
                        y -= _time_lift(_text_pos(lines, f, size, x, y + _label_h(lab, st), a, emph), f, grow)
                    boxes = _ink_boxes(_text_pos(lines, f, size, x, y + _label_h(lab, st), a, emph), f, st,
                                       [_label_box(lab, x, y, a)])
                    # 中央寄せの見せ方は、中央を少しひいきする（左右に寄せるのは、中央が写真の大事な所と重なるとき）
                    bias = 0.04 * (v == "top") + 0.08 * (v == "middle") + 0.02 * (a == "right") \
                        + 0.02 * (align == "center" and a != "center")
                    hide = hide_w * keep.hide(boxes)
                    sc = _busy_boxes(det, boxes) + hide + hide_w * keep.touch(boxes) + bias + SHRINK_COST * (1 - s)
                    if position == v:
                        sc *= 0.7
                    cands.append((sc, v, a, y, x0, f, lab, bw, bh, hide, boxes))
        _, v, a, y, x0, f, lab, bw, bh, hide, boxes = min(cands, key=lambda c: c[0])
        most = hide_w * keep.most(boxes)
        size = f.size
        # 文字の後ろを、画面の幅（高さ）いっぱいに暗くする。上か下なら、その端から文字の向こう側まで（画面の下半分など）。
        # 間の高さなら、左右に寄せた文字はその側の端から（画面の右半分など）、真ん中の文字は横いっぱいの帯で
        strength = _shade_need(img, (x0, y, x0 + bw, y + bh), st.get("shade", 150), dz["shade"])
        if v == "top":
            _shade(img, "y", 0, y + bh + 24, strength, "top")
        elif v == "bottom":
            _shade(img, "y", y - 24, H, strength, "bottom")
        elif a == "left":
            _shade(img, "x", 0, x0 + bw + 30, strength, "left")
        elif a == "right":
            _shade(img, "x", x0 - 30, W, strength, "right")
        else:
            _shade(img, "y", y - 24, y + bh + 24, strength)
        x = W / 2 if a == "center" else (margin if a == "left" else W - margin)
        st = {**st, "_accent": accent}
        tbox = (x0, y, x0 + bw, y + bh)
        lbox = _label_box(lab, x, y, a)
        y += _draw_label(img, lab, x, y, a, st, accent, subj)
        tpos = _text_pos(lines, f, size, x, y, a, emph)
        _draw_styled(img, lines, f, size, x, y, a, st, emph, pos=tpos)
    # 文字を主役の後ろに回す（大きな文字が1字でも隠れすぎる・小さな見出しが少しでも隠れるなら回さない）・バッジ
    glyphs = _glyphs(tpos, f) if effect == "depth" else None
    img = A.finish(img, base, cut, subj, dz, st, accent, tbox, fp, idx, glyphs, lbox)
    out = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=2))
    out.info["hide"] = round(float(hide), 3)
    out.info["most"] = round(float(most), 3)
    return out


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

def _compose_set(frame, p: dict, lays: list[str], tpl, font, design: dict, keeps, swap: bool):
    """1枚の写真から、デザイン（lays）ごとのサムネイル [(デザイン, 画像)]。
    swap=True（AIにデザインを任せるとき）なら、文字や帯が大事な所を隠しすぎるデザインは、まだ使っていないデザインに替える"""
    import rc_thumbart as A

    def make(lay):
        return compose(frame, p["text"], lay, tpl, font, p.get("label", ""), p.get("text_position", "auto"),
                       subject_px(p.get("subject_box")), design, keeps)
    if not swap:
        return [(lay, make(lay)) for lay in lays]
    good, poor = [], []
    for lay in lays + [x for x in A.LAYOUTS if x not in lays]:
        if len(good) >= len(lays):
            break
        img = make(lay)
        ok = img.info.get("hide", 0) <= POOR_HIDE and img.info.get("most", 0) <= POOR_MOST
        (good if ok else poor).append((lay, img))
    good += sorted(poor, key=lambda m: m[1].info.get("hide", 0) + m[1].info.get("most", 0))[:len(lays) - len(good)]
    order = {}   # AIが選んだ順、替えたものはその後ろ（デザインの決まった順）
    for k, lay in enumerate(lays + list(A.LAYOUTS)):
        order.setdefault(lay, k)
    return sorted(good, key=lambda m: order[m[0]])


def generate(out_dir: Path, tpl: Template, llm: str = "api", effort: str | None = None,
             custom_text: str | None = None, request: str = "") -> list[Path]:
    """サムネイル案を作る。effort: AIの考える深さ（None ならテンプレートの [thumbnail] の effort。初期値 high）。
    request: 今のサムネイル案を見て、AIに指示して作り直す（「案2は文字を左に」「もっと明るく」など。llm="api" のとき）"""
    import rc_thumbart as A
    request = " ".join((request or "").split())
    if request and llm != "api":
        print("  [注意] AIへの指示は、AIを「Claude APIで自動」にしたときだけ使えます（指示なしで作ります）")
        request = ""
    effort = effort if effort in EFFORTS else thumb_effort(tpl)
    art = art_director(tpl)
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
    prev = _previous(tdir) if request else None   # 前回の案（画像は消す前に読み込む）
    print("■ サムネイル案")
    print("  候補の場面を取り出しています…", flush=True)
    pro = _pro(tpl)   # 顔で選ぶ・人物の切り抜き（rc_thumbspro。使えなければ None）
    cands = extract_candidates(src, candidate_times(sess, tl, chunks, decisions, plan), work,
                               pro.frame_key if pro else None)
    if not cands:
        raise SystemExit("[エラー] 候補の静止画を取り出せませんでした。")
    if pro:
        cands = pro.annotate(cands)

    usage, asked = None, False
    if llm == "api" and (request or not custom_text):
        if request:
            print(f"  Claudeが指示に合わせて作り直しています（指示: {request}）…", flush=True)
        print(f"  Claudeが候補{len(cands)}枚から選んでいます（考える深さ: {effort}"
              f"{'・デザインもおまかせ' if art else ''}）…", flush=True)
        try:
            picks, usage = pick_with_ai(cands, title, ideas, sess.get("mode", "talk"), effort,
                                        text_style(tpl)["label"], art, request, prev)
        except Exception as e:  # noqa: BLE001
            print(f"  [注意] AIでの選択に失敗したので、自動で選びます: {e}")
            picks = []
        asked = bool(picks)
        if not picks:
            picks = pick_simple(cands, ideas, title)
        if custom_text:
            for p in picks:
                p["text"] = custom_text
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
        # デザイン: AIが決めたもの（デザインもおまかせのとき）。AIを使わないときは、数字（30g など）だけ強調する
        design, lays = {}, [x for x in layouts if x != "person"]
        if art:
            design = A.clean_design(p if "emphasis" in p else {"emphasis": A.auto_emphasis(p["text"])},
                                    p["text"], p.get("label", ""))
            if p.get("layouts"):
                lays = A.pick_layouts(p["layouts"], lays)
        keeps = [b for b in (subject_px(x) for x in (p.get("keep_boxes") or [])[:3]) if b]
        made = _compose_set(frame, p, lays, tpl, font, design, keeps, swap=art)
        lays = [lay for lay, _img in made]
        for lay, img in made:
            path = tdir / f"thumb_{i}_{lay}.jpg"
            save_jpeg(img, path)
            outs.append(path)
        outs += pouts
        info.append({"image": i, "text": p["text"], "label": p.get("label", ""),
                     "text_position": p.get("text_position", "any"), "subject_box": p.get("subject_box"),
                     **({"keep_boxes": p["keep_boxes"]} if p.get("keep_boxes") else {}),
                     **({k: design.get(k, "") for k in ("emphasis", "badge", "effect", "shade")} if design else {}),
                     "layouts": lays, "reason": p.get("reason", ""),
                     "source_time": round(by_no[p["frame"]]["t"], 2)})
    (tdir / "thumbnails.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    # 反映した指示を残す（次に指示するとき、AIが前の指示も守れるように）。指示なしで作り直したら、まっさらに戻す
    req_file = tdir / "requests.json"
    if request and asked:
        hist = (prev or {}).get("history", []) + [request]
        req_file.write_text(json.dumps(hist[-20:], ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        if request:
            print("  [注意] AIが使えなかったので、指示は反映していません")
        req_file.unlink(missing_ok=True)
    print(f"  {len(outs)}枚のサムネイル案を作りました: {tdir}")
    if usage and usage["in"]:
        import rc_llm
        print(f"  Claude API: {rc_llm.cost_text(usage)}")
    return outs
