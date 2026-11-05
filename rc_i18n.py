"""完成動画の字幕・YouTube のタイトルと概要欄を、Claude でほかの言語（英語など）に翻訳する。

- 字幕：○○_完成.srt → ○○_完成.en.srt（表示の始まりは日本語の字幕に合わせ、1枚の長さは英語の量に合わせて調整）
- タイトル・概要欄：YouTube の「翻訳（localizations）」として設定する。チャプターの時刻はそのまま残す
- 翻訳の結果は translations.json に、字幕の訳は translate_cache.json に保存する。カットを直して
  書き出し直しても、変わっていない字幕は訳し直さない（API料金がかからない）

日本語の字幕は1文の途中で区切られていることが多く、1枚ずつ訳すと英語の語順が崩れる。
そこで、続けて表示される字幕を1つの「まとまり」にして訳し、英語の長さで区切り直す。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 言語コード（YouTube の言語コード）: (日本語の名前, 英語での名前, YouTubeでの表示名, 単語をスペースで区切るか,
#                                       1行の文字数, 1秒に読める文字数)
LANGS = {
    "en": ("英語", "English", "English", True, 42, 17),
    "zh-Hans": ("中国語（簡体字）", "Simplified Chinese", "中文（简体）", False, 16, 7),
    "zh-Hant": ("中国語（繁体字）", "Traditional Chinese", "中文（繁體）", False, 16, 7),
    "ko": ("韓国語", "Korean", "한국어", True, 18, 9),
    "es": ("スペイン語", "Spanish", "Español", True, 42, 17),
    "fr": ("フランス語", "French", "Français", True, 42, 17),
    "de": ("ドイツ語", "German", "Deutsch", True, 42, 16),
    "pt": ("ポルトガル語", "Portuguese", "Português", True, 42, 17),
    "it": ("イタリア語", "Italian", "Italiano", True, 42, 17),
    "id": ("インドネシア語", "Indonesian", "Bahasa Indonesia", True, 42, 17),
    "vi": ("ベトナム語", "Vietnamese", "Tiếng Việt", True, 42, 17),
    "th": ("タイ語", "Thai", "ไทย", False, 35, 15),
    "ru": ("ロシア語", "Russian", "Русский", True, 42, 16),
}
CHAPTER_HEAD_JA = "▼ チャプター"
MODE_JA = {"talk": "レビュー・解説の動画", "craft": "工作・ものづくりの動画", "outdoor": "屋外の企画動画"}
TRANSLATIONS = "translations.json"
CACHE = "translate_cache.json"
WINDOW_CHARS = 5000      # 1回で Claude に渡す日本語の量（長い動画は分けて、同時に4つまで訳す）
CONTEXT_GROUPS = 6       # 前の窓の最後の数まとまりを、参考として付ける


# ---------------------------------------------------------------- 設定
def lang_config(code: str, tpl_cfg: dict | None = None) -> dict:
    """言語ごとの設定。テンプレートの [translate.<言語コード>] があれば、その値で上書きする"""
    ja, en, native, spaced, max_line, cps = LANGS.get(code, (code, code, code, True, 42, 17))
    c = {"code": code, "name_ja": ja, "name_en": en, "track_name": native, "spaced": spaced,
         "max_line": max_line, "lines": 2, "reading_speed": float(cps), "min_duration": 1.0, "max_extend": 1.5}
    over = ((tpl_cfg or {}).get("translate", {}) or {}).get(code, {})
    if isinstance(over, dict):
        for k in ("max_line", "lines", "reading_speed", "min_duration", "max_extend"):
            if k in over:
                c[k] = float(over[k]) if k in ("reading_speed", "min_duration", "max_extend") else int(over[k])
        for k in ("name_en", "track_name"):
            if over.get(k):
                c[k] = str(over[k])
        if "spaced" in over:
            c["spaced"] = bool(over["spaced"])
    return c


def languages(tpl_cfg: dict | None = None, codes: list[str] | None = None) -> list[dict]:
    if not codes:
        codes = ((tpl_cfg or {}).get("translate", {}) or {}).get("languages") or ["en"]
    out, seen = [], set()
    for code in codes:
        code = str(code).strip()
        if code and code != "ja" and code not in seen:
            seen.add(code)
            out.append(lang_config(code, tpl_cfg))
    return out


# ---------------------------------------------------------------- SRT
def _ts(s: str) -> float:
    h, m, rest = s.strip().replace(".", ",").split(":")
    sec, ms = (rest.split(",") + ["0"])[:2]
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")[:3]) / 1000


def read_srt(path: Path, join: str = "") -> list[tuple[float, float, str]]:
    text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    caps = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        i = next((k for k, ln in enumerate(lines) if "-->" in ln), None)
        if i is None:
            continue
        a, b = lines[i].split("-->")
        body = join.join(ln.strip() for ln in lines[i + 1:])
        if body:
            caps.append((_ts(a), _ts(b.split()[0]), body))
    return caps


def write_srt(path: Path, cues: list[tuple[float, float, str]]) -> None:
    from rc_export import write_srt as _w
    _w(path, cues)


# ---------------------------------------------------------------- まとまりと区切り直し
def group_captions(caps: list[tuple[float, float, str]], max_gap: float = 0.05, max_sec: float = 12.0,
                   max_items: int = 4) -> list[dict]:
    """すき間なく続いている字幕（同じ発言を区切ったもの）を、1つのまとまりにする"""
    groups: list[dict] = []
    for s, e, t in caps:
        g = groups[-1] if groups else None
        if g and s - g["e"] <= max_gap and e - g["s"] <= max_sec and len(g["parts"]) < max_items:
            g["parts"].append(t)
            g["e"] = max(g["e"], e)
        else:
            groups.append({"s": s, "e": e, "parts": [t]})
    for i, g in enumerate(groups, 1):
        g["id"] = i
        g["text"] = "".join(g["parts"]) if len(g["parts"]) == 1 else "　".join(g["parts"])
    return groups


_STOP = ".!?…。！？．"          # 文の終わり（ここで切るのが一番良い）
_PUNCT = ",;:、，；：)」』）"   # 文の途中の区切り（ここで切るのも良い）


def _tokens(text: str, spaced: bool) -> list[str]:
    # スペースで区切らない言語は1文字ずつ。ただし数字や英単語（1.2kg など）は途中で切らない
    return text.split() if spaced else re.findall(r"[A-Za-z0-9][A-Za-z0-9.,%'+\-]*|\S|\s", text)


def _join(toks: list[str], spaced: bool) -> str:
    return (" " if spaced else "").join(toks)


def balanced_split(text: str, k: int, spaced: bool, limit: int | None = None) -> list[str]:
    """text を k 個に分ける。長さがそろい、句読点の後ろで切れるほど良い（limit を超える長さは避ける）"""
    toks = _tokens(text, spaced)
    k = max(1, min(k, len(toks)))
    if k == 1:
        return [_join(toks, spaced).strip()] if toks else []
    sep = 1 if spaced else 0
    pre = [0]
    for t in toks:
        pre.append(pre[-1] + len(t) + sep)

    def seglen(i, j):
        return pre[j] - pre[i] - (sep if j > i else 0)

    total = seglen(0, len(toks))
    target = total / k
    n = len(toks)
    INF = float("inf")
    # cost[j][m] = toks[:j] を m 個に分けたときの最小の悪さ
    cost = [[INF] * (k + 1) for _ in range(n + 1)]
    back = [[0] * (k + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for m in range(1, k + 1):
        for j in range(m, n - (k - m) + 1):
            best, arg = INF, 0
            for i in range(m - 1, j):
                if cost[i][m - 1] == INF:
                    continue
                L = seglen(i, j)
                c = abs(L - target)
                if limit and L > limit:
                    c += 1e6 + (L - limit) * 1e3
                if j < n:   # 文の終わりで切れるのが一番良い。句読点でもない所で切るのは悪い
                    last = toks[j - 1][-1:]
                    c += 0 if last in _STOP else target * 0.15 if last in _PUNCT else target * 0.5
                c += cost[i][m - 1]
                if c < best:
                    best, arg = c, i
            cost[j][m], back[j][m] = best, arg
    cuts, j = [], n
    for m in range(k, 0, -1):
        i = back[j][m]
        cuts.append((i, j))
        j = i
    return [_join(toks[i:j], spaced).strip() for i, j in reversed(cuts)]


def wrap_lines(text: str, cfg: dict) -> str:
    """1枚の字幕を、1行の文字数に収まるように（なるべく同じ長さで）改行する"""
    if len(text) <= cfg["max_line"]:
        return text
    k = math.ceil(len(text) / cfg["max_line"])
    return "\n".join(balanced_split(text, k, cfg["spaced"], cfg["max_line"]))


def fit_cues(text: str, s: float, e: float, limit: float, cfg: dict) -> list[tuple[float, float, str]]:
    """訳した文を、表示の始まり s から、読める速さで何枚かの字幕に分ける。

    英語が長くて読み切れないときは、次の字幕が始まる直前（limit）まで表示を延ばす。
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    limit = max(limit, e)
    need = max(len(text) / cfg["reading_speed"], cfg["min_duration"])
    end = min(max(e, s + need), limit)
    cap = cfg["max_line"] * cfg["lines"]
    n = max(1, math.ceil(len(text) / cap))
    n = min(n, max(1, int((end - s) / cfg["min_duration"])))   # 1枚が短くなりすぎないように
    pieces = balanced_split(text, n, cfg["spaced"], cap)
    total = sum(len(p) for p in pieces) or 1
    out, t = [], s
    for i, p in enumerate(pieces):
        d = (end - s) * len(p) / total
        t1 = end if i == len(pieces) - 1 else t + d
        out.append((round(t, 3), round(t1, 3), wrap_lines(p, cfg)))
        t = t1
    return out


