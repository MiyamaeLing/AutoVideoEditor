"""企画・台本支援と、全動画の発言検索のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_preprod.py          # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 企画: Claude の返答（偽の通信）から plan.json / plan.md / plan.html を作る。AIなしのひな形・claude.ai の返答の取り込み
  - 撮り漏れチェック: 言葉の一致での照合（カットした発言だけで話した要点も見分ける）、Claude での照合（偽の通信・窓分け）
  - 発言検索: 日本語・空白入りのフォルダ、FTS5 と LIKE、ひらがな／カタカナ、カット済みの扱い、差分更新、消えたフォルダ
  - GUI: 「企画」「素材検索」タブと「③ 撮り漏れチェック」（画面なしモード。画像を tests/_fixtures/preprod/ に保存）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable

PLAN_REPLY = {
    "title": "マキタ18Vドリルを本音レビュー",
    "concept": "初めての電動ドリル選びで迷っている人に、軽さと使いやすさを実演で伝える",
    "audience": "DIY初心者",
    "minutes": 8,
    "sections": [
        {"name": "オープニング", "purpose": "何の動画か", "minutes": 0.5,
         "points": [{"text": "今日はマキタのドリルをレビューすると伝える", "keywords": ["ドリル", "レビュー"], "must": True}]},
        {"name": "スペック", "purpose": "数字で伝える", "minutes": 2,
         "points": [{"text": "重さ（1.2kg）を伝える", "keywords": ["重さ|おもさ", "キロ"], "must": True},
                    {"text": "値段とバッテリーの話", "keywords": ["値段|価格", "バッテリー"], "must": False},
                    {"text": "1.5キロのモデルとの違い", "keywords": ["1.5キロ"], "must": False}]},
        {"name": "実演", "purpose": "実際に使う", "minutes": 4,
         "points": [{"text": "実際に穴を開けてみる", "keywords": ["穴"], "must": True},
                    {"text": "仕上がりの感想", "keywords": ["きれい|綺麗"], "must": False}]},
        {"name": "まとめ", "purpose": "", "minutes": 1,
         "points": [{"text": "チャンネル登録のお願い", "keywords": ["チャンネル登録"], "must": False}]},
    ],
    "shots": [
        {"section": 1, "kind": "front", "what": "オープニングのあいさつ", "tip": "", "must": True},
        {"section": 3, "kind": "hands", "what": "穴を開ける手元", "tip": "スマホで真横から", "must": True},
        {"section": 2, "kind": "insert", "what": "ドリル本体のアップ", "tip": "回しながら", "must": False},
        {"section": 3, "kind": "broll", "what": "木くずが飛ぶスロー", "tip": "120fps", "must": False},
    ],
    "props": ["ドリル本体", "木の板", "三脚"],
    "title_ideas": ["軽すぎるドリル", "マキタ18V 本音レビュー"],
}


def _sse(text: str, stop: str = "end_turn") -> bytes:
    ev = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
          ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "text_delta", "text": text}}),
          ("content_block_stop", {"type": "content_block_stop", "index": 0}),
          ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 300}}),
          ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for e, d in ev).encode("utf-8")


class FakeClaude:
    """rc_llm._client を差し替えて、Claude API の代わりに決まった返答（SSE）を返す"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __enter__(self):
        import anthropic
        import httpx2
        import rc_llm

        def handler(req):
            self.requests.append(json.loads(req.content.decode("utf-8")))
            r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            body = r(self.requests[-1]) if callable(r) else r
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   content=_sse(json.dumps(body, ensure_ascii=False)))
        client = anthropic.Anthropic(api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self._orig = rc_llm._client
        rc_llm._client = lambda: (anthropic, client)
        return self

    def __exit__(self, *a):
        import rc_llm
        rc_llm._client = self._orig


# ---------------------------------------------------------------- 企画
def t_plan(check, base: Path) -> Path | None:
    import preprod
    import rc_preprod as P
    pdir = base / "projects"
    with FakeClaude([PLAN_REPLY]) as fc:
        try:
            preprod.main(["--projects-dir", str(pdir), "new", "マキタのドリル レビュー", "--genre", "talk",
                          "--minutes", "8", "--notes", "軽さを伝えたい", "--name", "ドリル: 本音/レビュー", "--llm", "api"])
        except SystemExit as e:
            check("企画: Claudeで作る（偽の通信）", False, str(e))
            return None
    d = pdir / P.safe_name("ドリル: 本音/レビュー")
    ok = (d / "plan.json").exists() and (d / "plan.md").exists() and (d / "plan.html").exists()
    check("企画: plan.json・plan.md・plan.html ができる（Windowsで使えない文字は置き換え）", ok and ":" not in d.name, d.name)
    if not ok:
        return None
    req = fc.requests[0]
    check("企画: 構造化出力・テーマとジャンルを送る",
          req["output_config"]["format"]["type"] == "json_schema" and "マキタのドリル" in json.dumps(req, ensure_ascii=False)
          and "レビュー・解説" in req["system"] and req["model"] == "claude-opus-5-5")
    data = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    ids = [p["id"] for s in data["sections"] for p in s["points"]]
    check("企画: 要点とカットに番号が付く", ids == ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]
          and [k["id"] for k in data["shots"]] == ["K1", "K2", "K3", "K4"], str(ids))
    md = (d / "plan.md").read_text(encoding="utf-8")
    htm = (d / "plan.html").read_text(encoding="utf-8")
    check("企画: 印刷用（Markdown）に構成・台本・撮影リスト", "## 構成と台本" in md and "- [ ] ★重さ（1.2kg）を伝える" in md
          and "| ☐ | 手元 | 実演 | ★穴を開ける手元 |" in md and "## 準備するもの" in md)
    check("企画: 印刷用（HTML）", "<!doctype html>" in htm and "@media print" in htm and "穴を開ける手元" in htm
          and "<script" not in htm)

    # AIなしのひな形・claude.ai の返答の取り込み
    for g in ("talk", "craft", "outdoor", "mv"):
        preprod.main(["--projects-dir", str(pdir), "new", f"ひな形 {g}", "--genre", g, "--llm", "none"])
    sk = json.loads((pdir / "ひな形 craft" / "plan.json").read_text(encoding="utf-8"))
    check("企画: AIなしでジャンルのひな形", sk["source"] == "none" and any(k["kind"] == "hands" for k in sk["shots"])
          and len(sk["sections"]) >= 4)
    try:
        preprod.main(["--projects-dir", str(pdir), "new", "ひな形 talk", "--llm", "none"])
        check("企画: 同じ名前は上書きしない", False)
    except SystemExit as e:
        check("企画: 同じ名前は上書きしない", "--overwrite" in str(e))
    preprod.main(["--projects-dir", str(pdir), "new", "手動の企画", "--genre", "outdoor", "--llm", "manual"])
    md_ = pdir / "手動の企画"
    ok_prompt = (md_ / "prompt_plan.md").exists() and "JSON Schema" in (md_ / "prompt_plan.md").read_text(encoding="utf-8")
    ans = md_ / "返答.txt"
    ans.write_text("はい、どうぞ。\n```json\n" + json.dumps(PLAN_REPLY, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    preprod.main(["--projects-dir", str(pdir), "new", "--name", "手動の企画", "--answer", str(ans)])
    m = json.loads((md_ / "plan.json").read_text(encoding="utf-8"))
    check("企画: claude.ai 用のプロンプトと返答の取り込み", ok_prompt and m["source"] == "manual"
          and m["title"] == PLAN_REPLY["title"] and m["genre"] == "outdoor")
    # 手で直した plan.json（番号なし・余計な値）でも読める
    raw = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    raw["sections"][0]["points"].append({"text": "手で足した要点"})
    raw["shots"].append({"what": "手で足したカット", "kind": "変な値", "section": 99})
    (d / "plan.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    preprod.main(["--projects-dir", str(pdir), "export", str(d)])
    _d, data2 = P.load_project(d)
    newp = data2["sections"][0]["points"][-1]
    news = data2["shots"][-1]
    check("企画: 手で直した plan.json も読める", newp["id"] == "P8" and news["id"] == "K5" and news["kind"] == "insert"
          and news["section"] == 0)
    return d


# ---------------------------------------------------------------- 撮り漏れチェック
def t_check(check, out: Path, fx: Path, pdir: Path) -> Path:
    import preprod
    import rc_preprod as P
    work = fx / "preprod" / "撮影 1_roughcut"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(out, work, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "preprod_*"))
    # 元に戻す（GUIのテストで「撮った」の印を付けるので）
    _d, data = P.load_project(pdir)
    for k in data["shots"]:
        k["done"] = False
    data["sections"][0]["points"] = data["sections"][0]["points"][:1]
    data["shots"] = data["shots"][:4]
    P.save_project(pdir, data)

    preprod.main(["check", str(work), "--project", str(pdir)])
    res = json.loads((work / "preprod_check.json").read_text(encoding="utf-8"))
    st = {p["text"]: p["status"] for p in res["points"]}
    check("撮り漏れ: 話した要点", st["今日はマキタのドリルをレビューすると伝える"] == "said"
          and st["実際に穴を開けてみる"] == "said" and st["仕上がりの感想"] == "said", str(st))
    check("撮り漏れ: 話していない要点", st["値段とバッテリーの話"] == "missing", st["値段とバッテリーの話"])
    check("撮り漏れ: カットした発言でしか話していない要点", st["1.5キロのモデルとの違い"] == "cut", st["1.5キロのモデルとの違い"])
    w = next(p for p in res["points"] if p["text"].startswith("重さ"))
    check("撮り漏れ: 言い直しの後の（残した）発言の時刻を出す", w["status"] == "said" and w["t"] == 18.0, f"{w['status']} {w['t']}")
    ss = {s["what"]: s["status"] for s in res["shots"]}
    check("撮り漏れ: カットの判定（2台目カメラ→手元・正面・発言だけでは決めない）",
          ss == {"オープニングのあいさつ": "likely", "穴を開ける手元": "likely", "ドリル本体のアップ": "missing",
                 "木くずが飛ぶスロー": "missing"}, str(ss))
    md = (work / "preprod_check.md").read_text(encoding="utf-8")
    check("撮り漏れ: 一覧（Markdown）", "## 話し忘れ・話し足りない要点" in md and "値段とバッテリーの話" in md
          and "木くずが飛ぶスロー" in md)
    check("撮り漏れ: 企画とのつながりを保存", P.linked_project(work) == pdir.resolve())
    preprod.main(["check", str(work)])   # 2回目は --project なしでよい
    check("撮り漏れ: 2回目は企画の指定なしで照合", (work / "preprod_check.json").exists())

    # Claude で照合（偽の通信）。長い素材は分けて送り、いちばん話せている判定を使う
    _d, data = P.load_project(pdir)
    chunks, cut_ids, _s = P.load_roughcut(work)

    def reply(req):
        text = req["messages"][0]["content"]
        said = "#6 " in text
        return {"points": [{"id": "P2", "status": "said" if said else "missing", "chunk_ids": [6] if said else [],
                            "note": "重さを話している"},
                           {"id": "P4", "status": "said", "chunk_ids": [4], "note": "カット済の発言で話している"},
                           {"id": "P99", "status": "said", "chunk_ids": [1], "note": "ない番号"}],
                "shots": [{"id": "K4", "status": "likely", "chunk_ids": [10, 999], "note": "すごいと言っている"}]}
    with FakeClaude([reply]) as fc:
        orig = P._windows
        P._windows = lambda ch, max_chars=80_000, context=10: orig(ch, max_chars=120, context=2)
        try:
            raw, usage = P.ai_check(data, chunks, cut_ids, "low")
        finally:
            P._windows = orig
    res2 = P.finalize_check(data, raw, chunks, cut_ids, "ai", pdir)
    p2 = next(p for p in res2["points"] if p["id"] == "P2")
    p4 = next(p for p in res2["points"] if p["id"] == "P4")
    k4 = next(s for s in res2["shots"] if s["id"] == "K4")
    first = fc.requests[0]["messages"][0]["content"]
    check("撮り漏れ(AI): 長い素材は分けて送る", len(fc.requests) >= 2 and usage["in"] == 1200 * len(fc.requests),
          f"{len(fc.requests)}回")
    check("撮り漏れ(AI): 台本と「(カット済)」の印を送る", "P1 [1. オープニング]" in first and "(カット済) #3" in first
          and fc.requests[0]["output_config"]["format"]["type"] == "json_schema")
    check("撮り漏れ(AI): 窓ごとの判定をまとめる", p2["status"] == "said" and p2["chunk_ids"] == [6], str(p2))
    check("撮り漏れ(AI): カット済の発言だけなら「カットした」", p4["status"] == "cut")
    check("撮り漏れ(AI): ない番号の発言は使わない", k4["status"] == "likely" and k4["chunk_ids"] == [10])
    # claude.ai の返答の取り込み
    ans = work / "返答.txt"
    ans.write_text("```json\n" + json.dumps(reply({"messages": [{"content": "#6 "}]}), ensure_ascii=False) + "\n```",
                   encoding="utf-8")
    preprod.main(["check", str(work), "--llm", "manual"])
    pr = (work / "prompt_preprod_check.md").read_text(encoding="utf-8")
    preprod.main(["check", str(work), "--answer", str(ans)])
    r3 = json.loads((work / "preprod_check.json").read_text(encoding="utf-8"))
    check("撮り漏れ: claude.ai のプロンプトと返答の取り込み", "## 文字起こし" in pr and "#12" in pr and r3["method"] == "ai")
    preprod.main(["check", str(work)])   # GUI のテスト用に、言葉の一致の結果に戻す
    return work


# ---------------------------------------------------------------- 発言検索
def _fake_roughcut(d: Path, texts: list[str], inputs: list[str], cut=(), corr=None, start=0.0, step=4.0):
    d.mkdir(parents=True, exist_ok=True)
    chunks = [{"id": i, "start": start + (i - 1) * step, "end": start + (i - 1) * step + step - 0.5, "text": t}
              for i, t in enumerate(texts, 1)]
    (d / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    (d / "session.json").write_text(json.dumps({"stem": d.name.replace("_roughcut", ""), "mode": "craft", "inputs": inputs,
                                                "cut_ids": list(cut), "corrections": corr or {},
                                                "meta": {"titles": ["本棚を作った"]}}, ensure_ascii=False),
                                    encoding="utf-8")


def t_search(check, out: Path, fx: Path) -> tuple[Path, Path]:
    import rc_search
    base = fx / "preprod" / "search"
    if base.exists():
        shutil.rmtree(base)
    root = base / "動画 フォルダ"
    a = root / "2026" / "ドリル レビュー_roughcut"
    shutil.copytree(out, a, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "preprod_*"))
    b = root / "工作" / "本棚_roughcut"
    _fake_roughcut(b, ["今日は本棚をつくります", "ＳＰＦ材とドリルを使います", "あ、間違えた", "ネジは３５ミリ",
                       "どりるで下穴をあけます", "完成しました"], [str(base / "なし.mp4")], cut=[3], corr={"4": "ネジは35ミリ"})
    (b / "work").mkdir()
    _fake_roughcut(b / "work" / "中_roughcut", ["ここは探さない"], [])     # 荒編集フォルダの中は探さない
    _fake_roughcut(root / ".hidden" / "x_roughcut", ["隠しフォルダは探さない"], [])
    db = base / "索引.sqlite"
    idx = rc_search.Index(db)
    st = idx.update(root)
    check("検索: 索引づくり（日本語・空白入りのフォルダ）", st["found"] == 2 and st["added"] == 2, str(st))
    check("検索: FTS5（trigram）が使える", idx.fts)
    r = idx.search("ドリル")
    folders = {Path(x["folder"]).name for x in r}
    check("検索: 動画をまたいで探す・カタカナとひらがなを区別しない",
          folders == {"ドリル レビュー_roughcut", "本棚_roughcut"} and any(x["text"].startswith("どりる") for x in r),
          str([x["text"] for x in r]))
    r = idx.search("spf")
    check("検索: 全角・半角と大文字・小文字を区別しない", len(r) == 1 and r[0]["spans"] == [(0, 3)], str(r[:1]))
    r = idx.search("重さ")
    check("検索: 2文字の言葉（LIKE）・カットした発言は除く", [x["id"] for x in r] == [6], str([x["id"] for x in r]))
    r = idx.search("重さ", include_cut=True)
    check("検索: カットした発言も含める", sorted(x["id"] for x in r) == [4, 6] and any(x["cut"] for x in r))
    r = idx.search("35ミリ")
    check("検索: 字幕の修正後の文で探す", len(r) == 1 and r[0]["text"] == "ネジは35ミリ")
    r = idx.search("ドリル レビュー")
    check("検索: 空白で区切ると両方を含む", len(r) == 1 and r[0]["id"] == 2 and r[0]["before"] == "どうもこんにちは。",
          str([x["text"] for x in r]))
    check("検索: 荒編集の中・隠しフォルダは探さない", not idx.search("探さない"))
    hit = idx.search("ドリルでした")[0]
    check("検索: 見つかった部分の位置", hit["text"][hit["spans"][0][0]:hit["spans"][0][1]] == "ドリルでした")
    check("検索: % や _ をそのまま探す", idx.search("%") == [] and idx.search("_") == [])
    # LIKE だけの環境（FTS5 なし）でも同じ結果
    fts = [x["id"] for x in idx.search("ドリル", include_cut=True)]
    idx.fts = False
    like = [x["id"] for x in idx.search("ドリル", include_cut=True)]
    idx.fts = True
    check("検索: FTS5 がなくても LIKE で同じ結果", fts == like and fts, f"{fts} {like}")
    # 再生する場所（分割ファイル）
    a_hit = next(x for x in idx.search("きれいに"))
    path, local = rc_search.locate(a_hit["inputs"], a_hit["start"])
    check("検索: 分割ファイルのどこを再生するか", Path(path).name == "C0002.MP4" and abs(local - 10.0) < 0.1,
          f"{path} {local:.2f}")
    # 差分更新・消えたフォルダ
    st2 = idx.update(root)
    check("検索: 変わっていない動画は読み直さない", st2["same"] == 2 and st2["added"] == st2["updated"] == 0, str(st2))
    time.sleep(0.01)
    _fake_roughcut(b, ["今日は本棚をつくります", "インパクトドライバーを使います"], [])
    shutil.rmtree(a)
    st3 = idx.update(root)
    check("検索: 変わった動画は読み直し、消えた動画は外す", st3["updated"] == 1 and st3["removed"] == 1
          and idx.stats()["videos"] == 1 and len(idx.search("ドライバー")) == 1 and not idx.search("ＳＰＦ"), str(st3))
    idx.close()
    shutil.copytree(out, a, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "preprod_*"))

    # 3時間の素材を何本も（メモリに全部載せない・速さ）
    big = base / "大きい"
    for n in range(3):
        texts = [f"これは{n}本目の{i}番目の発言で、ドリルや丸ノコ{i % 97}の話をしています" for i in range(1, 4001)]
        _fake_roughcut(big / f"長尺{n}_roughcut", texts, [], step=2.7)
    idx = rc_search.Index(base / "大きい.sqlite")
    t0 = time.time()
    idx.update(big)
    t1 = time.time()
    r = idx.search("丸ノコ96", limit=500)
    t2 = time.time()
    idx.close()
    check("検索: 3時間×3本の索引と検索", len(r) == 3 * (4000 // 97) and t1 - t0 < 30 and t2 - t1 < 2,
          f"{len(r)}件 索引{t1 - t0:.1f}秒 検索{(t2 - t1) * 1000:.0f}ms")

    # コマンド
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    p1 = subprocess.run([PY, "search.py", "--db", str(db), "index", str(root)], cwd=ROOT, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=e)
    p2 = subprocess.run([PY, "search.py", "--db", str(db), "find", "どりる", "--json"], cwd=ROOT, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", env=e)
    try:
        rows = json.loads(p2.stdout)
    except json.JSONDecodeError:
        rows = []
    check("検索: コマンド（search.py index / find）", p1.returncode == 0 and "見つかった荒編集: 2本" in p1.stdout
          and "(100%)" in p1.stdout and len(rows) >= 2, (p1.stdout + p1.stderr)[-300:])
    return root, db


# ---------------------------------------------------------------- GUI
GUI_CODE = r"""
import json, os, sys, time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
import rc_preprod as P
res = {}
app = QApplication([])
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: res.setdefault("warn", str(a[2]) if len(a) > 2 else ""))
w = gui.MainWindow(); w.resize(1300, 860); w.show()

def wait():
    t = time.time()
    while w.runner.busy and time.time() - t < 120:
        app.processEvents(); time.sleep(0.02)
    for _ in range(5):
        app.processEvents()

res["tabs"] = [w.tabs.tabText(i) for i in range(w.tabs.count())]
res["review_tabs"] = [w.review.tabs.tabText(i) for i in range(w.review.tabs.count())]
# 拡張（gui_ext_*.py）が項目を足したメニューだけ出す（何もないメニューは出さない）
res["menus_hidden"] = all(m.menuAction().isVisible() == bool(m.actions()) for m in (w.tools_menu, w.help_menu))

# ---- 企画タブ ----
pt = w.plan_tab
w.tabs.setCurrentWidget(pt)
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
pt.theme.setText("GUIで作る企画")
pt.genre.setCurrentIndex(pt.genre.findData("craft"))
pt.minutes.setValue(12)
pt.generate(); wait()
res["gen"] = pt.pdir is not None and pt.pdir.name == "GUIで作る企画" and pt.t_pts.rowCount() > 0
# 表を直して保存（見出しの名前を変える・要点を足す・カットを足す）
pt.t_pts.item(0, 0).setText("つかみ")
pt.t_pts.item(0, 2).setText("完成品をいきなり見せる")
pt.t_pts.item(0, 3).setText("完成|かんせい、見て")
pt.t_pts.setCurrentCell(0, 2)
pt.add_row()
pt.t_pts.item(1, 2).setText("足した要点")
pt.inner.setCurrentWidget(pt.t_shots)
pt.add_row()
r = pt.t_shots.rowCount() - 1
pt.t_shots.item(r, 4).setText("足したカット")
pt.t_shots.cellWidget(r, 2).setCurrentIndex(pt.t_shots.cellWidget(r, 2).findData("broll"))
pt.t_shots.cellWidget(r, 3).setCurrentIndex(2)
res["dirty"] = pt.b_save.text().endswith("●")
pt.save()
d = json.loads((pt.pdir / "plan.json").read_text(encoding="utf-8"))
res["sec0"] = d["sections"][0]["name"]
res["sec0_pts"] = [p["text"] for p in d["sections"][0]["points"]]
res["kw"] = d["sections"][0]["points"][0]["keywords"]
res["new_shot"] = [d["shots"][-1]["what"], d["shots"][-1]["kind"], d["shots"][-1]["section"]]
res["n_secs"] = len(d["sections"])
res["md_updated"] = "完成品をいきなり見せる" in (pt.pdir / "plan.md").read_text(encoding="utf-8")
pt.inner.setCurrentIndex(0)
app.processEvents()
w.grab().save(SHOTS + "/gui_plan.png")
pt.open_project(Path(PDIR))
pt.inner.setCurrentIndex(1)
for _ in range(5):
    app.processEvents()
w.grab().save(SHOTS + "/gui_plan_shots.png")

# ---- 撮り漏れチェック ----
rv = w.review
rv.load(Path(WORK))
w.tabs.setCurrentWidget(rv)
cp = w.check_panel
rv.tabs.setCurrentWidget(cp)
res["linked"] = cp.project.currentData() and Path(cp.project.currentData()).resolve() == Path(PDIR).resolve()
res["check_rows"] = cp.table.rowCount()
cp.run(ai=False); wait()
res["check_rows2"] = cp.table.rowCount()
res["summary"] = cp.summary.text()
cp.filter.setCurrentIndex(1)
w.grab().save(SHOTS + "/gui_check.png")
played = []
rv.play_range = lambda s, e: played.append((s, e))
row = next(r for r in range(cp.table.rowCount()) if "穴を開けてみる" in cp.table.item(r, 2).text())
cp._clicked(row, 2)
res["played"] = played
shot_row = next(r for r in range(cp.table.rowCount()) if "木くず" in cp.table.item(r, 2).text())
cp.table.item(shot_row, 0).setCheckState(Qt.Checked)
_d, pd = P.load_project(Path(PDIR))
res["shot_done"] = next(k["done"] for k in pd["shots"] if k["what"] == "木くずが飛ぶスロー")
cj = json.loads((Path(WORK) / "preprod_check.json").read_text(encoding="utf-8"))
res["check_saved"] = next(s["status"] for s in cj["shots"] if s["what"] == "木くずが飛ぶスロー")
shot_row = next(r for r in range(cp.table.rowCount()) if "木くず" in cp.table.item(r, 2).text())
cp.table.item(shot_row, 0).setCheckState(Qt.Unchecked)
cj = json.loads((Path(WORK) / "preprod_check.json").read_text(encoding="utf-8"))
res["check_unsaved"] = next(s["status"] for s in cj["shots"] if s["what"] == "木くずが飛ぶスロー")

# ---- 素材検索 ----
del rv.play_range
st = w.search_tab
w.tabs.setCurrentWidget(st)
st.root.setText(SEARCH_ROOT)
st.update_index(); wait()
st.query.setText("ドリル")
st.search()
res["search_rows"] = len(st.rows)
res["search_info"] = st.info.text()
r0 = next(i for i, x in enumerate(st.rows) if "レビュー" in x["text"])
st.table.selectRow(r0)
st.play(r0)
res["now"] = st.now.text()
app.processEvents()
w.grab().save(SHOTS + "/gui_search.png")
st.open_in_review(r0)
res["opened"] = str(rv.out) == st.rows[r0]["folder"] and w.tabs.currentWidget() is rv
res["sel_id"] = rv.chunks[rv.table.currentRow()]["id"] if rv.table.currentRow() >= 0 else None
res["want_id"] = st.rows[r0]["id"]
st.query.setText("見つからない言葉")
st.search()
res["none_info"] = st.info.text()
print("RESULT " + json.dumps(res, ensure_ascii=False, default=str))
"""


def t_gui(check, fx: Path, pdir: Path, work: Path, root: Path, db: Path):
    shots = fx / "preprod"
    pbase = pdir.parent
    code = (f"ROOT = {str(ROOT)!r}\nPDIR = {str(pdir)!r}\nWORK = {str(work)!r}\nSEARCH_ROOT = {str(root)!r}\n"
            f"SHOTS = {str(shots)!r}\n" + GUI_CODE)
    env = dict(os.environ, APPDATA=str(fx / "appdata_preprod"), XDG_CONFIG_HOME=str(fx / "appdata_preprod"),
               PYTHONIOENCODING="utf-8", AVE_PROJECTS_DIR=str(pbase), AVE_SEARCH_DB=str(fx / "preprod" / "gui.sqlite"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=600)
    except subprocess.TimeoutExpired:
        check("GUI(企画・検索): テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("GUI(企画・検索): テスト", False, (p.stdout + p.stderr)[-2000:])
        return
    r = json.loads(line[7:])
    check("GUI: 「企画」「素材検索」タブ", {"企画", "素材検索"} <= set(r["tabs"]) and r["tabs"][:2] == ["作成", "確認・仕上げ"],
          str(r["tabs"]))
    check("GUI: 確認タブに「③ 撮り漏れチェック」", "③ 撮り漏れチェック" in r["review_tabs"])
    check("GUI: メニューは増やさない", r["menus_hidden"])
    check("GUI(企画): ボタンで企画を作る", r["gen"])
    check("GUI(企画): 表で直して保存", r["dirty"] and r["sec0"] == "つかみ"
          and r["sec0_pts"] == ["完成品をいきなり見せる", "足した要点"] and r["kw"] == ["完成|かんせい", "見て"]
          and r["md_updated"], f"{r['sec0']} {r['sec0_pts']} {r['kw']}")
    check("GUI(企画): カットを足す（種類・場面）", r["new_shot"][:2] == ["足したカット", "broll"] and r["new_shot"][2] == 3,
          str(r["new_shot"]))
    check("GUI(撮り漏れ): 前回の企画と結果を表示", r["linked"] and r["check_rows"] > 0)
    check("GUI(撮り漏れ): 照合する", r["check_rows2"] == 11 and "話していない要点" in r["summary"], r["summary"])
    check("GUI(撮り漏れ): クリックで場面を再生", r["played"] and abs(r["played"][0][0] - 26.0) < 0.01, str(r["played"]))
    check("GUI(撮り漏れ): 「撮った」の印を企画と結果に記録", r["shot_done"] is True and r["check_saved"] == "done"
          and r["check_unsaved"] == "missing", f"{r['shot_done']} {r['check_saved']} {r['check_unsaved']}")
    check("GUI(検索): 索引を作って検索", r["search_rows"] >= 2 and "件" in r["search_info"], r["search_info"])
    check("GUI(検索): クリックでその場面", "00:03" in r["now"] and "見つかりません" not in r["now"], r["now"])
    check("GUI(検索): 確認タブでその発言を開く", r["opened"] and r["sel_id"] == r["want_id"], f"{r['sel_id']} {r['want_id']}")
    check("GUI(検索): 見つからないときの案内", "見つかりませんでした" in r["none_info"], r["none_info"])


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    base = fx / "preprod"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    pdir = t_plan(check, base)
    root = db = None
    try:
        root, db = t_search(check, out, fx)
    except Exception:  # noqa: BLE001
        import traceback
        check("検索: テスト", False, traceback.format_exc()[-1500:])
    if not pdir:
        return
    if not (out / "session.json").exists():
        check("撮り漏れ（荒編集の結果が必要）", False, "先に荒編集が成功している必要があります")
        return
    work = t_check(check, out, fx, pdir)
    if root:
        t_gui(check, fx, pdir, work, root, db)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    if not (out / "session.json").exists():
        print("（先に tests/smoke_test.py を実行すると、全部のテストができます）")
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
