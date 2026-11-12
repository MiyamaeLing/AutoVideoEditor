"""字幕の文を、AI が読みやすく整える（落ち着いたスタイル。rc_style）。

話した言葉をそのまま字幕にすると、言い直し・「えー」「まあ」・「9ミリ」のような書き方が残って読みにくい。
発言ごとに、意味を変えずに字幕用の文へ直してもらう。発言の数や順番は変えない（時刻は元の発言のまま使う）。
おかしな返事（長すぎる・ほとんど消えている）は使わず、元の文のままにする。
"""
from __future__ import annotations

import rc_llm

BATCH = 150   # 1回に渡す発言の数

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["captions"],
    "properties": {"captions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "text"],
        "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}}}},
}

PROMPT = """あなたは動画の字幕の校正者です。話した言葉の文字起こしを、画面に出す字幕として読みやすく整えてください。

## 入力
`#ID テキスト`（発言1つが1行。話した順）

## 直し方（意味と話し手の口調は変えない）
- 言い直し・言いかけは、言い直したあとの形だけを残す（「野原工芸野原工芸は」→「野原工芸は」/「全長は138全長は138ミリで」→「全長は138mmで」）
- 発言の終わりが言いかけで、次の発言で言い直しているときは、言いかけの部分を削る
  （「全長は138全長は」の次が「138ミリで少し短めで」→「全長は」）
- 意味のない言葉を削る（えー・あの・まあ・なんか・ちょっと、の口ぐせ。文の頭の「で、」「はい」）。意味のある「ちょっと」などは残す
- 単位と数字は表記にそろえる（9ミリ → 9mm、30グラム → 30g、7700円 → 7,700円、4桁以上の金額にはカンマ）
- 明らかな聞き間違い・誤字だけ直す（前後の発言から分かるもの。固有名詞は、ほかの発言での書き方にそろえる）
- 要約しない・言い換えない・情報を足さない。語尾（〜です／〜ですね／〜と思います）はそのまま
- 句点「。」は付けない。読点「、」は、読みやすくなる所にだけ
- 1つの発言が前後の発言と文の途中でつながっていても、その発言の分だけを返す（ほかの発言の言葉を持ってこない）
- 中身が口ぐせ・言いかけだけで、残す言葉が無い発言は、text を空にする

全部の ID について、同じ ID で1つずつ返してください。出力はJSONのみです。"""


def accept(old: str, new: str) -> bool:
    """整えた文を使ってよいか（長くなりすぎ・削りすぎは使わない）"""
    old, new = old.strip(), new.strip()
    if not new:
        return len(old) <= 8          # 短い口ぐせだけの発言は、消してよい
    if "\n" in new or len(new) > len(old) * 1.3 + 4:
        return False
    return len(new) >= len(old) * 0.4 or len(old) <= 8


def apply(items: list[tuple[int, str]], res: dict) -> dict[str, str]:
    """AIの返事 → {発言ID(文字): 整えた文}。元と同じもの・使えないものは入れない"""
    src = {i: t for i, t in items}
    out = {}
    for c in (res or {}).get("captions", []) or []:
        try:
            i, new = int(c["id"]), str(c.get("text", "")).strip().rstrip("。")
        except (KeyError, TypeError, ValueError):
            continue
        if i in src and new != src[i].strip().rstrip("。") and accept(src[i], new):
            out[str(i)] = new
    return out


def polish_api(items: list[tuple[int, str]], effort: str, usage: dict) -> dict[str, str]:
    """items: [(発言ID, 文)] → {発言ID(文字): 整えた文}。失敗しても止めない（元の文のまま）"""
    items = [(i, t) for i, t in items if str(t).strip()]
    if not items:
        return {}
    out: dict[str, str] = {}
    try:
        anthropic, client = rc_llm._client()
        print("  Claudeで字幕の文を整えています…", flush=True)
        for k in range(0, len(items), BATCH):
            part = items[k:k + BATCH]
            u = {"in": 0, "out": 0}
            res = rc_llm._call(client, anthropic, PROMPT, "\n".join(f"#{i} {t}" for i, t in part), SCHEMA, effort, u)
            for key in u:
                usage[key] = usage.get(key, 0) + u[key]
            out.update(apply(part, res))
    except (Exception, SystemExit) as e:  # noqa: BLE001  字幕の整えに失敗しても、書き出しは続ける
        print(f"  [注意] 字幕の文を整えられませんでした（元の文のまま使います）: {e}")
    print(f"  字幕 {len(out)} か所を整えました（全 {len(items)} 発言）")
    return out


def texts(items, caption_text: dict | None):
    """items: [(ID, 開始, 終了, 文)] に、整えた文を当てはめて返す"""
    ct = caption_text or {}
    return [(i, s, e, ct.get(str(i), t)) for i, s, e, t in items]