def layout(groups: list[dict], texts: dict[int, str], cfg: dict, duration: float | None = None,
           gap: float = 0.08) -> list[tuple[float, float, str]]:
    cues = []
    for i, g in enumerate(groups):
        t = texts.get(g["id"], "")
        if not t:
            continue
        nxt = groups[i + 1]["s"] - gap if i + 1 < len(groups) else g["e"] + cfg["max_extend"]
        limit = min(nxt, g["e"] + cfg["max_extend"])
        if duration:
            limit = min(limit, duration)
        cues += fit_cues(t, g["s"], g["e"], limit, cfg)
    return cues


# ---------------------------------------------------------------- Claude への依頼
LINES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["lines"],
    "properties": {"lines": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "text"],
        "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}}}},
}
META_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "description", "chapter_heading", "chapters"],
    "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                   "chapter_heading": {"type": "string"},
                   "chapters": {"type": "array", "items": {"type": "string"}}},
}


def _context(ctx: dict) -> str:
    out = []
    if ctx.get("title"):
        out.append(f"動画のタイトル: {ctx['title']}")
    if ctx.get("mode_hint"):
        out.append(f"動画の種類: {ctx['mode_hint']}")
    if ctx.get("glossary"):
        out.append(f"固有名詞・専門用語（日本語での書き方）: {'、'.join(ctx['glossary'][:80])}")
    return "\n".join(out)


