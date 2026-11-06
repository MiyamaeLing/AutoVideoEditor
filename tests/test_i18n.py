"""英語字幕と多言語対応（translate.py / rc_i18n.py）のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_i18n.py            # 単独で実行（先に tests/smoke_test.py で完成動画を作っておく）
    python tests/smoke_test.py           # 通しテストの最後からも呼ばれます

Claude の返答は偽の通信（httpx2.MockTransport で SSE を返す）、YouTube は HttpMockSequence で確認する。

確認すること:
  - 英語の字幕の区切り直し（1行の文字数・行数・読める速さ・次の字幕にかぶらない）
  - translate.py で ○○_完成.en.srt と英語のタイトル・概要欄ができる（チャプターの時刻はそのまま）
  - 2回目は Claude を呼ばない。タイトルを直したらタイトルだけ、字幕が変わったら変わった所だけ訳し直す
  - 3時間の字幕でも、分けて訳して全部そろう。訳し漏れは頼み直す
  - claude.ai に貼り付けて使う（--llm manual）
  - YouTube に英語字幕トラックと localizations を設定する（前の英語字幕は差し替え、ほかの言語の翻訳は残す）
  - 画面：「英語字幕を作る」ボタン、YouTube画面のチェック → 翻訳してから設定する
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable

EN = {
    "どうもこんにちは": "Hi, everyone!",
    "今日はこのドリルをレビューします": "Today I'm reviewing this drill.",
    "まずはスペックですが、重さが1.2キロです": "First, the specs: it weighs just 1.2 kilograms, which is "
                                       "surprisingly light for a cordless drill like this one.",
    "実際に穴を開けてみます": "Let's actually drill a hole.",
    "はい、きれいに開きました　すごい": "And it went right through, nice and clean. Wow!",
    "というわけでマキタのドリルでした": "So that was the Makita drill.",
    "チャンネル登録お願いします": "Please subscribe to the channel!",
}
EN_CH = {"オープニング": "Opening", "スペック": "Specs", "実際に使ってみた": "Trying It Out"}


# ---------------------------------------------------------------- 偽の Claude
class FakeClaude:
    """rc_llm._client を差し替えて、SSE（ストリーミング）の返答を返す偽の Claude"""

    def __init__(self, drop_once: set[int] | None = None):
        self.requests: list[dict] = []
        self.drop_once = set(drop_once or ())

    @staticmethod
    def lines_reply(user: str, drop: set[int] = frozenset()) -> dict:
        out = []
        for ln in user.splitlines():
            m = re.match(r"^#(\d+) \[[^\]]*\] \([\d.]+秒\) (.*)$", ln)
            if not m or int(m.group(1)) in drop:
                continue
            ja = m.group(2)
            out.append({"id": int(m.group(1)),
                        "text": EN.get(ja, f"This is line {m.group(1)}, translated into English for the test.")})
        return {"lines": out}

    @staticmethod
    def meta_reply(user: str) -> dict:
        ch = re.findall(r"^\d+\. (.+)$", user.split("## チャプターの見出し", 1)[-1], re.M)
        return {"title": "Honest Review: The Super Light Makita Drill <test>",
                "description": "I reviewed the Makita drill.\nIt is really light.",
                "chapter_heading": "Chapters", "chapters": [EN_CH.get(c, c + " (en)") for c in ch]}

    def handler(self, request):
        import httpx2
        body = json.loads(request.content)
        self.requests.append(body)
        user = body["messages"][0]["content"]
        props = body["output_config"]["format"]["schema"]["properties"]
        if "lines" in props:
            drop, self.drop_once = self.drop_once, set()
            res = self.lines_reply(user, drop)
        else:
            res = self.meta_reply(user)
        text = json.dumps(res, ensure_ascii=False)
        ev = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text[:len(text) // 2]}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text[len(text) // 2:]}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 500}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        sse = "".join(f"event: {k}\ndata: {json.dumps(v, ensure_ascii=False)}\n\n" for k, v in ev)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode("utf-8"))

    def __enter__(self):
        import anthropic
        import httpx2
        import rc_llm
        self._orig = rc_llm._client
        self._key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                     http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)))
        rc_llm._client = lambda: (anthropic, client)
        return self

    def __exit__(self, *a):
        import rc_llm
        rc_llm._client = self._orig
        if self._key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._key


def _quiet(*_a, **_k):
    pass


def _work(out: Path, fx: Path, name: str) -> Path:
    d = fx / "i18n" / name
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(out, d, ignore=shutil.ignore_patterns("work", "*.mp4", "*.png"))
    # 完成動画の場所（final_info.json）も、コピー先を指すようにする
    fi = json.loads((d / "final_info.json").read_text(encoding="utf-8"))
    fi["video"] = str(d / Path(fi["video"]).name)
    fi["srt"] = str(d / Path(fi["srt"]).name)
    fi["duration"] = 40.0   # YouTube のチャプターの条件（最後の章が10秒以上）を満たすように少し長くする
    (d / "final_info.json").write_text(json.dumps(fi, ensure_ascii=False), encoding="utf-8")
    for f in ("translations.json", "translate_cache.json", "youtube_meta.json"):
        (d / f).unlink(missing_ok=True)
    return d


def _cue_problems(cues, cfg) -> list[str]:
    bad = []
    for i, (s, e, t) in enumerate(cues):
        lines = t.split("\n")
        if len(lines) > cfg["lines"] or any(len(x) > cfg["max_line"] for x in lines):
            bad.append(f"長すぎる: {t!r}")
        if e <= s:
            bad.append(f"長さ0: {s}")
        if i + 1 < len(cues) and e > cues[i + 1][0] + 1e-6:
            bad.append(f"かぶる: {e} > {cues[i + 1][0]}")
    return bad


# ---------------------------------------------------------------- 小さな部品
def t_units(check):
    import rc_i18n as I
    en = I.lang_config("en")
    long = ("First, the specs: it weighs just 1.2 kilograms, which is surprisingly light for a cordless drill "
            "like this one, and the battery lasts for about two hours of continuous use.")
    cues = I.fit_cues(long, 5.0, 9.0, 10.5, en)
    check("字幕の区切り: 長い英語は何枚かに分けて、1行42文字・2行まで", len(cues) >= 2 and not _cue_problems(cues, en),
          str(cues))
    check("字幕の区切り: 始まりは日本語の字幕と同じ", cues[0][0] == 5.0)
    check("字幕の区切り: 読み切れない長さは、次の字幕の直前まで延ばす", abs(cues[-1][1] - 10.5) < 1e-6, str(cues[-1]))
    short = I.fit_cues("Wow!", 3.0, 3.4, 8.0, en)
    check("字幕の区切り: 短すぎる表示は1秒まで延ばす", short == [(3.0, 4.0, "Wow!")], str(short))
    check("字幕の区切り: 文の終わりで分ける", cues[0][2].replace("\n", " ").endswith((".", ",", ":")), str(cues))
    zh = I.lang_config("zh-Hans")
    zc = I.fit_cues("首先是规格，重量只有1.2公斤，对于这种尺寸的电钻来说，比我预想的要轻得多。", 5.0, 9.0, 10.0, zh)
    check("字幕の区切り: 中国語（スペースなし）も区切れる、数字は切らない",
          not _cue_problems(zc, zh) and any("1.2" in t for _, _, t in zc), str(zc))

    caps = [(0.0, 1.0, "まずは"), (1.0, 2.5, "スペックです"), (3.0, 4.0, "次へ")]
    g = I.group_captions(caps)
    check("字幕のまとまり: 続いている字幕を1つにする", [x["text"] for x in g] == ["まずは　スペックです", "次へ"],
          str([x["text"] for x in g]))
    cfg_t = {"translate": {"languages": ["en", "ko", "ja"], "ko": {"max_line": 20}}}
    langs = I.languages(cfg_t)
    check("言語の設定: テンプレートで言語を足せる（日本語は除く）",
          [c["code"] for c in langs] == ["en", "ko"] and langs[1]["max_line"] == 20)
    body, ch, idx = I.split_description("説明\n\n▼ チャプター\n00:00 オープニング\n1:02:03 まとめ\n\n#マキタ")
    t, d = I.build_localized({"title": "T", "description": "Desc\n\n#Makita", "chapter_heading": "Chapters",
                              "chapters": ["Opening", "Summary"]}, ch, idx)
    check("概要欄: チャプターの時刻はそのまま、ハッシュタグの前に入れる",
          d == "Desc\n\n▼ Chapters\n00:00 Opening\n1:02:03 Summary\n\n#Makita", d.replace("\n", " / "))


# ---------------------------------------------------------------- translate.py（API）
def t_translate(check, out: Path, fx: Path) -> Path:
    import rc_i18n as I
    import rc_youtube as Y
    import translate
    d = _work(out, fx, "api")
    with FakeClaude() as fc:
        translate.run(d, ROOT / "templates" / "default", log=_quiet)
    en = d / "C0001_完成.en.srt"
    cues = I.read_srt(en, join="\n") if en.exists() else []
    ja = I.read_srt(d / "C0001_完成.srt")
    check("翻訳: 英語の字幕ができた", en.exists() and len(cues) >= len(I.group_captions(ja)), f"{len(cues)}枚")
    check("翻訳: 英語の字幕の形（行の長さ・かぶらない）", bool(cues) and not _cue_problems(cues, I.lang_config("en")),
          str(_cue_problems(cues, I.lang_config("en")))[:300])
    check("翻訳: 最初の字幕の時刻は日本語と同じ", bool(cues) and abs(cues[0][0] - ja[0][0]) < 0.002)
    dur = json.loads((d / "final_info.json").read_text(encoding="utf-8"))["duration"]
    check("翻訳: 動画の長さを超えない", bool(cues) and cues[-1][1] <= dur + 1e-6, f"{cues[-1][1] if cues else 0} / {dur}")
    req = fc.requests[0] if fc.requests else {}
    check("翻訳: Claude の呼び出し方（モデル・考える・構造化出力）",
          req.get("model") == "claude-opus-5-5" and req.get("thinking") == {"type": "adaptive"}
          and req.get("output_config", {}).get("format", {}).get("type") == "json_schema", str(req.get("model")))
    tr = json.loads((d / "translations.json").read_text(encoding="utf-8"))
    e = tr.get("en", {})
    check("翻訳: 英語のタイトル（< > は取り除く）", e.get("title") == "Honest Review: The Super Light Makita Drill test",
          str(e.get("title")))
    desc = e.get("description", "")
    ja_times = re.findall(r"^(\d+:\d\d) ", Y.default_meta(d)["description"], re.M)
    want = "▼ Chapters\n" + "\n".join(f"{t} {n}" for t, n in zip(ja_times, EN_CH.values()))
    check("翻訳: 英語の概要欄にチャプター（時刻は日本語のまま）", len(ja_times) == 3 and want in desc,
          desc.replace("\n", " / "))
    check("翻訳: 概要欄の上限（5000バイト）", len(desc.encode("utf-8")) <= 5000)

    # 2回目は Claude を呼ばない
    with FakeClaude() as fc2:
        translate.run(d, ROOT / "templates" / "default", log=_quiet)
    check("翻訳: 2回目は Claude を呼ばない（最新）", len(fc2.requests) == 0, str(len(fc2.requests)))

    # タイトルを直したら、タイトル・概要欄だけ訳し直す
    m = Y.default_meta(d)
    m["title"] = "自分で直したタイトル"
    Y.save_meta(d, m)
    with FakeClaude() as fc3:
        translate.run(d, ROOT / "templates" / "default", log=_quiet)
    kinds = ["lines" if "lines" in r["output_config"]["format"]["schema"]["properties"] else "meta"
             for r in fc3.requests]
    check("翻訳: タイトルを直したら、タイトル・概要欄だけ訳し直す", kinds == ["meta"], str(kinds))

    # 書き出し直して字幕が少し変わった（時刻がずれて、1か所だけ文が変わった）
    src = d / "C0001_完成.srt"
    caps = [(s + 0.5, e + 0.5, ("新しく足した発言です" if i == 1 else t)) for i, (s, e, t) in enumerate(I.read_srt(src))]
    I.write_srt(src, caps)
    with FakeClaude() as fc4:
        translate.run(d, ROOT / "templates" / "default", log=_quiet)
    sent = [ln for r in fc4.requests for ln in r["messages"][0]["content"].splitlines()
            if ln.startswith("#")]
    cues2 = I.read_srt(en, join="\n")
    check("翻訳: 字幕が変わったら、変わった所だけ訳し直す", len(sent) == 1 and "新しく足した発言" in sent[0], str(sent))
    check("翻訳: 新しい時刻に合わせる", bool(cues2) and abs(cues2[0][0] - caps[0][0]) < 0.002)
    return d


def t_long(check, fx: Path):
    """3時間の字幕：分けて同時に訳し、全部そろう。訳し漏れは頼み直す"""
    import rc_i18n as I
    d = fx / "i18n" / "long"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    caps, t, i = [], 1.0, 0
    while t < 3 * 3600:
        i += 1
        caps.append((t, t + 2.4, f"これはとても長い企画動画の{i}番目の発言で、内容を説明しています"))
        t += 3.0
    src = d / "LONG_完成.srt"
    I.write_srt(src, caps)
    (d / "session.json").write_text(json.dumps({"stem": "LONG", "mode": "outdoor", "meta": {}}), encoding="utf-8")
    (d / "final_info.json").write_text(json.dumps({"srt": str(src), "duration": 3 * 3600 + 5}), encoding="utf-8")
    t0 = time.time()
    with FakeClaude(drop_once={5}) as fc:
        I.translate_srt(d, I.lang_config("en"), I.Claude(), _quiet)
    cues = I.read_srt(d / "LONG_完成.en.srt", join="\n")
    n_req = len(fc.requests)
    check("長い素材: 3時間の字幕を分けて訳す", n_req >= 10 and len(cues) >= len(caps),
          f"{len(caps)}枚 → {len(cues)}枚 / 呼び出し{n_req}回 / {time.time() - t0:.1f}秒")
    check("長い素材: 訳し漏れは頼み直す", any(abs(s - caps[4][0]) < 0.002 for s, _, _ in cues))
    check("長い素材: 字幕の形", not _cue_problems(cues, I.lang_config("en")))
    tr = json.loads((d / "translations.json").read_text(encoding="utf-8"))
    check("長い素材: 全部訳せたので「最新」の印", bool(tr.get("en", {}).get("srt_source")))


# ---------------------------------------------------------------- claude.ai に貼り付けて使う
def t_manual(check, out: Path, fx: Path):
    import rc_i18n as I
    import translate
    d = _work(out, fx, "manual")
    pend = translate.run(d, ROOT / "templates" / "default", llm="manual", log=_quiet)
    prompt = Path(pend[0]["prompt"]) if pend else None
    check("claude.ai: 貼り付け用のプロンプトを作る", bool(prompt and prompt.exists())
          and (d / "translate_pending.json").exists())
    text = prompt.read_text(encoding="utf-8") if prompt else ""
    # claude.ai の返答の代わり（```json 付き）
    ans = {"meta": FakeClaude.meta_reply(text.split("# 作業2")[0]), "lines": FakeClaude.lines_reply(text)["lines"]}
    Path(pend[0]["reply"]).write_text("```json\n" + json.dumps(ans, ensure_ascii=False) + "\n```", encoding="utf-8")
    pend2 = translate.run(d, ROOT / "templates" / "default", llm="manual", log=_quiet)
    en = d / "C0001_完成.en.srt"
    tr = json.loads((d / "translations.json").read_text(encoding="utf-8")) if (d / "translations.json").exists() else {}
    check("claude.ai: 返答を読み込んで字幕とタイトルができる",
          not pend2 and en.exists() and tr.get("en", {}).get("title", "").startswith("Honest Review")
          and not (d / "translate_pending.json").exists(), str(pend2))
    ok = I.read_srt(en)[0][2] == "Hi, everyone!" if en.exists() else False
    check("claude.ai: 返答のIDを正しい字幕に当てる", ok)


# ---------------------------------------------------------------- YouTube
class RecordingHttp:
    """HttpMockSequence に、送った内容を記録する機能を足したもの"""

    def __init__(self, responses):
        from googleapiclient.http import HttpMockSequence
        self.seq = HttpMockSequence(responses)
        self.calls: list[tuple[str, str, bytes]] = []

    def request(self, uri, method="GET", body=None, headers=None, **kw):
        b = body if isinstance(body, bytes) else (body or "").encode("utf-8") if isinstance(body, str) else b""
        self.calls.append((method, uri, b))
        return self.seq.request(uri, method, body, headers, **kw)

    def __getattr__(self, k):
        return getattr(self.seq, k)


def _yt(responses):
    from googleapiclient.discovery import build
    http = RecordingHttp(responses)
    return build("youtube", "v3", http=http, static_discovery=True, developerKey="x"), http


def _body_json(b: bytes) -> dict:
    s = b.decode("utf-8", "replace")
    a = s.find("{")
    try:
        return json.JSONDecoder().raw_decode(s[a:])[0] if a >= 0 else {}
    except Exception:  # noqa: BLE001
        return {}


def t_youtube(check, d: Path):
    import rc_youtube as Y
    import youtube
    ok = {"status": "200"}
    m = Y.default_meta(d)
    m["thumbnail"] = ""
    m["localize"] = True
    Y.save_meta(d, m)
    existing_loc = json.dumps({"items": [{"id": "VID", "localizations": {"fr": {"title": "Titre", "description": "x"}}}]})
    yt, http = _yt([(ok, existing_loc), (ok, json.dumps({"id": "VID"})), (ok, json.dumps({"items": []})),
                    (ok, json.dumps({"id": "CAP1"}))])
    try:
        Y.apply_meta(yt, "VID", Y.default_meta(d), log=_quiet)
        err = ""
    except (Exception, SystemExit) as e:  # noqa: BLE001
        err = str(e)
    upd = next(((u, b) for meth, u, b in http.calls if meth == "PUT" and "/videos" in u), None)
    body = _body_json(upd[1]) if upd else {}
    loc = body.get("localizations", {})
    check("YouTube: 英語のタイトル・概要欄（localizations）を設定", not err and upd is not None
          and "localizations" in upd[0] and loc.get("en", {}).get("title") == "Honest Review: The Super Light Makita Drill test"
          and "▼ Chapters" in loc.get("en", {}).get("description", ""), err or (upd[0] if upd else "更新なし"))
    check("YouTube: Studio で入れたほかの言語の翻訳は残す", loc.get("fr", {}).get("title") == "Titre", str(list(loc)))
    check("YouTube: 本体の言語は日本語のまま", body.get("snippet", {}).get("defaultLanguage") == "ja")
    ins = [(u, b) for meth, u, b in http.calls if meth == "POST" and "captions" in u]
    cap_meta = _body_json(ins[0][1]) if ins else {}
    check("YouTube: 英語の字幕トラックを追加", len(ins) == 1 and cap_meta.get("snippet", {}).get("language") == "en"
          and cap_meta["snippet"].get("name") == "English" and b"Hi, everyone!" in ins[0][1],
          str([u for u, _ in ins]))

    # 2回目：前に追加した英語字幕は差し替える（同じ字幕が2つにならない）
    caps = json.dumps({"items": [{"id": "ASR1", "snippet": {"language": "en", "name": "", "trackKind": "asr"}},
                                 {"id": "CAP1", "snippet": {"language": "en", "name": "English", "trackKind": "standard"}}]})
    yt, http = _yt([(ok, existing_loc), (ok, json.dumps({"id": "VID"})), (ok, caps), (ok, json.dumps({"id": "CAP1"}))])
    Y.apply_meta(yt, "VID", Y.default_meta(d), log=_quiet)
    put = [u for meth, u, b in http.calls if meth == "PUT" and "captions" in u]
    post = [u for meth, u, b in http.calls if meth == "POST" and "captions" in u]
    check("YouTube: 前に追加した英語字幕は差し替える", len(put) == 1 and not post, str(put + post))

    # 翻訳した後に日本語のタイトルを直した＋APIキーなし → 古い訳は使わずに止める（何も変えない）
    m = Y.default_meta(d)
    m["title"] = "さらに直したタイトル"
    Y.save_meta(d, m)
    key = os.environ.pop("ANTHROPIC_API_KEY", None)
    yt, http = _yt([(ok, existing_loc)])
    try:
        Y.apply_meta(yt, "VID", Y.default_meta(d), log=_quiet)
        msg = ""
    except SystemExit as e:
        msg = str(e)
    finally:
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    check("YouTube: 訳が古いときは設定する前に止める", "translate.py" in msg and not http.calls, msg[:80])

    # APIキーがあれば、その場で訳し直してから設定する（youtube.py --localize）
    m["localize"] = False
    Y.save_meta(d, m)
    ch = json.dumps({"items": [{"id": "UC1", "snippet": {"title": "テストch"},
                                "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    yt, http = _yt([(ok, ch), (ok, existing_loc), (ok, json.dumps({"id": "VID"})), (ok, caps),
                    (ok, json.dumps({"id": "CAP1"}))])
    orig, argv = Y.get_service, sys.argv
    Y.get_service = lambda interactive=True: yt
    sys.argv = ["youtube.py", str(d), "--apply", "--video-id", "VID", "--localize"]
    try:
        with FakeClaude() as fc:
            youtube.main()
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        Y.get_service, sys.argv = orig, argv
    upd = next((b for meth, u, b in http.calls if meth == "PUT" and "/videos" in u), b"")
    check("YouTube: youtube.py --localize（古い訳はその場で訳し直す）",
          code == 0 and len(fc.requests) == 1 and b"localizations" in upd, f"{code} / 呼び出し{len(fc.requests)}回")


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import os, sys, json, time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog
import gui, rc_youtube as Y
app = QApplication([])
res = {}
shown = []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w = gui.MainWindow(); w.resize(1300, 860); w.show(); w.create.llm.setCurrentIndex(w.create.llm.findData("api", 257))  # 「Claude APIで自動」
out = Path(OUT)
rv = w.review
rv.load(out)
res["ext_loaded"] = "gui_ext_i18n" in w.extensions
b = getattr(w, "i18n_button", None)
res["button"] = bool(b) and b.isEnabled() and b.text() in ("英語字幕を作る", "英語字幕を作り直す")
w.runner.busy_changed.emit(True)
res["button_busy"] = bool(b) and not b.isEnabled()
w.runner.busy_changed.emit(False)
w.tabs.setCurrentWidget(rv)
for _ in range(5):
    app.processEvents()
w.grab().save(str(out / "gui_i18n_review.png"))

calls = []
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    if on_done:
        QTimer.singleShot(0, lambda: on_done(True))
    return True
w.runner.start = fake_start

# 「英語字幕を作る」：APIキーがなければ claude.ai に貼り付ける方法をすすめる → 結果の確認画面
import gui_ext_i18n
views = []
gui_ext_i18n.TranslationView.exec = lambda self: views.append(self) or 0
w.settings.setValue("api_key", "")
os.environ.pop("ANTHROPIC_API_KEY", None)
b.click()
for _ in range(20):
    app.processEvents(); time.sleep(0.02)
res["ext_args"] = calls[-1][1] if calls else []
res["view"] = bool(views)
if views:
    v = views[0]; v.show()
    for _ in range(5):
        app.processEvents()
    v.grab().save(str(out / "gui_i18n_view.png"))
    v.close()

# YouTube画面：チェックを入れて「② アップした動画に設定を反映」→ 翻訳してから設定
from rc_youtube import token_path
token_path().write_text("{}", encoding="utf-8")
calls.clear()
dlg = gui.YouTubeDialog(rv)
res["loc_default_off"] = not dlg.loc.isChecked()
dlg.loc.setChecked(True)
dlg.resize(900, 820); dlg.show()
for _ in range(5):
    app.processEvents()
dlg.grab().save(str(out / "gui_i18n_youtube.png"))
dlg._apply()
for _ in range(20):
    app.processEvents(); time.sleep(0.02)
res["chain"] = [(s, a) for s, a in calls]
res["saved_localize"] = bool(Y.default_meta(out).get("localize"))
dlg.reject()
dlg2 = gui.YouTubeDialog(rv)
res["reopen_checked"] = dlg2.loc.isChecked()
dlg2.loc.setChecked(False)
calls.clear()
dlg2._apply()
for _ in range(20):
    app.processEvents(); time.sleep(0.02)
res["no_loc_chain"] = [s for s, a in calls]
dlg2.reject()
token_path().unlink()
res["shown"] = shown
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, d: Path, fx: Path):
    appdata = fx / "i18n" / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(d)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(appdata), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(appdata), AVE_SETTINGS_DIR=str(appdata))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("翻訳の画面: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("翻訳の画面: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("翻訳の画面: 拡張（gui_ext_i18n）を読み込む", r["ext_loaded"])
    check("翻訳の画面: 「英語字幕を作る」ボタン（処理中は押せない）", r["button"] and r["button_busy"])
    check("翻訳の画面: APIキーがないときは claude.ai で翻訳", "--llm" in r["ext_args"]
          and r["ext_args"][r["ext_args"].index("--llm") + 1] == "manual", str(r["ext_args"]))
    check("翻訳の画面: 終わったら確認画面を出す", r["view"])
    check("翻訳の画面: YouTube画面のチェックは最初は外れている", r["loc_default_off"])
    chain = r["chain"]
    check("翻訳の画面: チェックを入れると翻訳してから YouTube に設定する",
          [s for s, _ in chain] == ["translate.py", "youtube.py"] and "--localize" in chain[1][1]
          and "--template" in chain[0][1], str(chain))
    check("翻訳の画面: チェックは保存されて、開き直しても残る", r["saved_localize"] and r["reopen_checked"])
    check("翻訳の画面: チェックを外せば今までどおり", r["no_loc_chain"] == ["youtube.py"], str(r["no_loc_chain"]))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    (fx / "i18n").mkdir(exist_ok=True)
    t_units(check)
    t_long(check, fx)
    if not (out / "final_info.json").exists() or not (out / "C0001_完成.srt").exists():
        check("翻訳のテスト（完成動画の字幕が必要）", False, "先に仕上げ（finish.py）が成功している必要があります")
        return
    d = t_translate(check, out, fx)
    t_manual(check, out, fx)
    t_youtube(check, d)
    g = _work(out, fx, "gui")
    with FakeClaude():
        import translate
        translate.run(g, ROOT / "templates" / "default", log=_quiet)
    shutil.copytree(out / "thumbnails", g / "thumbnails", dirs_exist_ok=True)
    t_gui(check, g, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
