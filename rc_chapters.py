"""チャプター（章）の位置を、発言のID（チャンクID）で覚えておくための処理。

AI が返すチャプターは「L番号」（編集後の字幕を10秒ごとにまとめた行の番号）です。
行の番号は、カットを変えると別の発言を指してしまうので、最初に当てはめたときに
その行の先頭の発言ID を覚えておき、あとは ID から今のタイムラインの時刻を求めます。
"""
from __future__ import annotations

import json
from pathlib import Path

LINES_FILE = "meta_lines.json"   # claude.ai 用のプロンプトを作ったときの「行番号 → 発言ID」


def build_lines(items: list[tuple[float, float, str]], item_ids: list[int], gap: float = 10.0, pause: float = 0.8):
    """編集後の字幕 (開始, 終了, 文字) を、10秒ごとくらいに1行にまとめる。

    話の間（pause 秒以上）が空いた所でも行を変える。章はその行の先頭の発言から始まるので、
    前の話題の最後の一言と次の話題が同じ行に入ると、章が早く始まってしまうため。

    戻り値: (meta_items=[(開始, 文字)], line_ids=[その行の先頭の発言ID])
    """
    meta_items, line_ids, prev, last_end = [], [], -999.0, -999.0
    for (s, e, t), cid in zip(items, item_ids):
        if not str(t).strip():
            continue
        if meta_items and s - prev < gap and s - last_end < pause:
            meta_items[-1] = (meta_items[-1][0], meta_items[-1][1] + " " + t)
        else:
            meta_items.append((s, t))
            line_ids.append(cid)
            prev = s
        last_end = e
    return meta_items, line_ids


def save_lines(out_dir: Path, line_ids: list[int]) -> None:
    (out_dir / LINES_FILE).write_text(json.dumps({"line_ids": line_ids}), encoding="utf-8")


def load_lines(out_dir: Path) -> list[int] | None:
    p = out_dir / LINES_FILE
    try:
        return [int(x) for x in json.loads(p.read_text(encoding="utf-8"))["line_ids"]]
    except Exception:  # noqa: BLE001
        return None


def chapter_time(tl, chunk) -> float:
    """発言の、編集後タイムラインでの開始時刻（カットされていれば、その直後に残っている位置）"""
    r = tl.map_range(chunk.start, chunk.end)
    return r[0][0] if r else tl.map_time_nearest(chunk.start)


def attach_ids(meta: dict, line_ids: list[int], prompt_line_ids: list[int] | None = None) -> None:
    """meta["chapters"] のうち、まだ発言IDを持っていないものに ID を付ける（meta を書き換える）。

    prompt_line_ids: claude.ai に貼ったプロンプトの行 → 発言ID（あればこちらを優先）
    """
    table = prompt_line_ids or line_ids
    for ch in meta.get("chapters", []) or []:
        if ch.get("id") is not None:
            continue
        idx = int(ch.get("line", 0)) - 1
        if 0 <= idx < len(table):
            ch["id"] = int(table[idx])


def resolve(meta: dict, tl, by_id: dict) -> list[dict]:
    """チャプターを、今のタイムラインの時刻に直す。最初は必ず0秒。

    戻り値: [{"t": 編集後の秒, "title": 見出し, "id": 発言ID, "src": 元素材の秒}]
    """
    chs = [c for c in meta.get("chapters", []) or [] if c.get("id") in by_id]
    chs.sort(key=lambda c: (by_id[c["id"]].start, c.get("line", 0)))
    out = []
    for c in chs:
        chunk = by_id[c["id"]]
        t = 0.0 if not out else round(chapter_time(tl, chunk), 2)
        out.append({"t": t, "title": c["title"], "id": c["id"], "src": round(chunk.start, 3)})
    return out


def retime(chapters: list[dict], tl, by_id: dict) -> list[dict]:
    """session.json に保存したチャプターを、別に作ったタイムライン（fps違いなど）の時刻に直す。

    発言IDのない古い session.json のチャプターは、そのままの時刻を使う。
    """
    out = []
    for c in chapters:
        t = c["t"]
        if c.get("id") in by_id:
            t = 0.0 if not out else round(chapter_time(tl, by_id[c["id"]]), 2)
        out.append({**c, "t": t})
    return out
