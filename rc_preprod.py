"""企画・台本の支援と、撮り漏れチェック。

- 企画: テーマとジャンルから、構成案・台本（話す要点）・撮影リストを作り、projects/<名前>/ に保存する
    plan.json … 中身（GUI や撮り漏れチェックが読む）
    plan.md   … 印刷・メモ用（Markdown）
    plan.html … 印刷用のページ（ブラウザで開いて印刷。チェック欄つき）
- 撮り漏れチェック: 荒編集（○○_roughcut）の文字起こしと台本を照らし合わせ、
  話し忘れた要点・撮っていないかもしれないカットを一覧にする
    <荒編集フォルダ>/preprod_check.json / preprod_check.md
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

GENRES = {
    "talk": ("レビュー・解説", "商品やテーマを紹介・解説する動画。結論を早めに出し、根拠・実演・まとめの順で分かりやすく。"),
    "craft": ("工作", "ものづくりの動画。完成品のチラ見せ → 材料・道具 → 作業の手順 → 完成・使ってみる、の流れ。"
                     "手元のアップ（2台目カメラ・スマホ）を多めに。"),
    "outdoor": ("屋外企画", "外で撮る企画動画（30〜180分の長尺素材）。企画の説明・ルール → 移動や挑戦 → 山場 → 結果・感想。"
                         "その場の雰囲気やリアクション、景色のBロールが大事。"),
    "mv": ("MV（音楽）", "音楽サークルのMV。曲の構成（イントロ・Aメロ・サビなど）ごとに映像のイメージを決める。"
                      "「話す要点」は歌詞やメッセージ、演奏の見せ場として扱う。"),
    "other": ("そのほか", "自由な形式の動画。"),
}
SHOT_KINDS = {"front": "正面", "hands": "手元", "broll": "Bロール", "insert": "挿入カット"}
SHOT_HINT = {"front": "顔・上半身。話す場面", "hands": "手元のアップ（2台目カメラ・スマホ）",
             "broll": "雰囲気の映像（景色・道具・移動など）", "insert": "説明に差し込む短いカット（商品のアップ・画面など）"}

POINT_STATUS = {"said": "話した", "partial": "一部だけ", "missing": "話していない", "cut": "話したがカットした",
                "unknown": "判定できない"}
SHOT_STATUS = {"done": "撮った（チェック済み）", "likely": "たぶん撮れている", "missing": "撮った記録なし"}

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "concept", "audience", "minutes", "sections", "shots", "props", "title_ideas"],
    "properties": {
        "title": {"type": "string", "description": "仮のタイトル"},
        "concept": {"type": "string", "description": "この動画で伝えたいこと（1〜2文）"},
        "audience": {"type": "string", "description": "どんな人に見てほしいか"},
        "minutes": {"type": "number", "description": "完成動画の目安の長さ（分）"},
        "sections": {
            "type": "array",
            "description": "構成（話の流れ）。上から順に撮る・話す",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "purpose", "minutes", "points"],
                "properties": {
                    "name": {"type": "string", "description": "見出し（例: オープニング）"},
                    "purpose": {"type": "string", "description": "この部分の役割"},
                    "minutes": {"type": "number", "description": "目安の長さ（分）"},
                    "points": {
                        "type": "array",
                        "description": "話す要点（台本）。読み上げる文ではなく、話すことのメモ",
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["text", "keywords", "must"],
                            "properties": {
                                "text": {"type": "string"},
                                "keywords": {"type": "array", "items": {"type": "string"},
                                             "description": "話したかを文字起こしで確かめるための短い言葉2〜4個。"
                                                            "表記ゆれは「|」で区切る（例: 重さ|おもさ）"},
                                "must": {"type": "boolean", "description": "必ず話すこと"},
                            },
                        },
                    },
                },
            },
        },
        "shots": {
            "type": "array",
            "description": "撮影リスト",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["section", "kind", "what", "tip", "must"],
                "properties": {
                    "section": {"type": "integer", "description": "どの構成で使うか（sections の何番目か。1から）"},
                    "kind": {"type": "string", "enum": list(SHOT_KINDS)},
                    "what": {"type": "string", "description": "撮るもの"},
                    "tip": {"type": "string", "description": "撮り方のコツ（角度・長さなど）。なければ空"},
                    "must": {"type": "boolean"},
                },
            },
        },
        "props": {"type": "array", "items": {"type": "string"}, "description": "準備するもの・持ち物"},
        "title_ideas": {"type": "array", "items": {"type": "string"}, "description": "タイトル案（3〜5個）"},
    },
}

CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["points", "shots"],
    "properties": {
        "points": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["id", "status", "chunk_ids", "note"],
                      "properties": {"id": {"type": "string"},
                                     "status": {"type": "string", "enum": ["said", "partial", "missing"]},
                                     "chunk_ids": {"type": "array", "items": {"type": "integer"}},
                                     "note": {"type": "string"}}},
        },
        "shots": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["id", "status", "chunk_ids", "note"],
                      "properties": {"id": {"type": "string"},
                                     "status": {"type": "string", "enum": ["likely", "missing"]},
                                     "chunk_ids": {"type": "array", "items": {"type": "integer"}},
                                     "note": {"type": "string"}}},
        },
    },
}


# ============================================================
#  文字の正規化（全角・半角、カタカナ・ひらがなの違いを無視して比べる）
# ============================================================
def fold_char(ch: str) -> str:
    s = unicodedata.normalize("NFKC", ch).lower()
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def fold(text: str) -> str:
    """1文字ずつ正規化する（検索で、元の文の何文字目かを対応させやすくするため）"""
    return "".join(fold_char(c) for c in text)


_KW_RE = re.compile(r"[一-龯々〆ヵヶ]{2,}|[一-龯々][ぁ-ん]{1,2}|[ァ-ヴー]{2,}|[A-Za-z0-9][A-Za-z0-9.\-]+")


def auto_keywords(text: str, limit: int = 4) -> list[str]:
    """キーワードが無い要点から、確かめるための言葉（漢字・カタカナ・英数字のまとまり）を取り出す"""
    t = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for m in _KW_RE.findall(t):
        if m not in out:
            out.append(m)
    return out[:limit]


def _alts(kw: str) -> list[str]:
    return [fold(a.strip()) for a in re.split(r"[|｜/／]", kw) if a.strip()]


# ============================================================
#  プロジェクトの保存場所
# ============================================================
def projects_dir(base: Path | str | None = None) -> Path:
    if base:
        return Path(base)
    env = os.environ.get("AVE_PROJECTS_DIR")
    return Path(env) if env else APP_DIR / "projects"


def safe_name(name: str) -> str:
    """Windows のフォルダ名に使えない文字を置き換える"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name.strip())
    s = s.strip(" .")[:60]
    if s.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        s = "_" + s
    return s or "企画"