def lines_instructions(cfg: dict, ctx: dict) -> str:
    return f"""あなたはYouTube動画の字幕の翻訳者です。
日本語の字幕を{cfg['name_ja']}（{cfg['name_en']}）に翻訳してください。
{_context(ctx)}

## 入力の形式
各行が1つの字幕のまとまりです: `#ID [開始時刻] (表示秒数) 日本語`
「　」（全角スペース）は、画面で字幕が切り替わる位置です。

## 翻訳のしかた
- 話し言葉の自然な{cfg['name_ja']}にする。意味を足したり、説明を加えたりしない
- 字幕として読みやすい長さにする。表示秒数 × {cfg['reading_speed']:g} 文字くらいが読める量の目安。超えそうなら、意味を保って短く言い換える
- 1つの文が2つのIDにまたがっているときは、それぞれのIDに、その部分の意味を自然な語順で入れる（文の頭と終わりがずれてもよい）
- 「えー」「あの」などのつなぎの言葉は訳さない。ID全体がそれだけなら text は空文字にする
- 固有名詞は一般的な{cfg['name_ja']}の表記にする（メーカー名・製品名は公式の表記）
- 「(参考)」の付いた行は前後関係を知るためのものです。出力に含めないでください
- 入力のすべてのID（参考を除く）について、id と text を返す。改行は入れない

出力はJSONのみです。"""


def group_lines(groups: list[dict], ref: set[int] = frozenset()) -> str:
    from rc_transcribe import fmt_ts
    return "\n".join(f"{'(参考) ' if g['id'] in ref else ''}#{g['id']} [{fmt_ts(g['s'])}] "
                     f"({g['e'] - g['s']:.1f}秒) {g['text']}" for g in groups)


def windows(groups: list[dict], max_chars: int = WINDOW_CHARS, context: int = CONTEXT_GROUPS):
    start = 0
    while start < len(groups):
        size, end = 0, start
        while end < len(groups) and (size < max_chars or end == start):
            size += len(groups[end]["text"]) + 20
            end += 1
        ctx_from = max(0, start - context)
        yield groups[ctx_from:end], {g["id"] for g in groups[ctx_from:start]}
        start = end


