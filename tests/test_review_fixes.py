"""統合後の確認で見つかった問題の直しのテスト（早送りと分析の時刻・日本語のパス・処理中の結果の切り替えなど）。

    python tests/test_review_fixes.py   # 単独で実行（先に tests/smoke_test.py を実行しておくと全部できます）
    python tests/smoke_test.py          # 通しテストの最後からも呼ばれます

Claude は偽の通信（httpx2.MockTransport で SSE を返す）、Google は HttpMockSequence。画面は offscreen で動かす。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
PY = sys.executable


# ---------------------------------------------------------------- 1. 分析の時刻（早送り）
def t_analytics_speedup(check, base: Path):
    import rc_analytics as A
    bm = A.BodyMap([(0, 100)], [{"start": 20, "end": 60, "speed": 4}])
    r = bm.range(70, 71)
    check("分析: 早送りのあとの発言は、縮めた分だけ早い時刻", r is not None and abs(r[0] - 40) < 1e-6, str(r))
    r2 = bm.range(30, 34)
    check("分析: 早送りの中の発言は 1/倍率 の長さ", r2 is not None and abs(r2[0] - 22.5) < 1e-6
          and abs(r2[1] - 23.5) < 1e-6, str(r2))
    check("分析: 本編の長さも早送りの分だけ短い", abs(bm.duration - 70) < 1e-6, str(bm.duration))
    bm2 = A.BodyMap([(0, 10), (20, 100)], [{"start": 30, "end": 70, "speed": 4}])
    r3 = bm2.range(80, 81)
    check("分析: カットと早送りの両方", r3 is not None and abs(r3[0] - (10 + 10 + 10 + 10)) < 1e-6, str(r3))
    check("分析: 早送りがなければ今までどおり", A.BodyMap([(0, 10), (20, 30)]).range(25, 26) == (15.0, 16.0))

    od = base / "analytics_speed"
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True)
    chunks = [{"id": 1, "start": 5, "end": 8, "text": "まずは開封します"},
              {"id": 2, "start": 30, "end": 34, "text": "ここから穴をあけます"},
              {"id": 3, "start": 70, "end": 73, "text": "完成しました"}]
    (od / "session.json").write_text(json.dumps({"keep": [[0, 100]], "cut_ids": [], "mode": "craft",
                                                 "stem": "工作"}, ensure_ascii=False), encoding="utf-8")
    (od / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    (od / "plan_used.json").write_text(json.dumps({
        "telops": [{"start_id": 3, "end_id": 3, "text": "完成！", "style": "emphasis"}], "zooms": [{"id": 3}],
        "speedups": [{"start": 20, "end": 60, "speed": 4}]}, ensure_ascii=False), encoding="utf-8")
    (od / "final_info.json").write_text(json.dumps({"duration": 73.0, "intro_offset": 3.0,
                                                    "chapters": [{"t": 0, "title": "オープニング"}]}),
                                        encoding="utf-8")
    ctx = A.load_context(od)
    by = {ln["id"]: ln for ln in ctx["lines"]}
    check("分析: load_context が plan の早送りを使う（発言）", abs(by[3]["t"] - (40 + 3)) < 0.01, str(by.get(3)))
    check("分析: load_context が plan の早送りを使う（テロップ・ズーム）",
          ctx["telops"] and abs(ctx["telops"][0]["t"] - 43) < 0.01 and abs(ctx["zooms"][0] - 43) < 0.01,
          str(ctx["telops"]) + str(ctx["zooms"]))
    sc = A.scene_context(ctx, 42, 45)
    check("分析: 離脱場面の直前の発言が正しい", any(ln["id"] == 3 for ln in sc["lines"]) and sc["where"] == "",
          str([ln["id"] for ln in sc["lines"]]) + sc["where"])
    # 手直し（plan_edited.json）が優先される
    (od / "plan_edited.json").write_text(json.dumps({"telops": [], "zooms": [], "speedups": []}), encoding="utf-8")
    ctx2 = A.load_context(od)
    check("分析: 手直しした早送り（plan_edited.json）を優先", abs({ln["id"]: ln for ln in ctx2["lines"]}[3]["t"] - 73) < 0.01)


# ---------------------------------------------------------------- 2. Google のエラー・許可
def t_google(check, base: Path):
    import rc_analytics as A
    from googleapiclient.errors import HttpError

    class Resp(dict):
        def __init__(self, status):
            super().__init__(status=str(status))
            self.status = status
            self.reason = ""
    body = json.dumps({"error": {"code": 403, "message": "YouTube Analytics API has not been used in project 123 "
                                 "before or it is disabled.", "errors": [{"reason": "accessNotConfigured"}]}}).encode()
    e = HttpError(Resp(403), body, uri="https://youtubeanalytics.googleapis.com/v2/reports?metrics=audienceWatchRatio")
    msg = A.api_error_text(e)
    check("分析: APIが無効のときは「有効にする」案内", "YouTube Analytics API" in msg and "有効" in msg
          and "持ち主" not in msg, msg)
    # 403 のあとに、比べる相手の指標を外して問い合わせ直さない
    from test_analytics import RET_HEADERS, retention_points

    def fake_yta(seq):
        from googleapiclient.discovery import build
        from googleapiclient.http import HttpMockSequence

        class Rec(HttpMockSequence):
            def __init__(self, sq):
                super().__init__(sq)
                self.sent = []

            def request(self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None):
                self.sent.append((method, uri))
                return super().request(uri, method, body, headers, redirections, connection_type)
        h = Rec([r if isinstance(r, tuple) else ({"status": "200"}, json.dumps(r)) for r in seq])
        y = build("youtubeAnalytics", "v2", http=h, static_discovery=True, developerKey="x")
        y.http_rec = h
        return None, y
    yt, yta = fake_yta([({"status": "403"}, body.decode())])
    try:
        A.fetch_retention(yta, "VID", "2026-10-20T10:00:00Z")
        ok = False
    except HttpError:
        ok = True
    check("分析: 403 のときは問い合わせ直さない（URL に指標の名前が入っていても）",
          ok and len(yta.http_rec.sent) == 1, str(len(yta.http_rec.sent)))
    yt, yta = fake_yta([({"status": "400"}, json.dumps({"error": {"code": 400, "message": "Query not supported."}})),
                               {"columnHeaders": RET_HEADERS[:2], "rows": [r[:2] for r in retention_points()]},
                               {"columnHeaders": [], "rows": []}])
    pts, _ = A.fetch_retention(yta, "VID", "2026-10-20T10:00:00Z")
    check("分析: 400 のときは維持率だけで取り直す", len(pts) == 100 and pts[0]["rel"] is None, str(len(pts)))

    # Google の許可の画面で一部のチェックを外されたとき
    import google_auth_oauthlib.flow as F
    import rc_youtube as Y
    from google.oauth2.credentials import Credentials
    appd = base / "appdata_review_fixes"
    shutil.rmtree(appd, ignore_errors=True)
    old = os.environ.get("APPDATA")
    os.environ["APPDATA"] = str(appd)
    try:
        Y.client_secret_path().parent.mkdir(parents=True, exist_ok=True)
        Y.client_secret_path().write_text(json.dumps({"installed": {
            "client_id": "x", "client_secret": "y", "auth_uri": "https://a", "token_uri": "https://t",
            "redirect_uris": ["http://localhost"]}}), encoding="utf-8")
        granted = {"v": Y.SCOPES[:2]}

        class FakeFlow:
            @classmethod
            def from_client_secrets_file(cls, *_a, **_k):
                return cls()

            def run_local_server(self, **_k):
                check("分析: 一部の許可でも止まらない設定（OAUTHLIB_RELAX_TOKEN_SCOPE）",
                      os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1")
                return Credentials("tok", refresh_token="r", token_uri="https://t", client_id="x", client_secret="y",
                                   scopes=Y.SCOPES, granted_scopes=granted["v"])
        orig = F.InstalledAppFlow
        F.InstalledAppFlow = FakeFlow
        try:
            try:
                A.relink()
                ok = False
                text = ""
            except SystemExit as ex:
                ok, text = True, str(ex)
            check("分析: 分析の許可が外されたら、分かりやすく知らせる", ok and "チェック" in text, text)
            check("分析: 実際に許可された範囲で保存（連携し直しが必要と分かる）", A.link_state() == "old"
                  and A.ANALYTICS_SCOPE not in A.token_scopes(), str(A.token_scopes()))
            granted["v"] = [Y.SCOPES[2]]
            try:
                A.oauth_login(Y.client_secret_path())
                text = ""
            except SystemExit as ex:
                text = str(ex)
            check("分析: YouTube の許可が外されたら、エラーで案内", "アップロード" in text, text)
            granted["v"] = list(Y.SCOPES)
            A.relink()
            check("分析: すべて許可されたら ok", A.link_state() == "ok")
        finally:
            F.InstalledAppFlow = orig
    finally:
        if old is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old


def t_comments_replied(check):
    """返信が多いスレッドで、一部しか返ってこない返信に自分の返信がなくても、全部読んで確かめる"""
    import rc_analytics as A
    from test_analytics import fake_google

    def c(cid, author, text):
        return {"id": cid, "snippet": {"authorChannelId": {"value": author}, "textDisplay": text,
                                       "authorDisplayName": author}}
    th = {"items": [{"id": "T1", "snippet": {"channelId": "UCme", "totalReplyCount": 7, "topLevelComment": {
        "id": "C1", "snippet": {"authorChannelId": {"value": "UCx"}, "textDisplay": "質問です？", "likeCount": 3}}},
        "replies": {"comments": [c("R1", "UCa", "わかる"), c("R2", "UCb", "同じく")]}}]}
    full = {"items": [c("R1", "UCa", "わかる"), c("R2", "UCb", "同じく"), c("R3", "UCme", "ありがとうございます")]}
    yt, _ = fake_google([th, full])
    res = A.fetch_comments(yt, "VID")
    check("コメント: 一部だけの返信でも、自分の返信を見つける", res and res[0]["answered"] and "_shown" not in res[0],
          str(res))


# ---------------------------------------------------------------- 3. Claude の途中での切り替え（fallback）
def _sse_fallback(partial: str, full: str, iterations: bool = True) -> bytes:
    usage_final = {"output_tokens": 300}
    if iterations:
        usage_final["iterations"] = [
            {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 50,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 1100, "output_tokens": 300,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]
    ev = [
        {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None, "usage": {"input_tokens": 1100, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": partial}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {
            "type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
            "trigger": {"type": "refusal"}}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": full}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": usage_final},
        {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in ev).encode("utf-8")


def _fake_client(bodies: list[bytes]):
    import anthropic
    import httpx2
    it = iter(bodies)

    def handler(_req):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=next(it))
    return anthropic, anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def t_llm_fallback(check):
    import rc_llm
    schema = {"type": "object", "properties": {"titles": {"type": "array", "items": {"type": "string"}}}}
    full = json.dumps({"titles": ["a", "b"]})
    anthropic, client = _fake_client([_sse_fallback('{"titles": ["a"', full)])
    usage = {"in": 0, "out": 0}
    try:
        res = rc_llm._call(client, anthropic, "s", "u", schema, "low", usage)
    except Exception as e:  # noqa: BLE001
        res = repr(e)
    check("Claude: 途中で切り替わっても、切り替わった後の返答を読む", res == {"titles": ["a", "b"]}, str(res))
    check("Claude: 断られた分のトークンも数える", usage["in"] == 2100 and usage["out"] == 350, str(usage))
    want = 2100 / 1e6 * 4 + 350 / 1e6 * 20 + (1100 / 1e6 * 1 + 300 / 1e6 * 5)
    check("Claude: 切り替わった先のモデルの料金で数える", abs(rc_llm.cost_usd(usage) - want) < 1e-9,
          f"{rc_llm.cost_usd(usage)} / {want}")
    # 続きとして書かれた場合（つなげると JSON になる）
    anthropic, client = _fake_client([_sse_fallback('{"titles": ["a", ', '"b"]}', iterations=False)])
    try:
        res = rc_llm._call(client, anthropic, "s", "u", schema, "low", {"in": 0, "out": 0})
    except Exception as e:  # noqa: BLE001
        res = repr(e)
    check("Claude: 続きとして書かれた返答も読める", res == {"titles": ["a", "b"]}, str(res))
    # 壊れた JSON は RuntimeError（ほかの失敗と同じ）
    anthropic, client = _fake_client([_sse_fallback('{"t', '{"titles": [', iterations=False)])
    try:
        rc_llm._call(client, anthropic, "s", "u", schema, "low", {"in": 0, "out": 0})
        kind = "なし"
    except RuntimeError as e:
        kind = "RuntimeError " + str(e)
    except Exception as e:  # noqa: BLE001
        kind = repr(e)
    check("Claude: 壊れた返答は RuntimeError（JSONDecodeError で落ちない）", kind.startswith("RuntimeError"), kind)

    # 分析の ai_task は、断られたら AI なしの分析に切り替える
    import rc_analytics as A
    orig = rc_llm._client
    rc_llm._client = lambda: _fake_client([_sse_fallback('{"t', '{"x', iterations=False)])
    try:
        res, usage = A.ai_task("t", "s", "u", schema, "api", ROOT / "tests" / "_fixtures")
        check("分析: Claude が断ったり壊れた返答なら、AIなしの分析に切り替える（落ちない）", res is None)
    except BaseException as e:  # noqa: BLE001
        check("分析: Claude が断ったり壊れた返答なら、AIなしの分析に切り替える（落ちない）", False, repr(e))
    finally:
        rc_llm._client = orig


# ---------------------------------------------------------------- 4. 日本語のパス（OpenCV）
def t_opencv_paths(check, base: Path):
    import cv2
    import numpy as np
    import rc_thumbspro as T
    jp = base / "日本語 フォルダ" / "山田"
    shutil.rmtree(jp.parent, ignore_errors=True)
    (jp / "cv2data").mkdir(parents=True)
    src = Path(cv2.data.haarcascades)
    shutil.copy(src / "haarcascade_frontalface_default.xml", jp / "cv2data")
    model = jp / T.MODEL_NAME
    shutil.copy(ROOT / "tests" / "data" / "fake_humanseg.onnx", model)

    # Windows の OpenCV と同じく、ASCII 以外の文字を含むパスの文字列は開けないことにする
    real_cc, real_onnx = cv2.CascadeClassifier, cv2.dnn.readNetFromONNX

    def cc(*a):
        if a and isinstance(a[0], str) and not a[0].isascii():
            return real_cc()   # 空（empty() が True）
        return real_cc(*a)

    def onnx(x):
        if isinstance(x, str) and not x.isascii():
            raise cv2.error("Failed to parse ONNX model")
        return real_onnx(x)
    old_dir = cv2.data.haarcascades
    cv2.CascadeClassifier, cv2.dnn.readNetFromONNX = cc, onnx
    cv2.data.haarcascades = str(jp / "cv2data") + os.sep
    T._cascades.clear()
    T._nets.clear()
    try:
        c = T._cascade("haarcascade_frontalface_default.xml")
        check("サムネイル: 日本語のフォルダでも顔検出のデータを読める", c is not None and not c.empty())
        prob = T._model_prob(np.zeros((300, 300, 3), np.uint8), model)
        check("サムネイル: 日本語のフォルダでも人物のデータを読める", prob is not None, "")
        try:
            T._read_onnx(model)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check("サムネイル: ダウンロードの確認も中身から読む", ok)
    finally:
        cv2.CascadeClassifier, cv2.dnn.readNetFromONNX = real_cc, real_onnx
        cv2.data.haarcascades = old_dir
        T._cascades.clear()
        T._nets.clear()


# ---------------------------------------------------------------- 5. MV の素材集め
def t_mv_collect(check, base: Path):
    import rc_mv
    root = base / "mvt" / "work" / "MV素材"
    shutil.rmtree(base / "mvt", ignore_errors=True)
    (root / "work").mkdir(parents=True)
    (root / "旅_mv").mkdir()
    for p in (root / "a.mp4", root / "b.mp4", root / "work" / "x.mp4", root / "旅_mv" / "y.mp4"):
        p.write_bytes(b"x")
    got = [p.name for p in rc_mv.collect_materials([root])]
    check("MV: 上の階層に work があっても素材を集める", got == ["a.mp4", "b.mp4"], str(got))
    r2 = base / "mvt" / "旅_roughcut" / "素材"
    r2.mkdir(parents=True)
    (r2 / "c.mp4").write_bytes(b"x")
    got2 = [p.name for p in rc_mv.collect_materials([r2])]
    check("MV: ○○_roughcut の中に置いた素材も使える", got2 == ["c.mp4"], str(got2))


# ---------------------------------------------------------------- 6. 小さな直し
def t_small(check, base: Path):
    # 順番待ちの一覧: 同時に書いても、相手の更新が消えない・一時ファイルがぶつからない
    import threading
    import rc_ops_queue as Q
    qp = base / "queue_lock" / "queue.json"
    shutil.rmtree(qp.parent, ignore_errors=True)
    q = Q.Queue(qp)
    q.save([{"id": f"i{k}", "status": "waiting", "n": 0} for k in range(4)])
    errs = []

    def work(k):
        try:
            for _ in range(25):
                it = q.get(f"i{k}")
                q.update(f"i{k}", n=it["n"] + 1)
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    ths = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    ns = [it["n"] for it in q.load()]
    check("まとめて処理: 同時に書き換えても更新が消えない", ns == [25] * 4 and not errs, f"{ns} {errs[:1]}")
    check("まとめて処理: 一時ファイル・ロックが残らない", not list(qp.parent.glob("*.tmp")) and
          not list(qp.parent.glob("*.lock")))

    # テンプレートの enabled を画面から切り替える
    import rc_graphics
    td = base / "tpl_toggle"
    shutil.rmtree(td, ignore_errors=True)
    shutil.copytree(ROOT / "templates" / "default", td, ignore=shutil.ignore_patterns("bgm", "se", "generated"))
    before = (td / "template.toml").read_text(encoding="utf-8")
    ok = rc_graphics.set_enabled(td, "opening", True) and rc_graphics.set_enabled(td, "progress", True)
    after = (td / "template.toml").read_text(encoding="utf-8")
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    check("グラフィック: オープニング・章の進み具合を画面から有効にできる",
          ok and rc_graphics.get_enabled(td, "opening") and rc_graphics.get_enabled(td, "progress")
          and len(changed) == 2 and all("# true にすると" in b for _, b in changed), str(changed))
    rc_graphics.set_enabled(td, "opening", False)
    check("グラフィック: 元に戻せる（コメントもそのまま）", not rc_graphics.get_enabled(td, "opening"))

    # サムネイル候補の均等割り（早送りの区間は倍率をかけて元素材の時刻に戻す）
    import rc_thumb
    from rc_timeline import Piece

    class TL:
        fps = 10
        duration = 20.0
        pieces = [Piece(0, 0, 0, 0, 100, 0.0, 10.0), Piece(0, 0, 0, 100, 200, 10.0, 90.0)]   # 2つ目は ×8

        def map_time(self, t):
            return 0.0 if 0 <= t < 90 else None
    cands = rc_thumb.candidate_times({}, TL(), {}, {}, {})
    late = sorted(t for t, why in cands if why == "全体から" and t >= 10)
    check("サムネイル: 早送りの区間でも、完成の時刻で均等に選ぶ", late and late[-1] > 60, str(late))

    # 早送りのタイムラインでも、カメラの最短ショット長を完成の時刻で守る
    import rc_multicam as M
    from rc_speedup import _Part

    class STL:
        fps = 10
        duration = 30.0
        parts = [_Part(0.0, 10.0, 0, 100, 1.0, None, 0), _Part(10.0, 90.0, 100, 100, 8.0, 0, 0),
                 _Part(90.0, 100.0, 200, 100, 1.0, None, 0)]

        @property
        def pieces(self):
            return [Piece(p.file_index, 0, 0, p.o0, p.o0 + p.of, p.g0, p.g1) for p in self.parts]
    ed = M.to_edit(STL(), [(50.0, 74.0)])
    check("2台カメラ: 早送りのタイムラインの時刻（倍率を考える）", ed and abs(ed[0][0] - 15) < 1e-6
          and abs(ed[0][1] - 18) < 1e-6, str(ed))
    src = M.to_source(STL(), [(15.0, 18.0)])
    check("2台カメラ: 完成の時刻 → 元素材の時刻（倍率を考える）", src and abs(src[0][0] - 50) < 1e-6
          and abs(src[0][1] - 74) < 1e-6, str(src))

    class Cam:
        cover = (0.0, 100.0)
    shots = M.compute_shots(STL(), [{"start": 50.0, "end": 58.0, "cam": "cam2"}], Cam(), 3.0)
    ed_shots = M.to_edit(STL(), shots)
    check("2台カメラ: 早送りの中でも、完成で3秒以上は同じカメラ",
          ed_shots and all(e - s >= 3.0 - 1e-6 for s, e in ed_shots), str(ed_shots))


# ---------------------------------------------------------------- 7. 画面
GUI_CODE = r'''
import os, sys, json, time, shutil
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog
import gui
app = QApplication([])
res = {}
shown, asked = [], []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warning", a[2] if len(a) > 2 else "")))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[1] if len(a) > 1 else "")))
answer = {"v": QMessageBox.No}
def question(*a, **k):
    asked.append(a[1] if len(a) > 1 else "")
    return answer["v"]
QMessageBox.question = staticmethod(question)
A, B = Path(OUT_A), Path(OUT_B)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
rv = w.review
rv.load(A)
for _ in range(5):
    app.processEvents()

# --- 処理中は、別の結果を開けない（終わったときの続きが別の結果に向かないように） ---
w.runner.proc = object()
w.runner.busy_changed.emit(True)
shown.clear()
r = rv.load(B)
res["busy_refused"] = (r is False) and rv.out == A and any("処理中" in s for _, s in shown)
res["busy_other_disabled"] = not rv.b_other.isEnabled()
res["busy_speakers_disabled"] = not w.speaker_button.isEnabled()
res["busy_search_disabled"] = not w.search_tab.b_index.isEnabled()
res["busy_actions_disabled"] = all(not a.isEnabled() for a in w.graphics_actions + w.thumbspro_actions)
res["busy_sample_disabled"] = not w.enhance_row.sample.isEnabled()
w.search_tab.rows = [{"folder": str(B), "id": 1, "start": 0, "end": 1}]
w.search_tab.open_in_review(0)
res["busy_search_open_refused"] = rv.out == A
w.runner.proc = None
w.runner.busy_changed.emit(False)
res["idle_enabled"] = rv.b_other.isEnabled() and w.speaker_button.isEnabled() and w.search_tab.b_index.isEnabled()

# --- 演出プランの処理中に別の結果を開いても、終わったときの続きは元の結果に向く ---
dones = []
def fake_start(script, args, title, on_done=None, handle_error=None):
    dones.append((script, list(args), on_done))
    return True
w.runner.start = fake_start
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
rv.make_plan()
(B / "plan_edited.json").write_text(json.dumps({"telops": []}), encoding="utf-8")
(A / "plan_edited.json").write_text(json.dumps({"telops": []}), encoding="utf-8")
rv.load(B)
dones[-1][2](True)
res["plan_done_redirect"] = (B / "plan_edited.json").exists() and not (A / "plan_edited.json").exists() and rv.out == B
(B / "plan_edited.json").unlink()

# --- 表の手直しは、別の結果を開く・閉じるときに保存される ---
rv.load(A)
rv.t_telop.item(0, 2).setText("ドリルをレビュー！")
rv.t_telop.item(1, 0).setCheckState(Qt.Unchecked)
rv.load(B)
res["saved_on_switch"] = (A / "plan_edited.json").exists() and \
    json.loads((A / "plan_edited.json").read_text(encoding="utf-8"))["telops"][0]["text"] == "ドリルをレビュー！"
res["B_untouched"] = not (B / "plan_edited.json").exists()
rv.load(A)
res["reopen_text"] = rv.t_telop.item(0, 2).text()
res["reopen_checked"] = rv.t_telop.item(1, 0).checkState() == Qt.Checked
rv.load(B)
rv.t_telop.item(0, 2).setText("閉じる前の手直し")
w.close()
res["saved_on_close"] = (B / "plan_edited.json").exists() and \
    json.loads((B / "plan_edited.json").read_text(encoding="utf-8"))["telops"][0]["text"] == "閉じる前の手直し"

# --- Esc で閉じても保存の確認が出る（話者・テンプレート） ---
w = gui.MainWindow(); w.show(); rv = w.review; rv.load(A)
w.runner.start = fake_start
import gui_ext_speakers, gui_ext_design
dlg = gui_ext_speakers.SpeakerDialog(w)
dlg.show(); dlg.dirty = True
asked.clear(); answer["v"] = QMessageBox.No
QApplication.sendEvent(dlg, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
res["speaker_esc_asked"] = any("保存" in a for a in asked) and not dlg.isVisible()
ed = gui_ext_design.TemplateEditor(w, Path(w.create.template.currentData()), cfg={"subtitle": {}}, title="テスト")
ed.show()
asked.clear()
QMessageBox.question = staticmethod(lambda *a, **k: (asked.append("design"), QMessageBox.Cancel)[1])
QApplication.sendEvent(ed, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
res["design_esc_asked"] = asked == ["design"] and ed.isVisible()
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Discard)
ed.reject()
res["design_esc_discard_closes"] = not ed.isVisible()
QMessageBox.question = staticmethod(question)

# --- 「YouTubeへ」の翻訳: APIキーがないときは貼り付けで訳す ---
w.settings.setValue("api_key", "")
os.environ.pop("ANTHROPIC_API_KEY", None)
w.create.llm.setCurrentIndex(w.create.llm.findData("api"))
yd = gui.YouTubeDialog(rv)
yd.loc.setChecked(True)
dones.clear()
yd._run_yt([str(A), "--apply"], "設定を反映中", lambda ok: None)
first = dones[-1] if dones else ("", [], None)
res["yt_translate_manual"] = first[0] == "translate.py" and "--llm" in first[1] and \
    first[1][first[1].index("--llm") + 1] == "manual"
(A / "translate_pending.json").unlink(missing_ok=True)
first[2](True)
res["yt_then_apply"] = dones[-1][0] == "youtube.py" and "--localize" in dones[-1][1]
yd.close()

# --- ダークモードでも、色付きの行・まとめの文字が読める ---
import rc_theme
w.appearance.set_mode("dark")
tk = rc_theme.tokens()
cp = w.check_panel
cp.show_result({"counts": {"missing": 1, "points": 2, "partial": 0, "shots_missing": 1, "shots": 1},
                "method": "local", "created": "2026-11-05",
                "points": [{"id": "p1", "status": "missing", "must": True, "text": "重さを言う", "section_name": "本編",
                            "t": None, "note": ""},
                           {"id": "p2", "status": "said", "must": False, "text": "値段を言う", "section_name": "本編",
                            "t": 3.0, "note": "言っています"}],
                "shots": [{"id": "s1", "status": "missing", "must": False, "kind": "insert", "what": "手元の寄り",
                           "section_name": "本編", "t": None, "note": ""}]})
it = cp.table.item(0, 2)
res["dark_rows_fg"] = it.foreground().color().name().lower() == tk["text"].lower()
res["dark_rows_bg"] = it.background().color().name().lower() == tk["row_cut"].lower()
res["dark_legend"] = tk["row_cut"].lower() in cp.legend.text().lower()
an = getattr(w, "analytics", None)
res["dark_summary_role"] = an is None or an.ret_summary.property("role") == "summary"
cp.filter.setCurrentIndex(1)
rv.tabs.setCurrentWidget(cp)
w.tabs.setCurrentWidget(rv)
for _ in range(5):
    app.processEvents()
w.grab().save(SHOT_CHECK)
if an is not None:
    w.tabs.setCurrentWidget(an)
    for _ in range(5):
        app.processEvents()
    w.grab().save(SHOT_ANALYTICS)
w.appearance.set_mode("light")
res["light_rows_back"] = cp.table.item(0, 2).background().color().name().lower() == rc_theme.LIGHT["row_cut"].lower()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, fx: Path):
    if not (out / "session.json").exists():
        check("画面: テスト（先に smoke_test.py を実行してください）", False)
        return
    base = fx / "review_fixes"
    dirs = []
    for name in ("結果A", "結果 B"):
        d = base / name
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(out, d, ignore=shutil.ignore_patterns("work", "analytics", "shorts", "thumbnails"))
        shutil.copy(fx / "plan.json", d / "plan_used.json")
        (d / "plan_edited.json").unlink(missing_ok=True)
        dirs.append(d)
    shot1, shot2 = base / "gui_dark_check.png", base / "gui_dark_analytics.png"
    code = (f"ROOT = {str(ROOT)!r}\nOUT_A = {str(dirs[0])!r}\nOUT_B = {str(dirs[1])!r}\n"
            f"SHOT_CHECK = {str(shot1)!r}\nSHOT_ANALYTICS = {str(shot2)!r}\n" + GUI_CODE)
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("画面: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("画面: テスト", False, (p.stdout[-800:] + p.stderr[-1500:]))
        return
    r = json.loads(line[7:])
    check("画面: 処理中は別の結果を開けない（理由を知らせる）", r["busy_refused"])
    check("画面: 処理中は「別の結果を開く…」が灰色", r["busy_other_disabled"])
    check("画面: 処理中は拡張のボタン・メニューも灰色", r["busy_speakers_disabled"] and r["busy_search_disabled"]
          and r["busy_actions_disabled"] and r["busy_sample_disabled"], str(r))
    check("画面: 処理中は素材検索からも開けない", r["busy_search_open_refused"])
    check("画面: 終われば押せる", r["idle_enabled"])
    check("画面: 演出プランが終わったときは、始めた結果の手直しだけを消す", r["plan_done_redirect"])
    check("画面: 別の結果を開く前に、表の手直しを保存", r["saved_on_switch"] and r["B_untouched"])
    check("画面: 開き直しても手直しが残る", r["reopen_text"] == "ドリルをレビュー！" and not r["reopen_checked"],
          r["reopen_text"])
    check("画面: ツールを閉じるときも、表の手直しを保存", r["saved_on_close"])
    check("画面: 話者の画面は Esc でも保存の確認が出る", r["speaker_esc_asked"])
    check("画面: テンプレートの編集は Esc でも確認（キャンセルで閉じない）", r["design_esc_asked"]
          and r["design_esc_discard_closes"])
    check("画面: YouTubeの翻訳は、APIキーがなければ貼り付けで訳す", r["yt_translate_manual"])
    check("画面: 訳したあと、設定の反映に進む", r["yt_then_apply"])
    check("画面: ダークモードで色付きの行の文字が読める", r["dark_rows_fg"] and r["dark_rows_bg"] and r["dark_legend"])
    check("画面: ダークモードで分析のまとめの色をテーマに合わせる", r["dark_summary_role"])
    check("画面: ライトに戻すと行の色も戻る", r["light_rows_back"])
    check("画面: ダークモードの画像を保存", shot1.exists(), str(shot1))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    base = fx / "review_fixes"
    base.mkdir(parents=True, exist_ok=True)
    t_analytics_speedup(check, base)
    t_google(check, base)
    t_comments_replied(check)
    t_llm_fallback(check)
    t_opencv_paths(check, base)
    t_mv_collect(check, base)
    t_small(check, base)
    t_gui(check, out, fx)


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
