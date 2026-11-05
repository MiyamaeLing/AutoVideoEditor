"""Claude による「どこを切るか」の判定と、タイトル・概要欄・チャプターの生成。

- api   : Claude API を直接呼ぶ（ANTHROPIC_API_KEY が必要）
- manual: claude.ai に貼り付けるためのプロンプトを書き出す（API料金なし）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rc_transcribe import Chunk, fmt_ts

MODEL = os.environ.get("ROUGHCUT_MODEL", "claude-opus-5-5")
PRICE_IN, PRICE_OUT = 4.0, 20.0  # $/100万トークン（claude-opus-5-5）

MODE_HINT = {
    "talk": "レビュー・解説動画です。テンポを重視し、言い直し・NGテイク・不要な前置きは積極的にカットしてください。",
    "craft": "工作・ものづくり動画です。作業の説明は大事なので残し、言い直しやNG、カメラ確認などの不要な発言だけカットしてください。",
    "outdoor": "屋外の企画動画です。その場の雰囲気やリアクションも価値があるので、明らかなNGや撮影の段取りの会話だけをカット候補にしてください。",
}

CUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cuts", "review", "highlights", "corrections"],
    "properties": {
        "cuts": {
            "type": "array",
            "description": "カットするチャンク",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["ids", "type", "reason"],
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}},
                    "type": {"type": "string", "enum": ["retake", "mistake", "filler", "offscript", "other"]},
                    "reason": {"type": "string"},
                },
            },
        },
        "review": {
            "type": "array",
            "description": "カットするか迷ったので人間に確認してほしい箇所",
            "items": {"type": "object", "additionalProperties": False, "required": ["id", "note"],
                      "properties": {"id": {"type": "integer"}, "note": {"type": "string"}}},
        },
        "highlights": {
            "type": "array",
            "description": "サムネイルやショート動画に使えそうな見どころ",
            "items": {"type": "object", "additionalProperties": False, "required": ["id", "label"],
                      "properties": {"id": {"type": "integer"}, "label": {"type": "string"}}},
        },
        "corrections": {
            "type": "array",
            "description": "音声認識の誤りを直した字幕テキスト",
            "items": {"type": "object", "additionalProperties": False, "required": ["id", "text"],
                      "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}},
        },
    },
}

META_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["titles", "description", "chapters", "thumbnail_texts", "tags"],
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "chapters": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False, "required": ["line", "title"],
                      "properties": {"line": {"type": "integer"}, "title": {"type": "string"}}},
        },
        "thumbnail_texts": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


def _cut_instructions(mode: str, glossary: list[str]) -> str:
    g = f"\n固有名詞・専門用語（字幕の誤り訂正に使ってください）: {'、'.join(glossary)}" if glossary else ""
    return f"""あなたはYouTube動画の編集アシスタントです。撮影素材の文字起こしを読んで、荒編集でカットすべき部分を判定してください。
{MODE_HINT[mode]}{g}{channel_notes("cut", mode)}

## 入力の形式
各行が1つの「チャンク」（短い発話のかたまり）です:
`#ID [開始時刻] (+直前の無音秒数) テキスト`

## カットの基準
- retake: 同じ内容を言い直している場合、**最後の成功したテイクだけ残し**、それより前の失敗テイクをカット
- mistake: 言い間違えてすぐ訂正した場合の、間違った部分（「あ、違う」「もう一回」などの言葉も含む）
- filler: 「えー」「あのー」だけのチャンク
- offscript: 撮影の段取り・カメラ確認・独り言（「撮れてる？」「今のカットで」「ちょっと待って」など）
- 説明・情報・リアクションは残す。**迷ったらカットせず review に入れる**
- 音声認識の誤りが明らかなチャンクは corrections に正しいテキストを入れる（話し方や語尾は変えない。言い換え・要約はしない）
- 見どころ（面白い瞬間、結論、完成シーンの発言など）は highlights に短いラベル付きで入れる
- 「(参考)」の付いた行は前後関係の参考用です。判定には含めないでください