def meta_instructions(cfg: dict, ctx: dict) -> str:
    return f"""あなたはYouTubeチャンネルの運営アシスタントです。日本語の動画のタイトル・概要欄・チャプターの見出しを、
{cfg['name_ja']}（{cfg['name_en']}）を話す視聴者向けに翻訳してください。
{_context({k: v for k, v in ctx.items() if k != 'title'})}

- title: タイトル（100文字以内。直訳より、その言語のYouTubeで自然で内容が伝わるもの。誇張しない）
- description: 概要欄の文章。段落（空行）の区切りはそのまま。URL・@から始まる名前はそのまま残す。
  ハッシュタグ（#〜）は{cfg['name_ja']}にしてよいが、「#」で始まりスペースを含まない形を保つ
- chapter_heading: 「チャプター」という見出しの訳（1〜2語）
- chapters: チャプターの見出しの訳。入力と同じ数・同じ順番で（時刻は付けない）
- 文字「<」「>」は使わない

出力はJSONのみです。"""


def meta_input(title: str, body: str, chapter_titles: list[str]) -> str:
    ch = "\n".join(f"{i}. {t}" for i, t in enumerate(chapter_titles, 1)) or "（なし）"
    return f"## タイトル\n{title}\n\n## 概要欄\n{body or '（なし）'}\n\n## チャプターの見出し\n{ch}"


def _has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _need_key():
    if not _has_key():
        sys.exit("[エラー] 翻訳には Claude の ANTHROPIC_API_KEY が必要です（設定されていません）。\n"
                 "  APIキーがない場合は、translate.py --llm manual で claude.ai に貼り付けて翻訳できます"
                 "（画面では「確認・仕上げ」タブの「英語字幕を作る」）。")


class Claude:
    """rc_llm の呼び出しを、翻訳用にまとめたもの（使う直前に接続する）"""

    def __init__(self, effort: str = "medium"):
        self.effort = effort
        self.usage = {"in": 0, "out": 0}
        self._c = None

    def connect(self):
        import rc_llm
        if self._c is None:
            _need_key()
            self._c = rc_llm._client()
        return self._c

    def call(self, system: str, user: str, schema: dict) -> dict:
        import rc_llm
        anthropic, client = self.connect()
        usage = {"in": 0, "out": 0}
        try:
            res = rc_llm._call(client, anthropic, system, user, schema, self.effort, usage)
        except anthropic.APIStatusError as e:
            sys.exit(f"[エラー] Claude API: {e.status_code} {e.message}")
        except anthropic.APIConnectionError as e:
            sys.exit(f"[エラー] Claude APIに接続できません: {e}")
        except RuntimeError as e:
            sys.exit(f"[エラー] {e}")
        for k in usage:
            self.usage[k] = self.usage.get(k, 0) + usage[k]
        return res


def translate_groups(groups: list[dict], cfg: dict, ctx: dict, claude: Claude, log=print,
                     workers: int = 4) -> dict[int, str]:
    """まとまりを Claude で訳す。長い動画は分けて、同時にいくつか訳す。訳し漏れは1回だけ頼み直す"""
    system = lines_instructions(cfg, ctx)
    wins = list(windows(groups))
    done = [0]

    def one(win_ref):
        win, ref = win_ref
        res = claude.call(system, group_lines(win, ref), LINES_SCHEMA)
        done[0] += 1
        log(f"  Claudeで翻訳中…（{cfg['name_ja']} {done[0]}/{len(wins)}）")
        valid = {g["id"] for g in win} - ref
        return {int(x["id"]): str(x.get("text", "")).replace("\n", " ").strip()
                for x in res.get("lines", []) if int(x.get("id", -1)) in valid}

    claude.connect()   # 同時に訳し始める前に、1回だけ接続する
    log(f"  Claudeで翻訳中…（{cfg['name_ja']} 0/{len(wins)}）")
    out: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(wins)))) as ex:
        for r in ex.map(one, wins):
            out.update(r)
    missing = [g for g in groups if g["id"] not in out]
    if missing:
        log(f"  訳し漏れが {len(missing)} か所あったので、もう一度頼みます")
        ids = {g["id"] for g in missing}
        by_id = {g["id"]: i for i, g in enumerate(groups)}
        sub, ref = [], set()
        for g in missing:   # 1つ前のまとまりを参考に付ける
            j = by_id[g["id"]] - 1
            if j >= 0 and groups[j]["id"] not in ids and groups[j]["id"] not in ref:
                sub.append(groups[j])
                ref.add(groups[j]["id"])
            sub.append(g)
        for win, _ in windows(sub, context=0):
            res = claude.call(system, group_lines(win, ref & {g["id"] for g in win}), LINES_SCHEMA)
            for x in res.get("lines", []):
                if int(x.get("id", -1)) in ids:
                    out[int(x["id"])] = str(x.get("text", "")).replace("\n", " ").strip()
    return out