def project_path(name_or_path: str | Path, base: Path | str | None = None) -> Path:
    p = Path(name_or_path)
    if p.is_dir() and (p / "plan.json").exists():
        return p
    if p.name == "plan.json" and p.exists():
        return p.parent
    return projects_dir(base) / safe_name(str(name_or_path))


def list_projects(base: Path | str | None = None) -> list[tuple[Path, dict]]:
    root = projects_dir(base)
    out = []
    if root.is_dir():
        for d in root.iterdir():
            data = _read_json(d / "plan.json")
            if isinstance(data, dict):
                out.append((d, data))
    out.sort(key=lambda x: x[1].get("updated", ""), reverse=True)
    return out


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return default


def _write(p: Path, text: str):
    """途中で止まっても壊れたファイルが残らないように、一時ファイルに書いてから置き換える"""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def load_project(name_or_path: str | Path, base: Path | str | None = None) -> tuple[Path, dict]:
    d = project_path(name_or_path, base)
    data = _read_json(d / "plan.json")
    if not isinstance(data, dict):
        sys.exit(f"[エラー] 企画が見つかりません: {name_or_path}（{d / 'plan.json'}）")
    return d, normalize(data)


def save_project(d: Path, data: dict) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    data = normalize(data)
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(d / "plan.json", json.dumps(data, ensure_ascii=False, indent=1))
    _write(d / "plan.md", to_markdown(data))
    _write(d / "plan.html", to_html(data))
    return d


# ============================================================
#  企画の中身
# ============================================================
def normalize(data: dict) -> dict:
    """AIの返答や手で直した内容を、決まった形にそろえる（要点とカットに番号を振る）"""
    data = dict(data)
    data.setdefault("version", 1)
    data.setdefault("title", data.get("theme", ""))
    for k in ("concept", "audience", "genre", "theme", "name"):
        data.setdefault(k, "")
    data.setdefault("minutes", 0)
    secs = []
    used_p = {p.get("id") for s in data.get("sections", []) for p in s.get("points", []) if p.get("id")}
    used_s = {k.get("id") for k in data.get("shots", []) if k.get("id")}
    np_ = max([int(i[1:]) for i in used_p if re.fullmatch(r"P\d+", str(i))] or [0])
    ns_ = max([int(i[1:]) for i in used_s if re.fullmatch(r"K\d+", str(i))] or [0])
    for s in data.get("sections", []):
        pts = []
        for p in s.get("points", []):
            if isinstance(p, str):
                p = {"text": p}
            p = {"text": str(p.get("text", "")).strip(), "keywords": [str(k) for k in p.get("keywords", []) if str(k).strip()],
                 "must": bool(p.get("must", False)), "id": p.get("id")}
            if not p["text"]:
                continue
            if not p["id"]:
                np_ += 1
                p["id"] = f"P{np_}"
            pts.append(p)
        secs.append({"name": str(s.get("name", "")), "purpose": str(s.get("purpose", "")),
                     "minutes": s.get("minutes", 0) or 0, "points": pts})
    data["sections"] = secs
    shots = []
    for k in data.get("shots", []):
        kind = k.get("kind") if k.get("kind") in SHOT_KINDS else "insert"
        sec = k.get("section", 0)
        try:
            sec = int(sec)
        except (TypeError, ValueError):
            sec = 0
        k = {"id": k.get("id"), "section": sec if 1 <= sec <= len(secs) else 0, "kind": kind,
             "what": str(k.get("what", "")).strip(), "tip": str(k.get("tip", "")).strip(),
             "must": bool(k.get("must", False)), "done": bool(k.get("done", False))}
        if not k["what"]:
            continue
        if not k["id"]:
            ns_ += 1
            k["id"] = f"K{ns_}"
        shots.append(k)
    data["shots"] = shots
    data["props"] = [str(x) for x in data.get("props", []) if str(x).strip()]
    data["title_ideas"] = [str(x) for x in data.get("title_ideas", []) if str(x).strip()]
    return data