出力はJSONのみです。"""


def channel_notes(kind: str, mode: str) -> str:
    """公開した動画の分析（channel_insights.json）から分かった傾向。なければ空（今までどおり）"""
    try:
        import rc_analytics
        return rc_analytics.prompt_notes(kind, mode)
    except Exception:  # noqa: BLE001  分析の情報が読めなくても、編集は止めない
        return ""


def chunk_lines(chunks: list[Chunk], ref_ids: set[int] = frozenset()) -> str:
    lines, prev_end = [], None
    for c in chunks:
        gap = 0 if prev_end is None else c.start - prev_end
        tag = "(参考) " if c.id in ref_ids else ""
        lines.append(f"{tag}#{c.id} [{fmt_ts(c.start, ms=True)}] (+{gap:.1f}s) {c.text}")
        prev_end = c.end
    return "\n".join(lines)


def _windows(chunks: list[Chunk], max_chars: int = 80_000, context: int = 15):
    """長い素材は分けて判定する（前の窓の最後の数行を参考として付ける）"""
    start = 0
    while start < len(chunks):
        size, end = 0, start
        while end < len(chunks) and size < max_chars:
            size += len(chunks[end].text) + 30
            end += 1
        ctx_from = max(0, start - context)
        yield chunks[ctx_from:end], {c.id for c in chunks[ctx_from:start]}
        start = end


def _client():
    try:
        import anthropic
    except ImportError:
        sys.exit("[エラー] anthropic パッケージがありません: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        sys.exit("[エラー] ANTHROPIC_API_KEY が設定されていません。--llm manual なら API キーなしで使えます。")
    return anthropic, anthropic.Anthropic()


def _call(client, anthropic, system: str, user: str | list, schema: dict, effort: str, usage: dict) -> dict:
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        # 安全フィルタで断られた場合に別モデルで自動再実行する
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()
    add_usage(usage, msg)
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claudeがこのリクエストを処理できませんでした（refusal）。--llm manual を試してください。")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("出力が長すぎて途中で切れました。素材を分けて実行してください。")
    return parse_answer(msg.content)


# 別のモデルに切り替わったとき（安全フィルタの fallbacks）の料金（$/100万トークン）。長い名前から順に照合する
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4": (5.0, 25.0),
          "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4": (3.0, 15.0), "claude-haiku-5": (0.10, 0.50),
          "claude-haiku-4": (1.0, 5.0), "claude-fable": (10.0, 50.0), "claude-mythos": (10.0, 50.0)}


def price_of(model) -> tuple[float, float]:
    m = str(model or MODEL)
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k):
            return PRICES[k]
    return PRICE_IN, PRICE_OUT


def add_usage(usage: dict, msg) -> None:
    """使ったトークン数を足す。別のモデルでやり直したときは、断られた分も含めて、モデルごとの料金で数える。

    usage["adj"] は「既定の料金（PRICE_IN/OUT）との差額（ドル）」。cost_usd が足し合わせる。
    """
    u = msg.usage
    its = [it for it in (getattr(u, "iterations", None) or [])
           if getattr(it, "type", "") in ("message", "fallback_message")]
    if its:
        tries = [(getattr(it, "model", None) or (msg.model if it.type == "fallback_message" else MODEL),
                  int(it.input_tokens or 0), int(it.output_tokens or 0)) for it in its]
    else:
        tries = [(getattr(msg, "model", None) or MODEL, int(u.input_tokens or 0), int(u.output_tokens or 0))]
    for model, tin, tout in tries:
        usage["in"] = usage.get("in", 0) + tin
        usage["out"] = usage.get("out", 0) + tout
        pin, pout = price_of(model)
        if (pin, pout) != (PRICE_IN, PRICE_OUT):
            usage["adj"] = usage.get("adj", 0.0) + tin / 1e6 * (pin - PRICE_IN) + tout / 1e6 * (pout - PRICE_OUT)


def parse_answer(content) -> dict:
    """返答の JSON を取り出す。途中で別のモデルに切り替わったとき（fallback）は、切り替わった後の文字だけを使う
    （断られたモデルの書きかけの文字は捨てる）。読めなければ、ほかの失敗と同じ RuntimeError にする。"""
    blocks = list(content or [])
    last = max((i for i, b in enumerate(blocks) if getattr(b, "type", "") == "fallback"), default=-1)
    after = "".join(b.text for b in blocks[last + 1:] if getattr(b, "type", "") == "text")
    tries = [after]
    if last >= 0:   # 書きかけの続きとして書かれた場合に備えて、つなげた文字も試す
        tries.append("".join(b.text for b in blocks if getattr(b, "type", "") == "text"))
    for text in tries:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    raise RuntimeError("Claudeの返答を読み取れませんでした（JSONの形が壊れています）。もう一度試すか、--llm manual を試してください。")


def analyze_cuts_api(chunks: list[Chunk], mode: str, glossary: list[str], effort: str) -> tuple[dict, dict]:
    anthropic, client = _client()
    usage = {"in": 0, "out": 0}
    merged = {"cuts": [], "review": [], "highlights": [], "corrections": []}
    system = _cut_instructions(mode, glossary)
    windows = list(_windows(chunks))
    for i, (win, ref) in enumerate(windows, 1):
        print(f"  Claudeで判定中… ({i}/{len(windows)})", flush=True)
        try:
            res = _call(client, anthropic, system, chunk_lines(win, ref), CUT_SCHEMA, effort, usage)
        except anthropic.APIStatusError as e:
            sys.exit(f"[エラー] Claude API: {e.status_code} {e.message}")
        except anthropic.APIConnectionError as e:
            sys.exit(f"[エラー] Claude APIに接続できません: {e}")
        valid = {c.id for c in win} - ref
        for k in merged:
            for item in res.get(k, []):
                if k == "cuts":
                    item["ids"] = [x for x in item["ids"] if x in valid]
                    if item["ids"]:
                        merged[k].append(item)
                elif item.get("id") in valid:
                    merged[k].append(item)
    return merged, usage


def write_manual_cut_prompt(chunks: list[Chunk], mode: str, glossary: list[str], path: Path) -> None:
    schema = json.dumps(CUT_SCHEMA, ensure_ascii=False, indent=1)
    body = (
        _cut_instructions(mode, glossary)
        + f"\n\n## 出力するJSONの形式（JSON Schema）\n```json\n{schema}\n```\n\n## 文字起こし\n"
        + chunk_lines(chunks)
    )
    path.write_text(body, encoding="utf-8")


def meta_lines(items: list[tuple[float, str]]) -> str:
    return "\n".join(f"L{i} [{fmt_ts(t)}] {txt}" for i, (t, txt) in enumerate(items, 1))


def _meta_instructions(mode: str) -> str:
    return f"""あなたはYouTubeチャンネルの運営アシスタントです。編集済み動画の文字起こしから、投稿用の情報を作ってください。
{MODE_HINT[mode]}

