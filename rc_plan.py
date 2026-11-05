"""AIによる演出プラン（テロップ・ズーム・BGM・効果音）の作成。

プランは plan.json に保存され、人が書き換えてから書き出し直すこともできます。
"""
from __future__ import annotations

import json
from pathlib import Path

import rc_llm
from rc_graphics import CARD_STYLES
from rc_template import MOODS, SE_KINDS
from rc_transcribe import fmt_ts

MODE_PLAN_HINT = {
    "talk": "レビュー・解説動画。テロップは30〜60秒に1つ程度、ズームは20〜40秒に1回程度。テンポよく。",
    "craft": "工作動画。テロップは工程の区切りや重要な注意点に。ズームは控えめ（60秒に1回程度）。",
    "outdoor": "屋外企画。テロップはリアクションや場所・状況の説明に。ズームはほぼ使わない。",
}

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["telops", "zooms", "bgm"],
    "properties": {
        "telops": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["start_id", "end_id", "text", "style", "se"],
                "properties": {
                    "start_id": {"type": "integer"},
                    "end_id": {"type": "integer"},
                    "text": {"type": "string"},
                    "style": {"type": "string", "enum": ["emphasis", "info", "warning", "funny", *CARD_STYLES]},
                    "se": {"type": "string", "enum": ["none"] + SE_KINDS},
                },
            },
        },
        "zooms": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "strength"],
                "properties": {"id": {"type": "integer"},
                               "strength": {"type": "string", "enum": ["small", "medium"]}},
            },
        },
        "bgm": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["start_id", "mood"],
                "properties": {"start_id": {"type": "integer"},
                               "mood": {"type": "string", "enum": MOODS + ["none"]}},
            },
        },
    },
}


def _instructions(mode: str, available_moods: list[str]) -> str:
    moods = "、".join(available_moods) if available_moods else "（BGMは未登録なので bgm は空でよい）"
    return f"""あなたはYouTube動画の編集者です。カット済みの動画の文字起こしを読んで、仕上げの演出プランを作ってください。
{MODE_PLAN_HINT[mode]}

## 入力の形式
`#ID [編集後の時刻] テキスト`（字幕はすべての発言に自動で付くので、テロップは字幕とは別の「強調」です）

## 作るもの
- telops: 強調テロップ。start_id〜end_id の発言の間、画面に出ます
  - text は**15字以内**の短い言葉（発言そのままではなく要点。例: 「重さ わずか1.2kg！」「ここ注意」）
  - style: emphasis=大事なポイント / info=補足情報（商品名・数値・場所など）/ warning=注意 / funny=ツッコミ・面白い所
  - カード型の style（text の書き方が決まっています。「／」で改行。ここだけは15字を超えてよい）:
    - bubble=吹き出し（心の声・ひとこと。例: 「なにこれ！？」）
    - spec=商品スペックのカード（1行目が見出し、2行目から「項目：値」。例: 「マキタ DF033D／重さ：1.2kg／価格：9,800円」）。
      発言に出てきた数値だけを使う
    - compare=比較表（「／」で行、「｜」で列。1行目が見出し。例: 「｜A社｜B社／重さ｜1.2kg｜1.5kg」）。2つ以上を比べている所だけ
    - step=工作の工程のカード（例: 「STEP 2 穴をあける」。工程の始まりに。番号を省くと順に数えます）
    - place=屋外の場所カード（例: 「高尾山 山頂／標高599m」。場所が変わった所に）
    - 解説・レビューでは spec / compare、工作では step、屋外では place を中心に。カード型は1本に数個まで
  - se: テロップと同時に鳴らす効果音（pop=ポンッ / ding=チーン / whoosh=シュッ / boing=ボヨン / none）。鳴らしすぎない
  - 多すぎると見づらいので、本当に効く所だけ
- zooms: 映像をグッと寄せる発言のID。強調したい一言・結論・オチに。連続させない
- bgm: BGMの区間。start_id から次の区間まで、その雰囲気の曲が流れます
  - 使える雰囲気: {moods}（none=BGMなし。真剣な説明や音を聞かせたい所）
  - 最初の区間は必ず最初のIDから。区間は3〜8個程度。細かく切り替えすぎない

出力はJSONのみです。"""


def plan_lines(items: list[tuple[int, float, float, str]]) -> str:
    return "\n".join(f"#{i} [{fmt_ts(s, ms=True)}] {t}" for i, s, e, t in items)


def make_plan_api(items, mode: str, moods: list[str], effort: str) -> tuple[dict, dict]:
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    print("  Claudeで演出プランを作成中…", flush=True)
    plan = rc_llm._call(client, anthropic, _instructions(mode, moods), plan_lines(items), PLAN_SCHEMA, effort, usage)
    return plan, usage


def write_manual_prompt(items, mode: str, moods: list[str], path: Path) -> None:
    schema = json.dumps(PLAN_SCHEMA, ensure_ascii=False, indent=1)
    path.write_text(
        _instructions(mode, moods) + f"\n\n## 出力形式（JSON Schema）\n```json\n{schema}\n```\n\n## 文字起こし\n"
        + plan_lines(items), encoding="utf-8")


def default_plan(items, moods: list[str]) -> dict:
    """AIを使わない場合：BGMを1曲流すだけ"""
    bgm = [{"start_id": items[0][0], "mood": moods[0]}] if items and moods else []
    return {"telops": [], "zooms": [], "bgm": bgm}


def sanitize(plan: dict, valid_ids: set[int], moods: list[str]) -> dict:
    """存在しないIDや、BGMが用意されていない雰囲気を取り除く"""
    out = {"telops": [], "zooms": [], "bgm": []}
    for t in plan.get("telops", []):
        if t.get("start_id") in valid_ids and t.get("text"):
            if t.get("end_id") not in valid_ids or t["end_id"] < t["start_id"]:
                t["end_id"] = t["start_id"]
            t.setdefault("style", "emphasis")
            t.setdefault("se", "none")
            out["telops"].append(t)
    out["zooms"] = [z for z in plan.get("zooms", []) if z.get("id") in valid_ids]
    for b in sorted(plan.get("bgm", []), key=lambda b: b.get("start_id", 0)):
        if b.get("start_id") in valid_ids:
            if b.get("mood") not in moods:
                b["mood"] = "none" if b.get("mood") == "none" or not moods else moods[0]
            out["bgm"].append(b)
    # ほかの機能が足した項目（2台目カメラの "angles" など）は、そのまま引き継ぐ
    out.update({k: v for k, v in plan.items() if k not in out and k != "_off"})
    if "broll" in plan:   # Bロール（差し込み素材。rc_broll.py）
        import rc_broll
        out["broll"] = rc_broll.sanitize(plan["broll"], valid_ids)
    return out