def all_points(data: dict):
    for si, s in enumerate(data["sections"], 1):
        for p in s["points"]:
            yield si, s, p


def _plan_instructions(genre: str) -> str:
    gname, ghint = GENRES.get(genre, GENRES["other"])
    kinds = "\n".join(f"  - {k}: {SHOT_KINDS[k]}（{SHOT_HINT[k]}）" for k in SHOT_KINDS)
    return f"""あなたは日本の個人YouTuberの企画・構成作家です。動画のテーマから、撮影前に使う「企画書」を作ってください。
ジャンル: {gname}。{ghint}

- sections: 話の流れ（構成）。オープニングから締めまで。各部分の目安の長さ（分）を付ける
- points: 各部分で話す要点（台本）。読み上げ原稿ではなく、話し忘れを防ぐための短いメモ（1つ40字以内）
  - keywords: 撮影後に文字起こしで「話したか」を確かめるための言葉を2〜4個。話すときに実際に口にしそうな名詞にする。
    表記ゆれは「|」で区切って1つにまとめる（例: 重さ|おもさ）
  - must: 必ず話すことは true
- shots: 撮影リスト。kind は次のどれか
{kinds}
  - section は使う構成の番号（1から）。編集で困らないよう、Bロールと挿入カットも具体的に挙げる
- props: 撮影に必要な道具・持ち物（三脚・充電器など撮影機材も）
- title_ideas: タイトル案を3〜5個（誇張しすぎず、内容が伝わるもの）

やさしい日本語で書いてください。出力はJSONのみです。"""


def _plan_user_text(req: dict) -> str:
    lines = [f"テーマ: {req.get('theme', '')}"]
    if req.get("minutes"):
        lines.append(f"完成動画の長さの目安: {req['minutes']}分")
    if req.get("notes"):
        lines.append(f"メモ（伝えたいこと・使う道具・撮影場所など）:\n{req['notes']}")
    return "\n".join(lines)


def generate_plan_api(req: dict, effort: str = "medium") -> tuple[dict, dict]:
    import rc_llm
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    print("Claudeで構成案・台本・撮影リストを作成中…", flush=True)
    try:
        res = rc_llm._call(client, anthropic, _plan_instructions(req.get("genre", "talk")), _plan_user_text(req),
                           PLAN_SCHEMA, effort, usage)
    except anthropic.APIStatusError as e:
        sys.exit(f"[エラー] Claude API: {e.status_code} {e.message}")
    except anthropic.APIConnectionError as e:
        sys.exit(f"[エラー] Claude APIに接続できません: {e}")
    return res, usage


def write_manual_plan_prompt(req: dict, path: Path) -> None:
    schema = json.dumps(PLAN_SCHEMA, ensure_ascii=False, indent=1)
    path.write_text(_plan_instructions(req.get("genre", "talk"))
                    + f"\n\n## 出力するJSONの形式（JSON Schema）\n```json\n{schema}\n```\n\n## 依頼\n"
                    + _plan_user_text(req), encoding="utf-8")


SKELETONS = {
    "talk": [("オープニング", "何の動画か・結論を先に", ["今日紹介するもの", "結論をひとことで"]),
             ("基本情報", "スペック・値段など", ["値段・買った場所", "大きさ・重さ"]),
             ("実際に使ってみる", "実演", ["使ってみた感想", "良かったところ", "気になったところ"]),
             ("まとめ", "おすすめの人", ["こんな人におすすめ", "チャンネル登録のお願い"])],
    "craft": [("オープニング", "完成品のチラ見せ", ["今日作るもの", "完成品を見せる"]),
              ("材料と道具", "", ["使う材料", "使う道具"]),
              ("作り方", "手順ごとに", ["手順1", "手順2", "失敗しやすいところ"]),
              ("完成・使ってみる", "", ["完成品の紹介", "使ってみた感想"]),
              ("まとめ", "", ["かかった時間と費用", "次回の予告"])],
    "outdoor": [("オープニング", "企画とルールの説明", ["今日の企画", "ルール・目標"]),
                ("移動・準備", "", ["場所の紹介", "意気込み"]),
                ("挑戦", "山場", ["途中経過", "ハプニング"]),
                ("結果", "", ["結果発表", "感想"]),
                ("エンディング", "", ["次回の予告"])],
    "mv": [("イントロ", "", ["曲の世界観"]), ("Aメロ", "", ["映像のイメージ"]), ("サビ", "見せ場", ["演奏・歌の見せ場"]),
           ("アウトロ", "", ["余韻"])],
    "other": [("オープニング", "", ["何の動画か"]), ("本編", "", ["伝えたいこと"]), ("まとめ", "", ["締めのひとこと"])],
}