# ---------------------------------------------------------------- 保存
def _sha(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p if isinstance(p, bytes) else str(p).encode("utf-8")) + b"\0")
    return h.hexdigest()[:16]


def load_json(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return default


def save_json(p: Path, data) -> None:
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def source_srt(out_dir: Path) -> Path:
    fi = load_json(out_dir / "final_info.json", {})
    if fi.get("srt") and Path(fi["srt"]).exists():
        return Path(fi["srt"])
    sess = load_json(out_dir / "session.json", {})
    p = out_dir / f"{sess.get('stem', '')}_完成.srt"
    if not p.exists():
        sys.exit(f"[エラー] 完成動画の字幕（{p.name}）がありません。先に完成動画を書き出してください"
                 "（試し書き出しでは作られません）。")
    return p


def target_srt(src: Path, code: str) -> Path:
    return src.with_name(f"{src.stem}.{code}.srt")


def context_for(out_dir: Path, title: str = "") -> dict:
    sess = load_json(out_dir / "session.json", {})
    glossary = []
    gp = sess.get("glossary")
    if gp and Path(gp).exists():
        glossary = [w.strip() for w in Path(gp).read_text(encoding="utf-8-sig").splitlines() if w.strip()]
    tags = (sess.get("meta") or {}).get("tags", [])
    return {"title": title or ((sess.get("meta") or {}).get("titles") or [""])[0],
            "mode_hint": MODE_JA.get(sess.get("mode"), ""), "glossary": glossary + list(tags)}


def srt_stamp(src: Path, cfg: dict) -> str:
    keys = ("max_line", "lines", "reading_speed", "min_duration", "max_extend", "spaced")
    return _sha(src.read_bytes(), *(cfg[k] for k in keys))


def srt_path(out_dir: Path, entry: dict) -> Path | None:
    """translations.json に書いた字幕のファイル（出力フォルダの中）"""
    return Path(out_dir) / Path(entry["srt"]).name if entry.get("srt") else None


def srt_is_fresh(out_dir: Path, cfg: dict) -> bool:
    src = source_srt(out_dir)
    info = load_json(out_dir / TRANSLATIONS, {}).get(cfg["code"], {})
    dst = target_srt(src, cfg["code"])
    return dst.exists() and info.get("srt_source") == srt_stamp(src, cfg)


def translate_srt(out_dir: Path, cfg: dict, claude: Claude | None, log=print, force: bool = False,
                  replies: dict[str, str] | None = None) -> Path | None:
    """完成動画の字幕を訳して ○○_完成.<言語>.srt を作る。

    claude が None で訳していない字幕が残るときは None を返す（claude.ai 用のプロンプトを作る場合）。
    replies は claude.ai の返答（日本語のまとまりの文 → 訳）。
    """
    out_dir = Path(out_dir)
    src = source_srt(out_dir)
    dst = target_srt(src, cfg["code"])
    stamp = srt_stamp(src, cfg)
    tr = load_json(out_dir / TRANSLATIONS, {})
    if not force and dst.exists() and tr.get(cfg["code"], {}).get("srt_source") == stamp:
        log(f"  {cfg['name_ja']}の字幕は最新です: {dst.name}")
        return dst
    caps = read_srt(src)
    groups = group_captions(caps)
    cache_all = load_json(out_dir / CACHE, {})
    cache = cache_all.setdefault(cfg["code"], {})
    texts: dict[int, str] = {}
    todo = []
    for g in groups:
        key = _sha(g["text"])
        if replies and g["text"] in replies:
            cache[key] = replies[g["text"]]
        if not force and key in cache:
            texts[g["id"]] = cache[key]
        else:
            todo.append(g)
    if todo:
        if claude is None:
            return None
        if len(todo) < len(groups):
            log(f"  前に訳した字幕を使います（新しく訳すのは {len(todo)}/{len(groups)} か所）")
        got = translate_groups(todo, cfg, context_for(out_dir), claude, log)
        for g in todo:
            if g["id"] in got:
                texts[g["id"]] = got[g["id"]]
                cache[_sha(g["text"])] = got[g["id"]]
        save_json(out_dir / CACHE, cache_all)
        lost = [g for g in todo if g["id"] not in got]
        if lost:
            log(f"  [注意] {len(lost)} か所は訳せなかったので、その字幕は出しません"
                f"（例: {lost[0]['text'][:20]}）。もう一度実行すると、そこだけ訳し直します。")
    elif replies:
        save_json(out_dir / CACHE, cache_all)
    duration = load_json(out_dir / "final_info.json", {}).get("duration")
    cues = layout(groups, texts, cfg, duration)
    write_srt(dst, cues)
    if not any(g["id"] not in texts for g in groups):   # 全部訳せたときだけ「最新」の印を付ける
        entry = tr.setdefault(cfg["code"], {})
        entry.update(srt=dst.name, srt_source=stamp, name=cfg["track_name"])   # フォルダを移しても使えるように名前だけ
    tr.setdefault("_settings", {})["languages"] = sorted({*tr.get("_settings", {}).get("languages", []),
                                                         cfg["code"]})
    tr["_settings"].setdefault("config", {})[cfg["code"]] = cfg
    save_json(out_dir / TRANSLATIONS, tr)
    log(f"  {cfg['name_ja']}の字幕を作りました: {dst.name}（{len(cues)}枚）")
    return dst


# ---------------------------------------------------------------- タイトル・概要欄
_CH_LINE = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2})\s+(.+?)\s*$")