- titles: タイトル案を5つ（誇張しすぎず、内容が伝わり、クリックしたくなるもの。40字以内）
- description: 概要欄の文章（動画の内容の要約3〜5行。チャプターは別に作るので含めない）
- chapters: YouTubeのチャプター。各チャプターの開始行番号（L番号の数字）と短い見出し。最初は必ず line=1。3〜12個程度
- thumbnail_texts: サムネイルに入れる短い文字の案を5つ（10字前後）
- tags: 検索用のタグを10個程度

入力の各行は `L番号 [編集後の時刻] テキスト` です。出力はJSONのみです。"""


def generate_meta_api(items: list[tuple[float, str]], mode: str, effort: str) -> tuple[dict, dict]:
    anthropic, client = _client()
    usage = {"in": 0, "out": 0}
    print("  Claudeでタイトル・概要欄・チャプターを作成中…", flush=True)
    try:
        res = _call(client, anthropic, _meta_instructions(mode), meta_lines(items), META_SCHEMA, effort, usage)
    except anthropic.APIError as e:
        print(f"  [注意] タイトル等の生成に失敗しました: {e}")
        return {}, usage
    return res, usage


def write_manual_meta_prompt(items: list[tuple[float, str]], mode: str, path: Path) -> None:
    schema = json.dumps(META_SCHEMA, ensure_ascii=False, indent=1)
    path.write_text(
        _meta_instructions(mode) + f"\n\n## 出力形式（JSON Schema）\n```json\n{schema}\n```\n\n## 文字起こし\n" + meta_lines(items),
        encoding="utf-8",
    )


def cost_usd(usage: dict) -> float:
    return usage["in"] / 1e6 * PRICE_IN + usage["out"] / 1e6 * PRICE_OUT + float(usage.get("adj", 0.0) or 0.0)
