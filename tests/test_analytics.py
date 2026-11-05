"""公開後の分析（analytics.py / rc_analytics.py / gui_ext_analytics.py）のテスト。

    python tests/test_analytics.py      # 単独で実行（先に tests/smoke_test.py を実行しておくと全部できます）
    python tests/smoke_test.py          # 通しテストの最後からも呼ばれます

Google（YouTube Data API・YouTube Analytics API）は HttpMockSequence、Claude は偽の通信
（httpx2.MockTransport で SSE を返す）で確認する。ネット・APIキーは使わない。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
PY = sys.executable
VID = "VID12345678"


# ---------------------------------------------------------------- 偽の通信
def sse(obj: dict) -> bytes:
    text = json.dumps(obj, ensure_ascii=False)
    ev = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 300}},
        {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in ev).encode("utf-8")


@contextmanager
def fake_claude(answers: list[dict]):
    """rc_llm._client を、決まった返答を返す偽の通信に差し替える。送った中身は reqs に入る"""
    import anthropic
    import httpx2
    import rc_llm
    reqs, it = [], iter(answers)

    def handler(req):
        reqs.append(json.loads(req.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(next(it)))
    orig = rc_llm._client
    rc_llm._client = lambda: (anthropic, anthropic.Anthropic(
        api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    try:
        yield reqs
    finally:
        rc_llm._client = orig


def fake_google(yt_responses: list, yta_responses: list | None = None):
    """(YouTube, YouTube Analytics) の偽物。送ったリクエストは .sent に (method, uri, body) で入る"""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    class Rec(HttpMockSequence):
        def __init__(self, seq):
            super().__init__(seq)
            self.sent = []

        def request(self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None):
            self.sent.append((method, uri, body))
            return super().request(uri, method, body, headers, redirections, connection_type)
    ok = {"status": "200"}
    h1 = Rec([(ok, json.dumps(r)) if not isinstance(r, tuple) else r for r in yt_responses])
    h2 = Rec([(ok, json.dumps(r)) for r in (yta_responses or [])])
    yt = build("youtube", "v3", http=h1, static_discovery=True, developerKey="x")
    yta = build("youtubeAnalytics", "v2", http=h2, static_discovery=True, developerKey="x")
    yt.http_rec, yta.http_rec = h1, h2
    return yt, yta


@contextmanager
def appdata(path: Path):
    """設定フォルダ（channel_insights.json・連携情報）をテスト用の場所にする"""
    old = os.environ.get("APPDATA")
    os.environ["APPDATA"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old


def retention_points(drops=((0.40, 0.15), (0.75, 0.10)), rise=(0.60, 0.05)) -> list[list]:
    rows = []
    for i in range(1, 101):
        r = i / 100
        w = 1.0 - 0.3 * r - sum(d for at, d in drops if r > at) + (rise[1] if rise[0] < r <= rise[0] + 0.03 else 0)
        rows.append([r, round(w, 4), 0.45])
    return rows


def video_item(duration="PT30S"):
    return {"items": [{"id": VID, "snippet": {"title": "マキタのドリルを本音レビュー", "description": "",
                                              "publishedAt": "2026-10-20T10:00:00Z", "channelId": "UCme"},
                       "contentDetails": {"duration": duration},
                       "statistics": {"viewCount": "1234", "likeCount": "56", "commentCount": "3"},
                       "status": {"privacyStatus": "public"}}]}


RET_HEADERS = [{"name": "elapsedVideoTimeRatio", "columnType": "DIMENSION", "dataType": "FLOAT"},
               {"name": "audienceWatchRatio", "columnType": "METRIC", "dataType": "FLOAT"},
               {"name": "relativeRetentionPerformance", "columnType": "METRIC", "dataType": "FLOAT"}]
TOTALS = {"columnHeaders": [{"name": n, "columnType": "METRIC", "dataType": "FLOAT"} for n in
                            ("views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                             "subscribersGained")],
          "rows": [[1234, 300, 14, 47.5, 12]]}
AI_RET = {"summary": "冒頭はよく見られていますが、スペックの説明の後で離れる人が多いです。",
          "scenes": [{"no": 1, "cause": "穴あけの前の説明が長い可能性", "fix": "説明を短くして、すぐ穴あけを見せる"},
                     {"no": 2, "cause": "締めのあいさつで終わったと思われた可能性", "fix": "締めを短く"}],
          "good_scenes": [{"no": 1, "why": "穴あけの瞬間が見返されている"}],
          "cut_hints": ["スペックの説明は1回にまとめる", "締めのあいさつは5秒以内に"],
          "plan_hints": ["説明が続く所にテロップを入れる"], "shoot_hints": ["結論を最初に言う"]}


# ---------------------------------------------------------------- 小さな部品
def t_units(check, out: Path):
    import rc_analytics as A
    check("分析: 動画の長さを読む", A.parse_duration("PT1H2M3S") == 3723 and A.parse_duration("PT45S") == 45)
    check("分析: Xの文字数（日本語2・URL23）", A.x_length("新しい動画 https://youtu.be/abc #tag") == 39
          and A.x_length("あ" * 140) == 280)

    # 残す区間からの時刻計算が、仕上げのタイムライン（フレーム単位）とほぼ同じ
    import rc_chapters
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    from rc_transcribe import Chunk
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    chunks = [Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))]
    ctx = A.load_context(out)
    errs = []
    for ln in ctx["lines"]:
        c = next(c for c in chunks if c.id == ln["id"])
        errs.append(abs(rc_chapters.chapter_time(tl, c) - ln["t"]))
    check("分析: 完成動画の時刻→発言の対応（誤差0.1秒未満）", errs and max(errs) < 0.1, f"最大 {max(errs or [9]):.3f}秒")
    check("分析: テロップの時刻", [t["text"] for t in ctx["telops"]][:1] == ["ドリルをレビュー！"]
          and abs(ctx["telops"][0]["t"] - next(ln["t"] for ln in ctx["lines"] if ln["id"] == 2)) < 0.01)

    # 離脱場面の検出：大きく減った所を見つけ、終わりの5%は除く
    pts = [{"r": r, "w": w, "rel": rel} for r, w, rel in retention_points(drops=((0.40, 0.15), (0.75, 0.10),
                                                                                  (0.97, 0.2)))]
    drops, ups = A.find_scenes(pts, 600.0)
    at = sorted(round(d["a"] / 600, 2) for d in drops)
    check("分析: 離脱場面を見つける", at[:2] == [0.40, 0.75] and all(a < 0.95 for a in at), str(at))
    check("分析: 見返された場面を見つける", [round(u["a"] / 600, 2) for u in ups] == [0.60], str(ups))
    flat = [{"r": i / 100, "w": 1 - 0.003 * i, "rel": None} for i in range(1, 101)]
    check("分析: なだらかな動画では離脱場面なし", A.find_scenes(flat, 600.0)[0] == [])


def t_scopes(check, base: Path):
    import rc_analytics as A
    import rc_youtube as Y
    check("分析: 連携の許可に yt-analytics.readonly を追加", A.ANALYTICS_SCOPE in Y.SCOPES
          and "https://www.googleapis.com/auth/youtube.force-ssl" in Y.SCOPES)
    with appdata(base / "scope"):
        tp = Y.token_path()
        tp.unlink(missing_ok=True)
        s0 = A.link_state()
        tp.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                  "scopes": Y.SCOPES[:2]}), encoding="utf-8")
        s1 = A.link_state()
        code = ""
        try:
            A._credentials()
        except SystemExit as e:
            code = str(e.code)
        tp.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                  "scopes": Y.SCOPES, "expiry": "2999-01-01T00:00:00Z"}), encoding="utf-8")
        s2 = A.link_state()
        creds = A._credentials()
    check("分析: 連携の状態（未連携→許可が足りない→OK）", (s0, s1, s2) == ("none", "old", "ok"), str((s0, s1, s2)))
    check("分析: 前からの連携では、連携し直しを案内", "連携をやり直す" in code, code[:60])
    check("分析: 保存した許可の範囲のまま読む", A.ANALYTICS_SCOPE in (creds.scopes or []))


# ---------------------------------------------------------------- 視聴維持率
def t_retention(check, out: Path, base: Path):
    import analytics
    import rc_analytics as A
    import rc_llm
    import rc_plan
    with appdata(base / "appdata"):
        yt, yta = fake_google([video_item()], [{"columnHeaders": RET_HEADERS, "rows": retention_points()}, TOTALS])
        orig = A.get_services
        A.get_services = lambda: (yt, yta)
        try:
            with fake_claude([AI_RET]) as reqs:
                analytics.main([str(out), "--retention", "--video-id", VID, "--llm", "api"])
        finally:
            A.get_services = orig
        a = out / "analytics"
        res = json.loads((a / "analysis.json").read_text(encoding="utf-8"))
        rep = (a / "report.md").read_text(encoding="utf-8")
        check("分析: 視聴維持率のレポート", "穴あけの前の説明が長い可能性" in rep and "スペックの説明は1回にまとめる" in rep)
        check("分析: グラフの画像", (a / "retention.png").exists() and (a / "retention.png").stat().st_size > 3000)
        frames = [f for f in res["frames"] if f]
        check("分析: 離脱場面の静止画を完成動画から取り出す", len(frames) == len(res["drops"]) >= 2, str(len(frames)))
        sent = [u for _m, u, _b in yta.http_rec.sent]
        check("分析: Analytics API への問い合わせ", any("elapsedVideoTimeRatio" in u and f"video%3D%3D{VID}" in u
                                                        and "audienceWatchRatio" in u for u in sent), sent[0][:200])
        msg = reqs[0]["messages"][0]["content"] if reqs else []
        texts = " ".join(b.get("text", "") for b in msg if isinstance(b, dict))
        imgs = [b for b in msg if isinstance(b, dict) and b.get("type") == "image"]
        check("分析: Claude に場面の発言・テロップ・画面を渡す",
              "きれいに開きました" in texts and "テロップ" in texts and len(imgs) == len(frames), f"画像{len(imgs)}枚")
        check("分析: 構造化出力で頼む", reqs and reqs[0]["output_config"]["format"]["type"] == "json_schema")
        # 1つ目の離脱（40%）の場面は、完成動画の12秒あたり → 「穴を開けてみます」の前後
        d1 = res["drops"][0]
        said = " ".join(ln["text"] for ln in res["contexts"][0]["lines"])
        check("分析: 離脱場面を元の発言と対応づける", 11.5 <= d1["a"] <= 12.5 and "実際に穴を開けてみます" in said,
              f"{d1['a']}秒")

        ins = json.loads(A.insights_path().read_text(encoding="utf-8"))
        check("分析: channel_insights.json に記録", VID in ins["videos"]
              and ins["videos"][VID]["cut_hints"][0] == "スペックの説明は1回にまとめる")

        # 次の荒編集・演出プランの AI への指示に入る
        cut = rc_llm._cut_instructions("talk", [])
        plan = rc_plan._instructions("talk", ["calm"])
        check("分析: 次のカット判定の指示に傾向を足す", "スペックの説明は1回にまとめる" in cut and "過去の動画の分析" in cut)
        check("分析: 次の演出プランの指示に傾向を足す", "説明が続く所にテロップを入れる" in plan)
        ins["use_in_prompts"] = False
        A.insights_path().write_text(json.dumps(ins, ensure_ascii=False), encoding="utf-8")
        check("分析: 「AIに伝える」を切ると足さない", "過去の動画の分析" not in rc_llm._cut_instructions("talk", []))
        ins["use_in_prompts"] = True
        A.insights_path().write_text(json.dumps(ins, ensure_ascii=False), encoding="utf-8")
    with appdata(base / "empty_appdata"):
        check("分析: 分析していなければ、指示は今までどおり",
              "過去の動画の分析" not in rc_llm._cut_instructions("craft", []) and rc_llm.channel_notes("plan", "talk") == "")

    # claude.ai に貼る流れ：プロンプトを作る → 返答を読む（YouTube には問い合わせ直さない）
    with appdata(base / "appdata"):
        yt, yta = fake_google([video_item()], [{"columnHeaders": RET_HEADERS, "rows": retention_points()}, TOTALS])
        A.get_services = lambda: (yt, yta)
        try:
            analytics.main([str(out), "--retention", "--video-id", VID, "--llm", "manual"])
            prompt = out / "analytics" / "prompt_analytics.md"
            ok1 = prompt.exists() and "JSON Schema" in prompt.read_text(encoding="utf-8")
            ans = out / "analytics" / "claude_analytics.json"
            ans.write_text("```json\n" + json.dumps({**AI_RET, "summary": "貼り付けた返答"}, ensure_ascii=False) + "\n```",
                           encoding="utf-8")

            def no_google():
                raise AssertionError("YouTube に問い合わせ直した")
            A.get_services = no_google
            analytics.main([str(out), "--retention", "--llm", "manual", "--answer", str(ans)])
            ok2 = json.loads((out / "analytics" / "analysis.json").read_text(encoding="utf-8"))["ai"]["summary"] \
                == "貼り付けた返答"
        except (AssertionError, SystemExit) as e:
            ok1 = ok2 = False
            print("   ", e)
        finally:
            A.get_services = orig
    check("分析: claude.ai 用のプロンプトを作る", ok1)
    check("分析: claude.ai の返答を読む（動画IDは前回の分析から）", ok2)

    # AIを使わない・データがまだない（別の設定フォルダで。上の分析結果は画面のテストで使う）
    with appdata(base / "appdata_none"):
        yt, yta = fake_google([video_item()], [{"columnHeaders": RET_HEADERS, "rows": retention_points()}, TOTALS])
        r = A.analyze_retention(None, VID, "none", services=(yt, yta), frames=False)
        check("分析: AIなしでも数字だけのレポート", r and r["ai"]["by_ai"] is False and len(r["drops"]) >= 2)
        yt, yta = fake_google([video_item()], [{"columnHeaders": RET_HEADERS, "rows": []}, TOTALS])
        code = ""
        try:
            A.analyze_retention(None, VID, "none", services=(yt, yta), frames=False)
        except SystemExit as e:
            code = str(e.code)
        check("分析: データがまだないときの案内", "2〜3日" in code, code[:50])
        from googleapiclient.errors import HttpError
        import httplib2
        e403 = HttpError(httplib2.Response({"status": 403}),
                         b'{"error": {"errors": [{"reason": "insufficientPermissions"}]}}')
        check("分析: 許可が足りないエラーは、連携し直しを案内", "連携をやり直す" in A.api_error_text(e403))


def t_trends(check, base: Path):
    import rc_analytics as A
    with appdata(base / "appdata"):
        d = A.load_insights()
        d["videos"]["OTHER1"] = {"title": "工作の動画", "mode": "craft", "analyzed_at": "2026-10-01T00:00:00+00:00",
                                 "numbers": {"retention_30s": 0.6, "duration": 600}, "summary": "前置きが長い",
                                 "cut_hints": ["前置きは10秒以内に"], "plan_hints": []}
        A.save_insights(d)
        trends_ai = {"summary": "どの動画も、前置きの長さで離れる人が多いです。",
                     "cut_hints": [{"text": "前置きは10秒以内に", "mode": "all"},
                                   {"text": "作業の無言は早送りにする", "mode": "craft"}],
                     "plan_hints": [{"text": "説明が続く所にテロップを入れる", "mode": "all"}]}
        with fake_claude([trends_ai]) as reqs:
            A.build_trends("api")
        sent = reqs[0]["messages"][0]["content"] if reqs else ""
        check("分析: 複数本の傾向をまとめる", "工作の動画" in sent and "マキタ" in sent)
        talk, craft = A.prompt_notes("cut", "talk"), A.prompt_notes("cut", "craft")
        check("分析: 傾向はジャンルごとに使い分ける", "早送り" in craft and "早送り" not in talk and "前置き" in talk)


# ---------------------------------------------------------------- コメント
def thread(i, text, likes=0, replies=None):
    return {"id": f"T{i}", "snippet": {"channelId": "UCme", "videoId": VID, "totalReplyCount": len(replies or []),
                                       "topLevelComment": {"id": f"C{i}", "snippet": {
                                           "authorDisplayName": f"視聴者{i}", "textDisplay": text, "likeCount": likes,
                                           "publishedAt": "2026-10-21T00:00:00Z",
                                           "authorChannelId": {"value": f"UCv{i}"}}}},
            "replies": {"comments": replies or []}}


def t_comments(check, out: Path, base: Path):
    import analytics
    import rc_analytics as A
    my_reply = [{"id": "R1", "snippet": {"authorChannelId": {"value": "UCme"}, "textDisplay": "ありがとう"}}]
    page1 = {"items": [thread(1, "重さはバッテリー込みですか？", 5), thread(2, "最高でした！", 2, my_reply)],
             "nextPageToken": "P2"}
    page2 = {"items": [thread(3, "格安で買えるサイトはこちら http://spam.example")]}
    ai = {"summary": "好評です。重さの質問があります。", "praise": ["分かりやすい"], "requests": [],
          "questions": ["重さはバッテリー込みか"], "concerns": [],
          "replies": [{"c": 1, "reply": "バッテリー込みで1.2kgです！", "why": "質問なので"}],
          "skip": [{"c": 3, "why": "宣伝"}]}
    with appdata(base / "appdata"):
        yt, _ = fake_google([video_item(), page1, page2])
        orig = A.get_services
        A.get_services = lambda: (yt, None)
        try:
            with fake_claude([ai]) as reqs:
                analytics.main([str(out), "--comments", "--video-id", VID])
        finally:
            A.get_services = orig
        a = out / "analytics"
        raw = json.loads((a / "comments.json").read_text(encoding="utf-8"))
        res = json.loads((a / "comments_ai.json").read_text(encoding="utf-8"))
        check("分析: コメントを2ページ分読む", len(raw["comments"]) == 3)
        check("分析: 自分が返信済みのコメントが分かる", [c["answered"] for c in raw["comments"]] == [False, True, False])
        sent = reqs[0]["messages"][0]["content"] if reqs else ""
        check("分析: Claude にコメントを渡す（返信済みの印つき）", "C2 [いいね2・返信1]（返信済み）" in sent, sent[:80])
        check("分析: 返信案を元のコメントIDに対応づける", res["replies"][0]["comment_id"] == "C1"
              and res["skip"][0]["comment_id"] == "C3")
        check("分析: コメントのまとめ", "バッテリー込みで1.2kgです！" in (a / "comments.md").read_text(encoding="utf-8"))

        # 返信の投稿（人が確認してから）
        yt, _ = fake_google([{"id": "NEWREPLY", "snippet": {}}])
        A.get_services = lambda: (yt, None)
        try:
            analytics.main([str(out), "--reply", "C1", "--text", "バッテリー込みで1.2kgです！", "--video-id", VID])
        finally:
            A.get_services = orig
        posts = [(m, b) for m, u, b in yt.http_rec.sent if m == "POST"]
        body = json.loads(posts[0][1]) if posts else {}
        check("分析: 返信を投稿する（comments.insert）", body.get("snippet", {}).get("parentId") == "C1"
              and "1.2kg" in body["snippet"].get("textOriginal", ""))
        res = json.loads((a / "comments_ai.json").read_text(encoding="utf-8"))
        check("分析: 投稿済みの印", bool(res["replies"][0].get("posted")))

        # 30日たったコメントのデータは消す
        old_dir = base / "old_comments"
        old_dir.mkdir(exist_ok=True)
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        (old_dir / "comments.json").write_text(json.dumps({"fetched_at": old, "comments": []}), encoding="utf-8")
        (old_dir / "comments_ai.json").write_text("{}", encoding="utf-8")
        A.remember_comment_dir(old_dir)
        A.prune_all()
        check("分析: 30日たったコメントのデータを消す", not (old_dir / "comments.json").exists()
              and not (old_dir / "comments_ai.json").exists() and (a / "comments.json").exists())


# ---------------------------------------------------------------- 告知文
def t_promo(check, out: Path, base: Path):
    import analytics
    import rc_analytics as A
    ai = {"x_posts": ["マキタのドリルを本音でレビューしました！軽さに驚き #マキタ", "穴あけ一瞬。" + "長い" * 150],
          "instagram_caption": "ドリルのレビューです\nプロフィールのリンクから見られます",
          "instagram_hashtags": ["#マキタ", "ドリル", "DIY"],
          "shorts": [{"no": 1, "title": "一瞬で穴があく", "description": "マキタのドリルで穴あけ"}]}
    with appdata(base / "appdata"):
        with fake_claude([ai]) as reqs:
            analytics.main([str(out), "--promo", "--video-id", VID])
        p = json.loads((out / "analytics" / "promo.json").read_text(encoding="utf-8"))
    sent = reqs[0]["messages"][0]["content"] if reqs else ""
    check("分析: 告知文に見どころを渡す", "S1 [" in sent and "穴あけ成功" in sent)
    check("分析: Xの投稿にURLを付けて文字数を確認", p["x_posts"][0]["text"].endswith(f"https://youtu.be/{VID}")
          and p["x_posts"][0]["ok"] and not p["x_posts"][1]["ok"])
    check("分析: Instagram のハッシュタグ", p["instagram_hashtags"] == ["マキタ", "ドリル", "DIY"]
          and "#DIY" in p["instagram_text"])
    s = p["shorts"][0]
    check("分析: ショートの説明（本編へのリンクと #Shorts）", f"https://youtu.be/{VID}" in s["description"]
          and "#Shorts" in s["description"] and s["label"] == "穴あけ成功")
    with appdata(base / "appdata"):
        r = A.make_promo(out, "", "none")
    check("分析: AIなし・動画IDなしでも告知文", r["x_posts"] and A.URL_PLACEHOLDER in r["x_posts"][0]["text"])
    # 動画IDなし・告知文だけなら、前の分析の動画IDを使う
    check("分析: 前の分析から動画IDを読む", A.guess_video_id(out) == VID)


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import json, os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer
import gui
app = QApplication([])
shown = []
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
w = gui.MainWindow(); w.resize(1300, 900); w.show()
res = {}
res["tab"] = [w.tabs.tabText(i) for i in range(w.tabs.count())]
# 拡張（gui_ext_*.py）が項目を足したメニューだけ出す（何もないメニューは出さない）
res["menus_hidden"] = all(m.menuAction().isVisible() == bool(m.actions()) for m in (w.tools_menu, w.help_menu))
out = Path(OUT)
w.review.load(out)
btn = [w.review.ext_bar.itemAt(i).widget() for i in range(w.review.ext_bar.count())]
btn = [b for b in btn if b is not None and b.text() == "公開後の分析"]
res["ext_btn"] = bool(btn)
btn[0].click()
pg = w.analytics
for _ in range(10): app.processEvents()
res["vid"] = pg.vid.text()
res["scene_rows"] = pg.t_scene.rowCount()
res["cause"] = pg.t_scene.item(0, 4).text() if pg.t_scene.rowCount() else ""
pg.t_scene.setCurrentCell(0, 0)
for _ in range(5): app.processEvents()
res["frame"] = pg.frame.pixmap() is not None and not pg.frame.pixmap().isNull()
res["chart"] = pg.chart.pixmap() is not None and not pg.chart.pixmap().isNull()
names = ["retention", "comments", "promo", "trends", "audit"]
for i, n in enumerate(names):
    pg.tabs.setCurrentIndex(i)
    for _ in range(5): app.processEvents()
    w.grab().save(str(out / f"gui_analytics_{n}.png"))
res["reply_rows"] = pg.t_reply.rowCount()
res["reply_text"] = pg.t_reply.item(0, 2).text() if pg.t_reply.rowCount() else ""
res["link"] = pg.link.text()
res["videos_rows"] = pg.t_videos.rowCount()
# 「AIに伝える」のチェックを外すと保存される
pg.use.setChecked(False)
import rc_analytics as A
res["use_off"] = A.load_insights()["use_in_prompts"] is False
pg.use.setChecked(True)
# ボタンで analytics.py を呼ぶ（AIなし）
calls = []
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    if handle_error:
        res["handled"] = handle_error("[エラー] 分析を使うには、Googleアカウントとの連携をやり直す必要があります")
    return True
w.runner.start = fake_start
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
pg.run_task("promo", ["--promo"])
pg.run_task("analytics", ["--retention"])
res["calls"] = calls
# 連携し直しが必要なとき（許可が足りない）は、分析を始める前に知らせる
res["blocked_before_run"] = len([c for c in calls if "--retention" in c[1]]) == 0
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, base: Path):
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(out)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(base / "appdata"), PYTHONIOENCODING="utf-8",
               XDG_CONFIG_HOME=str(base / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("分析: 画面のテスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("分析: 画面のテスト", False, (p.stdout + p.stderr)[-1500:])
        return
    r = json.loads(line[7:])
    check("分析: 「分析」タブ", "分析" in r["tab"], str(r["tab"]))
    check("分析: 拡張を入れても空のメニューは出さない", r["menus_hidden"])
    check("分析: 確認・仕上げタブに「公開後の分析」ボタン", r["ext_btn"])
    check("分析: 画面で動画IDを読み込む", r["vid"] == VID, r["vid"])
    check("分析: 画面に離脱場面と原因", r["scene_rows"] >= 2 and "可能性" in r["cause"], r["cause"])
    check("分析: 画面にグラフと場面の静止画", r["chart"] and r["frame"])
    check("分析: 画面に返信案", r["reply_rows"] == 1 and "1.2kg" in r["reply_text"])
    check("分析: 画面に分析した動画の一覧", r["videos_rows"] >= 2)
    check("分析: 「AIに伝える」の切り替えを保存", r["use_off"])
    calls = r["calls"]
    check("分析: 画面から analytics.py を呼ぶ", calls and calls[0][0] == "analytics.py" and "--promo" in calls[0][1]
          and str(out) in calls[0][1] and "none" in calls[0][1], str(calls)[:200])
    check("分析: 連携し直しが必要なら、分析の前に知らせる", r["blocked_before_run"] and "連携" in r["link"], r["link"])
    check("分析: 連携し直しのエラーは画面で案内", r.get("handled") is True)


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    base = fx / "analytics"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    if not (out / "final_info.json").exists():
        check("分析のテスト（完成動画が必要）", False, "先に tests/smoke_test.py で完成動画まで作る必要があります")
        return
    ad = base / "out"
    shutil.copytree(out, ad, ignore=shutil.ignore_patterns("work", "analytics", "gui_*.png"))
    meta = {"title": "マキタのドリルを本音レビュー", "video_id": "", "tags": ["マキタ", "ドリル"]}
    (ad / "youtube_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    t_units(check, ad)
    t_scopes(check, base)
    t_retention(check, ad, base)
    t_trends(check, base)
    t_comments(check, ad, base)
    t_promo(check, ad, base)
    # 画面：許可が足りない連携（前からの連携）の状態で開く
    import rc_youtube as Y
    with appdata(base / "appdata"):
        Y.token_path().write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c",
                                              "client_secret": "s", "scopes": Y.SCOPES[:2]}), encoding="utf-8")
    t_gui(check, ad, base)
    doc = ROOT / "docs" / "youtube_audit_draft.md"
    check("分析: 審査の下書き", doc.exists() and "Audit and Quota Extension Form" in doc.read_text(encoding="utf-8"))


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