def split_description(desc: str) -> tuple[list[str], list[tuple[str, str]], int | None]:
    """概要欄を「チャプター以外の段落」「チャプター（時刻, 見出し）」「チャプターの位置」に分ける"""
    parts = desc.split("\n\n")
    idx = next((i for i, p in enumerate(parts) if p.lstrip().startswith(CHAPTER_HEAD_JA)), None)
    chapters: list[tuple[str, str]] = []
    if idx is not None:
        for ln in parts[idx].splitlines()[1:]:
            m = _CH_LINE.match(ln)
            if m:
                chapters.append((m.group(1), m.group(2)))
        body = parts[:idx] + parts[idx + 1:]
    else:
        body = parts
    return body, chapters, idx


def build_localized(res: dict, chapters: list[tuple[str, str]], idx: int | None) -> tuple[str, str]:
    title = re.sub(r"[<>]", "", str(res.get("title", ""))).strip()[:100]
    body = re.sub(r"[<>]", "", str(res.get("description", ""))).strip()
    parts = [p for p in body.split("\n\n") if p.strip()]
    names = [str(x).strip() for x in res.get("chapters", [])]
    if chapters:
        if len(names) != len(chapters) or not all(names):
            names = [t for _, t in chapters]   # 数が合わないときは、日本語の見出しのまま
        head = re.sub(r"[<>]", "", str(res.get("chapter_heading") or "Chapters")).strip()
        block = f"▼ {head}\n" + "\n".join(f"{t} {re.sub(r'[<>]', '', n)}" for (t, _), n in zip(chapters, names))
        if idx is None:
            idx = len(parts)
        # 日本語と同じ位置（ハッシュタグがあればその前）に入れる
        tag = next((i for i, p in enumerate(parts) if p.strip().startswith("#")), len(parts))
        parts.insert(min(idx, tag), block)
    desc = "\n\n".join(parts)
    while len(desc.encode("utf-8")) > 5000:
        desc = desc[: len(desc) - 50]   # 上限（5000バイト）を超えるときは、最後を少しずつ削る
    return title, desc


def meta_stamp(title: str, description: str) -> str:
    return _sha(title, description)


def meta_is_fresh(out_dir: Path, code: str, title: str, description: str) -> bool:
    e = load_json(Path(out_dir) / TRANSLATIONS, {}).get(code, {})
    return bool(e.get("title")) and e.get("meta_source") == meta_stamp(title, description)


def translate_meta(out_dir: Path, cfg: dict, title: str, description: str, claude: Claude | None,
                   log=print, force: bool = False, reply: dict | None = None) -> dict | None:
    """YouTube のタイトル・概要欄を訳して translations.json に保存する（claude が None で未訳なら None）"""
    out_dir = Path(out_dir)
    code = cfg["code"]
    if not force and reply is None and meta_is_fresh(out_dir, code, title, description):
        log(f"  {cfg['name_ja']}のタイトル・概要欄は最新です")
        return load_json(out_dir / TRANSLATIONS, {})[code]
    body, chapters, idx = split_description(description)
    if reply is None:
        if claude is None:
            return None
        log(f"  Claudeで翻訳中…（{cfg['name_ja']}のタイトル・概要欄）")
        reply = claude.call(meta_instructions(cfg, context_for(out_dir, title)),
                            meta_input(title, "\n\n".join(body), [t for _, t in chapters]), META_SCHEMA)
    t, d = build_localized(reply, chapters, idx)
    if not t:
        sys.exit(f"[エラー] {cfg['name_ja']}のタイトルが空でした。もう一度実行してください。")
    tr = load_json(out_dir / TRANSLATIONS, {})
    entry = tr.setdefault(code, {})
    entry.update(title=t, description=d, meta_source=meta_stamp(title, description), name=cfg["track_name"])
    tr.setdefault("_settings", {})["languages"] = sorted({*tr.get("_settings", {}).get("languages", []), code})
    tr["_settings"].setdefault("config", {})[code] = cfg
    save_json(out_dir / TRANSLATIONS, tr)
    log(f"  {cfg['name_ja']}のタイトル: {t}")
    return entry