def skeleton_plan(req: dict) -> dict:
    """AIを使わないときのひな形（ジャンルごとの定番の流れ）。中身は画面か plan.json で直す"""
    genre = req.get("genre", "talk")
    secs = [{"name": n, "purpose": p, "minutes": 0, "points": [{"text": t, "keywords": [], "must": False} for t in pts]}
            for n, p, pts in SKELETONS.get(genre, SKELETONS["other"])]
    shots = [{"section": 1, "kind": "front", "what": "オープニングのあいさつ", "tip": "", "must": True}]
    if genre == "craft":
        shots += [{"section": 3, "kind": "hands", "what": "作業の手元", "tip": "2台目カメラで真上から", "must": True},
                  {"section": 2, "kind": "insert", "what": "材料と道具を並べたところ", "tip": "", "must": False}]
    elif genre == "outdoor":
        shots += [{"section": 2, "kind": "broll", "what": "景色・場所の看板", "tip": "5秒以上ゆっくり", "must": True}]
    else:
        shots += [{"section": 2, "kind": "insert", "what": "紹介するもののアップ", "tip": "", "must": True},
                  {"section": 3, "kind": "broll", "what": "使っている様子", "tip": "", "must": False}]
    return {"title": req.get("theme", ""), "concept": "", "audience": "", "minutes": req.get("minutes") or 0,
            "sections": secs, "shots": shots, "props": [], "title_ideas": []}