# ---------------------------------------------------------------- claude.ai に貼り付けて使う場合
MANUAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["meta", "lines"],
    "properties": {"meta": META_SCHEMA, "lines": LINES_SCHEMA["properties"]["lines"]},
}


def manual_paths(out_dir: Path, code: str) -> tuple[Path, Path]:
    return Path(out_dir) / f"prompt_translate_{code}.md", Path(out_dir) / f"claude_translate_{code}.json"


def write_manual_prompt(out_dir: Path, cfg: dict, title: str, description: str,
                        groups: list[dict] | None) -> Path:
    """claude.ai に貼り付けるプロンプトを作る（タイトル・概要欄と字幕をまとめて1回で訳してもらう）"""
    body, chapters, _ = split_description(description)
    ctx = context_for(out_dir, title)
    schema = json.dumps(MANUAL_SCHEMA, ensure_ascii=False, indent=1)
    text = ("2つの作業をして、結果を1つのJSONで返してください。\n\n# 作業1（meta）\n" + meta_instructions(cfg, ctx)
            + "\n\n" + meta_input(title, "\n\n".join(body), [t for _, t in chapters])
            + "\n\n# 作業2（lines）\n" + lines_instructions(cfg, ctx)
            + "\n\n## 字幕\n" + (group_lines(groups) if groups else "（なし。lines は空の配列にしてください）")
            + f"\n\n# 出力するJSONの形式（JSON Schema）\n```json\n{schema}\n```\n")
    p, _ = manual_paths(out_dir, cfg["code"])
    p.write_text(text, encoding="utf-8")
    meta = {"groups": [g["text"] for g in (groups or [])], "ids": [g["id"] for g in (groups or [])],
            "meta_source": meta_stamp(title, description)}
    save_json(p.with_suffix(".json"), meta)   # プロンプトを作ったときの ID と字幕の対応
    return p


def read_manual_reply(out_dir: Path, cfg: dict) -> dict | None:
    """claude.ai の返答（claude_translate_<言語>.json）を読む。ないときは None

    返り値: {"meta": タイトル等の訳, "meta_source": プロンプトを作ったときの目印, "lines": {日本語の文: 訳}}
    """
    prompt, reply = manual_paths(out_dir, cfg["code"])
    if not reply.exists():
        return None
    s = reply.read_text(encoding="utf-8-sig")
    a, b = s.find("{"), s.rfind("}")
    try:
        data = json.loads(s[a:b + 1])
    except Exception:  # noqa: BLE001
        sys.exit(f"[エラー] {reply.name} のJSONが読み取れません。Claudeの返答を最初から最後まで全部貼り付けてください。")
    pm = load_json(prompt.with_suffix(".json"), {})
    # 返答の ID は、プロンプトを作ったときの字幕の文で、今の字幕に当てはめる（カットを変えても使える）
    by_old = dict(zip(pm.get("ids", []), pm.get("groups", [])))
    lines = {}
    for x in data.get("lines", []):
        ja = by_old.get(int(x.get("id", -1)))
        if ja is not None:
            lines[ja] = str(x.get("text", "")).replace("\n", " ").strip()
    return {"meta": data.get("meta"), "meta_source": pm.get("meta_source"), "lines": lines}


# ---------------------------------------------------------------- YouTube
def _out_dir_of(meta: dict) -> Path:
    for k in ("captions", "video"):
        if meta.get(k):
            return Path(meta[k]).resolve().parent
    raise SystemExit("[エラー] 完成動画のフォルダが分かりません。")


def saved_languages(out_dir: Path) -> list[dict]:
    tr = load_json(Path(out_dir) / TRANSLATIONS, {})
    st = tr.get("_settings", {})
    codes = st.get("languages") or ["en"]
    conf = st.get("config", {})
    return [conf.get(c) or lang_config(c) for c in codes]


def prepare_for_youtube(meta: dict, log=print) -> dict:
    """YouTube に設定する前に、訳が今のタイトル・概要欄・字幕に合っているか確かめる（古ければ訳し直す）"""
    out_dir = _out_dir_of(meta)
    claude = Claude()
    for cfg in saved_languages(out_dir):
        if not meta_is_fresh(out_dir, cfg["code"], meta["title"], meta["description"]):
            if not _has_key():
                sys.exit(f"[エラー] {cfg['name_ja']}のタイトル・概要欄が、今の日本語のタイトル・概要欄に合っていません"
                         "（翻訳した後に変わっています）。\n  先に translate.py で翻訳し直してください"
                         "（ANTHROPIC_API_KEY が必要です。ない場合は --llm manual）。")
            translate_meta(out_dir, cfg, meta["title"], meta["description"], claude, log)
        if meta.get("captions") and Path(meta["captions"]).exists() and not srt_is_fresh(out_dir, cfg):
            if translate_srt(out_dir, cfg, claude if _has_key() else None, log) is None:
                sys.exit(f"[エラー] {cfg['name_ja']}の字幕が、今の完成動画の字幕に合っていません。"
                         "先に translate.py で翻訳し直してください（ANTHROPIC_API_KEY が必要です）。")
    if claude.usage["in"]:   # 訳し直したときは料金も出す（まとめて処理の合計にも入る）
        import rc_llm
        log(f"  Claude API: {rc_llm.cost_text(claude.usage)}")
    return load_json(out_dir / TRANSLATIONS, {})


def add_localizations(yt, video_id: str, meta: dict, body: dict, log=print) -> str:
    """videos().update の body に、翻訳したタイトル・概要欄（localizations）を足す。update の part を返す

    Studio で自分で入れたほかの言語の翻訳は消さずに残す。
    """
    tr = prepare_for_youtube(meta, log)
    loc = {}
    try:
        r = yt.videos().list(part="localizations", id=video_id).execute()
        items = r.get("items", [])
        loc = dict((items[0].get("localizations") or {}) if items else {})
    except Exception as e:  # noqa: BLE001
        log(f"  [注意] 今の翻訳の設定を読めませんでした（上書きします）: {e}")
    names = []
    for code, e in tr.items():
        if code.startswith("_") or not e.get("title"):
            continue
        loc[code] = {"title": e["title"], "description": e.get("description", "")}
        names.append(LANGS.get(code, (code,))[0])
    if "ja" in loc:   # 日本語の翻訳が登録されていたら、本体と同じ内容にそろえる
        loc["ja"] = {"title": body["snippet"]["title"], "description": body["snippet"]["description"]}
    if not names:
        return "snippet"
    log(f"  {'・'.join(names)}のタイトル・概要欄も設定します")
    body["localizations"] = loc
    return "snippet,localizations"


def upload_captions(yt, video_id: str, meta: dict, log=print) -> None:
    """翻訳した字幕（○○_完成.<言語>.srt）を字幕トラックとして追加する。前に追加したものは差し替える"""
    from googleapiclient.http import MediaFileUpload
    out_dir = _out_dir_of(meta)
    tr = load_json(out_dir / TRANSLATIONS, {})
    todo = [(c, e) for c, e in tr.items() if not c.startswith("_") and e.get("srt") and srt_path(out_dir, e).exists()]
    if not todo:
        return
    try:
        existing = yt.captions().list(part="snippet", videoId=video_id).execute().get("items", [])
    except Exception as e:  # noqa: BLE001
        log(f"  [注意] 今の字幕の一覧を読めませんでした: {e}")
        existing = []
    for code, e in todo:
        name = e.get("name") or code
        same = next((c for c in existing if c["snippet"].get("language") == code
                     and c["snippet"].get("name") == name and c["snippet"].get("trackKind") != "asr"), None)
        media = MediaFileUpload(str(srt_path(out_dir, e)), mimetype="application/octet-stream")
        label = LANGS.get(code, (code,))[0]
        if same:
            log(f"  {label}の字幕を差し替えています…")
            yt.captions().update(part="snippet", body={"id": same["id"], "snippet": {"isDraft": False}},
                                 media_body=media).execute()
        else:
            log(f"  {label}の字幕を追加しています…")
            yt.captions().insert(part="snippet", body={
                "snippet": {"videoId": video_id, "language": code, "name": name, "isDraft": False}},
                media_body=media).execute()