def make_project(req: dict, plan: dict, source: str) -> dict:
    data = dict(plan)
    data.update({"name": req.get("name") or req.get("theme", ""), "theme": req.get("theme", ""),
                 "genre": req.get("genre", "talk"), "request": req, "source": source,
                 "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    if not data.get("minutes"):
        data["minutes"] = req.get("minutes") or 0
    return normalize(data)


# ============================================================
#  印刷用（Markdown / HTML）
# ============================================================
def _min(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return f"約{f:g}分"


def to_markdown(data: dict) -> str:
    g = GENRES.get(data.get("genre"), GENRES["other"])[0]
    L = [f"# {data.get('title') or data.get('name')}", ""]
    L.append(f"- ジャンル: {g}")
    if data.get("theme"):
        L.append(f"- テーマ: {data['theme']}")
    if _min(data.get("minutes")):
        L.append(f"- 長さの目安: {_min(data['minutes'])}")
    if data.get("audience"):
        L.append(f"- 見てほしい人: {data['audience']}")
    if data.get("concept"):
        L += ["", f"> {data['concept']}"]
    L += ["", "## 構成と台本（話す要点）", ""]
    for si, s in enumerate(data["sections"], 1):
        L.append(f"### {si}. {s['name']}" + (f"（{_min(s.get('minutes'))}）" if _min(s.get("minutes")) else ""))
        if s.get("purpose"):
            L.append(f"_{s['purpose']}_")
        L.append("")
        for p in s["points"]:
            L.append(f"- [ ] {'★' if p['must'] else ''}{p['text']}")
        L.append("")
    L += ["## 撮影リスト", "", "| 済 | 種類 | 使う場面 | 撮るもの | コツ |", "|---|---|---|---|---|"]
    for k in data["shots"]:
        sec = data["sections"][k["section"] - 1]["name"] if k["section"] else ""
        L.append(f"| {'☑' if k['done'] else '☐'} | {SHOT_KINDS[k['kind']]} | {sec} | "
                 f"{'★' if k['must'] else ''}{k['what'].replace('|', '｜')} | {k['tip'].replace('|', '｜')} |")
    if data["props"]:
        L += ["", "## 準備するもの", ""] + [f"- [ ] {x}" for x in data["props"]]
    if data["title_ideas"]:
        L += ["", "## タイトル案", ""] + [f"- {x}" for x in data["title_ideas"]]
    L += ["", "★ = 必ず話す・必ず撮る", ""]
    return "\n".join(L)


HTML_CSS = """
:root { --ink:#1A202C; --sub:#4A5568; --line:#CBD5E0; --accent:#2B6CB0; --soft:#EBF4FF; }
* { box-sizing: border-box; }
body { margin: 0; background: #F7FAFC; color: var(--ink);
  font-family: "BIZ UDPGothic", "Yu Gothic UI", "Meiryo", "Hiragino Sans", "Noto Sans CJK JP", sans-serif;
  line-height: 1.6; }
.page { max-width: 820px; margin: 24px auto; background: #fff; padding: 32px 40px; border: 1px solid var(--line); }
h1 { font-size: 24px; margin: 0 0 8px; border-bottom: 3px solid var(--accent); padding-bottom: 6px; }
h2 { font-size: 18px; margin: 28px 0 8px; color: var(--accent); }
h3 { font-size: 15px; margin: 16px 0 4px; }
.meta { color: var(--sub); font-size: 13px; margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: 4px 18px; }
.concept { background: var(--soft); padding: 8px 12px; border-left: 4px solid var(--accent); margin: 12px 0; }
.sec { break-inside: avoid; border: 1px solid var(--line); border-radius: 6px; padding: 8px 14px; margin: 8px 0; }
.sec .purpose { color: var(--sub); font-size: 13px; }
.sec .min { color: var(--sub); font-weight: normal; font-size: 13px; }
ul.check { list-style: none; padding-left: 4px; margin: 6px 0; }
ul.check li { padding: 2px 0 2px 26px; position: relative; }
ul.check li::before { content: ""; position: absolute; left: 0; top: 6px; width: 14px; height: 14px; border: 1.5px solid var(--sub); border-radius: 2px; }
ul.check li.done::before { content: "✓"; font-size: 12px; line-height: 14px; text-align: center; }
.must { color: #C53030; font-weight: bold; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid var(--line); padding: 4px 6px; text-align: left; vertical-align: top; }
th { background: var(--soft); }
td.box { width: 28px; text-align: center; font-size: 15px; }
.kind { white-space: nowrap; }
.note { color: var(--sub); font-size: 12px; margin-top: 18px; }
@media print {
  body { background: #fff; }
  .page { margin: 0; border: none; padding: 0; max-width: none; }
  @page { size: A4; margin: 14mm; }
  .noprint { display: none; }
}
@media (max-width: 600px) { .page { padding: 16px; margin: 0; } }
"""


def to_html(data: dict) -> str:
    e = html.escape
    g = GENRES.get(data.get("genre"), GENRES["other"])[0]
    meta = [f"<li>ジャンル: {e(g)}</li>"]
    if data.get("theme"):
        meta.append(f"<li>テーマ: {e(data['theme'])}</li>")
    if _min(data.get("minutes")):
        meta.append(f"<li>長さの目安: {_min(data['minutes'])}</li>")
    if data.get("audience"):
        meta.append(f"<li>見てほしい人: {e(data['audience'])}</li>")
    body = [f"<h1>{e(data.get('title') or data.get('name', ''))}</h1>", f"<ul class='meta'>{''.join(meta)}</ul>"]
    if data.get("concept"):
        body.append(f"<div class='concept'>{e(data['concept'])}</div>")
    body.append("<h2>構成と台本（話す要点）</h2>")
    for si, s in enumerate(data["sections"], 1):
        m = f" <span class='min'>（{_min(s.get('minutes'))}）</span>" if _min(s.get("minutes")) else ""
        pts = "".join(f"<li>{'<span class=must>★</span>' if p['must'] else ''}{e(p['text'])}</li>" for p in s["points"])
        purpose = f"<div class='purpose'>{e(s['purpose'])}</div>" if s.get("purpose") else ""
        body.append(f"<div class='sec'><h3>{si}. {e(s['name'])}{m}</h3>{purpose}<ul class='check'>{pts}</ul></div>")
    body.append("<h2>撮影リスト</h2><table><tr><th>済</th><th>種類</th><th>使う場面</th><th>撮るもの</th><th>コツ</th></tr>")
    for k in data["shots"]:
        sec = data["sections"][k["section"] - 1]["name"] if k["section"] else ""
        body.append(f"<tr><td class='box'>{'☑' if k['done'] else '☐'}</td><td class='kind'>{SHOT_KINDS[k['kind']]}</td>"
                    f"<td>{e(sec)}</td><td>{'<span class=must>★</span>' if k['must'] else ''}{e(k['what'])}</td>"
                    f"<td>{e(k['tip'])}</td></tr>")
    body.append("</table>")
    if data["props"]:
        body.append("<h2>準備するもの</h2><ul class='check'>" + "".join(f"<li>{e(x)}</li>" for x in data["props"]) + "</ul>")
    if data["title_ideas"]:
        body.append("<h2>タイトル案</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in data["title_ideas"]) + "</ul>")
    body.append("<div class='note'>★ = 必ず話す・必ず撮る　／　ブラウザの「印刷」（Ctrl+P）で紙やPDFにできます</div>")
    title = e(data.get("title") or data.get("name", "企画"))
    return (f"<!doctype html>\n<html lang='ja'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>{title}</title>"
            f"<style>{HTML_CSS}</style></head><body><div class='page'>\n" + "\n".join(body) + "\n</div></body></html>\n")


# ============================================================
#  撮り漏れチェック
# ============================================================
def load_roughcut(out: Path) -> tuple[list[dict], set[int], dict]:
    chunks = _read_json(out / "chunks.json")
    sess = _read_json(out / "session.json", {}) or {}
    if not isinstance(chunks, list):
        sys.exit(f"[エラー] {out} に chunks.json がありません。先に荒編集を実行してください。")
    corr = {int(k): v for k, v in (sess.get("corrections") or {}).items()}
    for c in chunks:
        if c["id"] in corr:
            c["text"] = corr[c["id"]]
    return chunks, set(sess.get("cut_ids", [])), sess


def link_path(out: Path) -> Path:
    return out / "preprod_link.json"


def linked_project(out: Path) -> Path | None:
    d = _read_json(link_path(out), {}) or {}
    p = d.get("project")
    return Path(p) if p and (Path(p) / "plan.json").exists() else None


def set_link(out: Path, pdir: Path):
    _write(link_path(out), json.dumps({"project": str(Path(pdir).resolve())}, ensure_ascii=False))


def _find_kw(kws: list[str], norms: list[str], live: list[bool] | None = None, window: int = 3):
    """連続する window 個の発言の中で、キーワードがいちばん多く出てくる所を探す
    （同じ数なら、カットしていない発言で話している所を選ぶ）"""
    alts = [_alts(k) for k in kws]
    alts = [a for a in alts if a]
    if not alts:
        return 0, 0, []
    live = live or [True] * len(norms)
    hit_rows = [[any(a in n for a in al) for al in alts] for n in norms]
    best, best_i = (0, 0), -1
    for i in range(len(norms)):
        seen = [False] * len(alts)
        seen_live = [False] * len(alts)
        for j in range(i, min(i + window, len(norms))):
            seen = [s or h for s, h in zip(seen, hit_rows[j])]
            if live[j]:
                seen_live = [s or h for s, h in zip(seen_live, hit_rows[j])]
        score = (sum(seen), sum(seen_live))
        if score > best:
            best, best_i = score, i
    ids = [] if best_i < 0 else [j for j in range(best_i, min(best_i + window, len(norms))) if any(hit_rows[j])]
    return best[0], len(alts), ids


def local_check(data: dict, chunks: list[dict], cut_ids: set[int], has_cam2: bool = False) -> dict:
    """言葉の一致で照合する（AIを使わない・すぐ終わる）"""
    norms = [fold(c["text"]) for c in chunks]
    live = [c["id"] not in cut_ids for c in chunks]
    points, shots = [], []
    for _si, _s, p in all_points(data):
        kws = p["keywords"] or auto_keywords(p["text"])
        hit, n, idx = _find_kw(kws, norms, live)
        if n == 0:
            status, note = "unknown", "確かめる言葉がありません（企画タブで「確かめる言葉」を入れてください）"
        else:
            need = max(1, math.ceil(n * 0.5))
            status = "said" if hit >= need else "partial" if hit > 0 else "missing"
            note = f"言葉の一致 {hit}/{n}（{'・'.join(kws)}）"
        points.append({"id": p["id"], "status": status, "chunk_ids": [chunks[i]["id"] for i in idx], "note": note})
    for k in data["shots"]:
        kws = auto_keywords(k["what"])
        hit, n, idx = _find_kw(kws, norms, live) if kws else (0, 0, [])
        if k["kind"] == "hands" and has_cam2:
            shots.append({"id": k["id"], "status": "likely", "chunk_ids": [chunks[i]["id"] for i in idx],
                          "note": "2台目カメラの素材があります（手元が映っているか確認してください）"})
        elif n and hit >= max(1, math.ceil(n * 0.5)):
            shots.append({"id": k["id"], "status": "likely", "chunk_ids": [chunks[i]["id"] for i in idx],
                          "note": "それらしい発言があります（その場面の映像を確認してください）"})
        elif k["kind"] == "front" and chunks:
            shots.append({"id": k["id"], "status": "likely", "chunk_ids": [chunks[i]["id"] for i in idx],
                          "note": "メインカメラの素材があります（正面で撮れているか確認してください）"})
        else:
            shots.append({"id": k["id"], "status": "missing", "chunk_ids": [],
                          "note": "発言からは確認できません（映像を見て、撮れていたら「撮った」に印を）"})
    return {"points": points, "shots": shots}


def _check_instructions() -> str:
    return """あなたはYouTube動画の編集アシスタントです。撮影前に作った「台本（話す要点）」と「撮影リスト」を、
撮影素材の文字起こしと照らし合わせて、話し忘れ・撮り忘れを見つけてください。

- points: 台本の要点ごとに
  - said: 内容をきちんと話している / partial: 少しだけ・あいまいに触れている / missing: 話していない
  - chunk_ids: 話している発言の番号（#の数字）。missing なら空
  - note: 判断の理由を短く（足りないことがあれば具体的に）
- shots: 撮影リストのカットごとに、発言から撮れていそうか判断する
  - likely: その場面を撮っていそうな発言がある（「手元を映します」「これが完成品です」など）
  - missing: 発言からは撮った様子が分からない
  - chunk_ids: 根拠の発言の番号
- 「(カット済)」の付いた発言は、荒編集でカットされた発言です。話していても使われないので、
  カット済の発言でしか話していない要点は note にそのことを書いてください
- 「(参考)」の付いた行は前後関係の参考用です
- id は台本・撮影リストの ID（P1, K1 など）をそのまま使います

出力はJSONのみです。"""


def _script_text(data: dict) -> str:
    L = ["## 台本（話す要点）"]
    for si, s, p in all_points(data):
        L.append(f"{p['id']} [{si}. {s['name']}] {'(必須) ' if p['must'] else ''}{p['text']}")
    L.append("\n## 撮影リスト")
    for k in data["shots"]:
        sec = data["sections"][k["section"] - 1]["name"] if k["section"] else ""
        L.append(f"{k['id']} [{SHOT_KINDS[k['kind']]}] {sec}: {k['what']}")
    return "\n".join(L)


def _transcript_lines(chunks: list[dict], cut_ids: set[int], ref_ids: set[int] = frozenset()) -> str:
    from rc_transcribe import fmt_ts
    out = []
    for c in chunks:
        tag = ("(参考) " if c["id"] in ref_ids else "") + ("(カット済) " if c["id"] in cut_ids else "")
        out.append(f"{tag}#{c['id']} [{fmt_ts(c['start'])}] {c['text']}")
    return "\n".join(out)


def _windows(chunks: list[dict], max_chars: int = 80_000, context: int = 10):
    start = 0
    while start < len(chunks):
        size, end = 0, start
        while end < len(chunks) and size < max_chars:
            size += len(chunks[end]["text"]) + 24
            end += 1
        ctx = max(0, start - context)
        yield chunks[ctx:end], {c["id"] for c in chunks[ctx:start]}
        start = end


RANK_P = {"said": 2, "partial": 1, "missing": 0}
RANK_S = {"likely": 1, "missing": 0}


def merge_ai(data: dict, results: list[dict], valid_ids: set[int]) -> dict:
    """窓ごとの AI の判定をまとめる（一番よく話せている窓の判定を使う）"""
    pid = {p["id"] for _a, _b, p in all_points(data)}
    sid = {k["id"] for k in data["shots"]}
    best: dict[str, dict] = {}
    for res in results:
        for key, ids, rank in (("points", pid, RANK_P), ("shots", sid, RANK_S)):
            for it in res.get(key, []):
                if it.get("id") not in ids or it.get("status") not in rank:
                    continue
                it = dict(it, chunk_ids=[i for i in it.get("chunk_ids", []) if i in valid_ids])
                cur = best.get(it["id"])
                if cur is None or rank[it["status"]] > rank[cur["status"]]:
                    best[it["id"]] = it
                elif rank[it["status"]] == rank[cur["status"]] and it["status"] != "missing":
                    cur["chunk_ids"] = cur["chunk_ids"] + [i for i in it["chunk_ids"] if i not in cur["chunk_ids"]]
    pts = [best.get(p["id"], {"id": p["id"], "status": "missing", "chunk_ids": [], "note": ""})
           for _a, _b, p in all_points(data)]
    shots = [best.get(k["id"], {"id": k["id"], "status": "missing", "chunk_ids": [], "note": ""}) for k in data["shots"]]
    return {"points": pts, "shots": shots}


def ai_check(data: dict, chunks: list[dict], cut_ids: set[int], effort: str = "medium") -> tuple[dict, dict]:
    import rc_llm
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    wins = list(_windows(chunks))
    results = []
    for i, (win, ref) in enumerate(wins, 1):
        print(f"Claudeで台本と照合中… ({i}/{len(wins)})", flush=True)
        user = _script_text(data) + "\n\n## 文字起こし\n" + _transcript_lines(win, cut_ids, ref)
        try:
            results.append(rc_llm._call(client, anthropic, _check_instructions(), user, CHECK_SCHEMA, effort, usage))
        except anthropic.APIStatusError as e:
            sys.exit(f"[エラー] Claude API: {e.status_code} {e.message}")
        except anthropic.APIConnectionError as e:
            sys.exit(f"[エラー] Claude APIに接続できません: {e}")
    return merge_ai(data, results, {c["id"] for c in chunks}), usage


def write_manual_check_prompt(data: dict, chunks: list[dict], cut_ids: set[int], path: Path) -> None:
    schema = json.dumps(CHECK_SCHEMA, ensure_ascii=False, indent=1)
    path.write_text(_check_instructions() + f"\n\n## 出力するJSONの形式（JSON Schema）\n```json\n{schema}\n```\n\n"
                    + _script_text(data) + "\n\n## 文字起こし\n" + _transcript_lines(chunks, cut_ids), encoding="utf-8")


def finalize_check(data: dict, raw: dict, chunks: list[dict], cut_ids: set[int], method: str, pdir: Path) -> dict:
    """判定に、台本の文・時刻・カット済みかどうか・「撮った」の印を足して、表示しやすい形にする"""
    by_id = {c["id"]: c for c in chunks}
    rp = {r["id"]: r for r in raw.get("points", [])}
    rs = {r["id"]: r for r in raw.get("shots", [])}
    points, shots = [], []
    for si, s, p in all_points(data):
        r = rp.get(p["id"], {"status": "unknown", "chunk_ids": [], "note": ""})
        ids = [i for i in r.get("chunk_ids", []) if i in by_id]
        status = r.get("status", "unknown")
        if status in ("said", "partial") and ids and all(i in cut_ids for i in ids):
            status = "cut"
        live = [i for i in ids if i not in cut_ids] or ids
        points.append({"id": p["id"], "section": si, "section_name": s["name"], "text": p["text"], "must": p["must"],
                       "status": status, "chunk_ids": ids, "t": by_id[live[0]]["start"] if live else None,
                       "note": r.get("note", "")})
    for k in data["shots"]:
        r = rs.get(k["id"], {"status": "missing", "chunk_ids": [], "note": ""})
        ids = [i for i in r.get("chunk_ids", []) if i in by_id]
        status = "done" if k.get("done") else r.get("status", "missing")
        sec = data["sections"][k["section"] - 1]["name"] if k["section"] else ""
        shots.append({"id": k["id"], "section": k["section"], "section_name": sec, "kind": k["kind"],
                      "what": k["what"], "must": k["must"], "status": status, "auto_status": r.get("status", "missing"),
                      "chunk_ids": ids,
                      "t": by_id[ids[0]]["start"] if ids else None, "note": r.get("note", "")})
    counts = {"points": len(points), "missing": sum(p["status"] in ("missing", "cut") for p in points),
              "partial": sum(p["status"] == "partial" for p in points),
              "shots": len(shots), "shots_missing": sum(s["status"] == "missing" for s in shots)}
    return {"version": 1, "project": str(Path(pdir).resolve()), "project_name": data.get("name", ""),
            "title": data.get("title", ""), "method": method, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "points": points, "shots": shots, "counts": counts}


def check_markdown(res: dict) -> str:
    from rc_transcribe import fmt_ts
    c = res["counts"]
    L = [f"# 撮り漏れチェック：{res.get('title') or res.get('project_name')}", "",
         f"- 照合の方法: {'Claude' if res['method'] == 'ai' else '言葉の一致（AIなし）'}　{res['created']}",
         f"- 話していない要点: {c['missing']} / {c['points']}（一部だけ: {c['partial']}）",
         f"- 撮った記録がないカット: {c['shots_missing']} / {c['shots']}", ""]
    bad = [p for p in res["points"] if p["status"] in ("missing", "cut", "partial", "unknown")]
    L += ["## 話し忘れ・話し足りない要点", ""]
    if not bad:
        L.append("- なし（台本の要点はすべて話しています）")
    for p in bad:
        t = f"（{fmt_ts(p['t'])}）" if p["t"] is not None else ""
        L.append(f"- [{POINT_STATUS[p['status']]}] {'★' if p['must'] else ''}{p['section_name']}：{p['text']}{t}"
                 + (f" — {p['note']}" if p["note"] else ""))
    miss = [s for s in res["shots"] if s["status"] == "missing"]
    L += ["", "## 撮った記録がないカット", ""]
    if not miss:
        L.append("- なし")
    for s in miss:
        L.append(f"- [{SHOT_KINDS[s['kind']]}] {'★' if s['must'] else ''}{s['section_name']}：{s['what']}")
    L += ["", "## すべての要点", "", "| 状態 | 場面 | 要点 | 時刻 |", "|---|---|---|---|"]
    for p in res["points"]:
        t = fmt_ts(p["t"]) if p["t"] is not None else ""
        L.append(f"| {POINT_STATUS[p['status']]} | {p['section_name']} | {p['text'].replace('|', '｜')} | {t} |")
    L += ["", "※ 映像そのものは見ていません。カットの「たぶん撮れている」は発言からの推測です。", ""]
    return "\n".join(L)


def save_check(out: Path, res: dict) -> Path:
    _write(out / "preprod_check.json", json.dumps(res, ensure_ascii=False, indent=1))
    _write(out / "preprod_check.md", check_markdown(res))
    return out / "preprod_check.json"


def load_check(out: Path) -> dict | None:
    d = _read_json(out / "preprod_check.json")
    return d if isinstance(d, dict) else None
