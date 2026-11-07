"""不具合の修正のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_fixes.py            # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 2台目カメラの同期（メインが長く、2台目が後半から撮り始めた場合）
  - チャプターが発言IDで保存され、カットを変えても・fpsを変えても同じ発言を指す
  - claude.ai の返答の行番号を、プロンプトを作ったときの行で読む
  - 出力フォルダを相対パスで渡しても書き出せる
  - youtube_meta.json を保存した後に完成動画を作り直すと、チャプターと動画IDが更新される
  - 動画が見つからないとき、候補の一覧を GUI 用に保存する
  - 書き出し後のサムネイル案で --llm の指定に従う
  - 演出の表の手直しが、読み込み直しで消えない（GUI）
  - 「中止」で孫プロセスまで止まる・中止はエラー扱いしない・エラーの理由を表示する（GUI）
  - BOM 付きの template.toml を読める
  - ドラッグ＆ドロップのファイルを名前の順に並べる、& を含む名前を読み取る
  - 屋外モードで反映しても「カット候補」の印が残る
  - 章カードを避けてずらしたテロップに合わせて効果音を鳴らす
  - サムネイル案の画面から、AIに指示して作り直す（thumbs.py --request。AIが「Claude APIで自動」のときだけ）（GUI）
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


def _run(args: list[str], cwd: Path = ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=e, timeout=900)


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


# ---------------------------------------------------------------- 同期
def t_sync(check):
    import numpy as np
    import rc_sync

    rng = np.random.default_rng(3)
    rate = 100   # エンベロープは10ms単位
    base = rng.normal(0, 1, rate * 60 * 80).astype(np.float32)   # メイン 80分

    def fake(env_a, env_b):
        it = iter([env_a, env_b])
        orig = rc_sync._envelope
        rc_sync._envelope = lambda _s: next(it)
        try:
            return rc_sync.find_offset(None, None)
        finally:
            rc_sync._envelope = orig

    # 2台目はメイン開始の50分後から7分だけ撮った → cam2時刻 = main時刻 - 3000秒
    s = 50 * 60 * rate
    cam2 = base[s:s + 7 * 60 * rate] + rng.normal(0, 0.3, 7 * 60 * rate).astype(np.float32)
    off, conf = fake(base, cam2)
    check("同期: メインが長く2台目が後半から", abs(off + 3000.0) < 0.05 and conf > 8, f"{off:+.2f}s 信頼度{conf:.1f}")

    # 逆: 2台目の方がずっと長く、メインが2台目開始の40分後から撮り始めた → offset=+2400秒
    long2 = rng.normal(0, 1, rate * 60 * 90).astype(np.float32)
    s = 40 * 60 * rate
    main = long2[s:s + 10 * 60 * rate].copy()
    off, conf = fake(main, long2)
    check("同期: 2台目が長くメインが後から", abs(off - 2400.0) < 0.05, f"{off:+.2f}s")

    # 短い素材で、2台目が先に撮り始めた場合（負のずれ）
    m = rng.normal(0, 1, rate * 60).astype(np.float32)
    c2 = np.concatenate([rng.normal(0, 1, rate * 5).astype(np.float32), m[: rate * 40]])
    off, _ = fake(m, c2)
    check("同期: 2台目が先に撮り始めた", abs(off - 5.0) < 0.05, f"{off:+.2f}s")


# ---------------------------------------------------------------- チャプター
def _copy_out(out: Path, dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copy(out / "transcript.json", dst / "transcript.json")
    return dst


def _rough(fx: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run(["roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--out", str(out), *extra])


def _session(out: Path) -> dict:
    return json.loads((out / "session.json").read_text(encoding="utf-8"))


def _expected_t(out: Path, cid: int, fps=None) -> float:
    """そのフォルダの session.json の残す区間から、発言 cid の編集後の開始時刻を求める"""
    import rc_chapters
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    from rc_transcribe import Chunk
    sess = _session(out)
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], fps or src.main.fps)
    by_id = {c["id"]: Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))}
    return round(rc_chapters.chapter_time(tl, by_id[cid]), 2)


def t_chapters(check, out: Path, fx: Path) -> Path | None:
    work = _copy_out(out, fx / "fixes" / "chapters")
    p = _rough(fx, work, "--mode", "talk", "--llm", "none", "--decisions", str(fx / "claude_cuts.json"),
               "--meta", str(fx / "claude_meta.json"))
    if p.returncode != 0:
        check("チャプター: 1回目の荒編集", False, _tail(p))
        return None
    s1 = _session(work)
    ids1 = [c.get("id") for c in s1["chapters"]]
    check("チャプター: 発言IDで保存", all(isinstance(i, int) for i in ids1) and len(ids1) == 3, str(s1["chapters"]))
    check("チャプター: meta にも発言ID", all("id" in c for c in s1["meta"]["chapters"]))

    # GUI の「カットの変更を反映」と同じ：前回の meta をそのまま渡し、カットを変えて作り直す
    (work / "meta_edited.json").write_text(json.dumps(s1["meta"], ensure_ascii=False), encoding="utf-8")
    d = json.loads((fx / "claude_cuts.json").read_text(encoding="utf-8"))
    d["manual"] = {"cut": [1, 2, 9], "keep": [4, 5, 7]}   # 残す発言をがらっと変える
    (work / "decisions_edited.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    p = _rough(fx, work, "--mode", "talk", "--llm", "none", "--decisions", str(work / "decisions_edited.json"),
               "--meta", str(work / "meta_edited.json"))
    if p.returncode != 0:
        check("チャプター: 2回目の荒編集", False, _tail(p))
        return None
    s2 = _session(work)
    ids2 = [c.get("id") for c in s2["chapters"]]
    check("チャプター: カットを変えても同じ発言を指す", ids2 == ids1, f"{ids1} → {ids2}")
    ok = all(abs(c["t"] - (0.0 if i == 0 else _expected_t(work, c["id"]))) < 0.02 for i, c in enumerate(s2["chapters"]))
    check("チャプター: 今のカットでの時刻", ok, str(s2["chapters"]))
    md = (work / "youtube.md").read_text(encoding="utf-8")
    check("チャプター: youtube.md も同じ時刻", all(c["title"] in md for c in s2["chapters"]))

    # claude.ai（manual）：1回目にプロンプト → 2回目に AI のカット＋返答（行番号）を渡す
    man = _copy_out(out, fx / "fixes" / "manual")
    p = _rough(fx, man, "--mode", "talk", "--llm", "manual")
    lines = json.loads((man / "meta_lines.json").read_text(encoding="utf-8"))["line_ids"] \
        if (man / "meta_lines.json").exists() else []
    check("manual: プロンプトの行と発言IDの対応を保存", p.returncode == 0 and len(lines) >= 3, str(lines))
    p = _rough(fx, man, "--mode", "talk", "--llm", "manual", "--decisions", str(fx / "claude_cuts.json"),
               "--meta", str(fx / "claude_meta.json"))
    if p.returncode == 0 and lines:
        meta = json.loads((fx / "claude_meta.json").read_text(encoding="utf-8"))
        want = [lines[c["line"] - 1] for c in meta["chapters"]]
        got = [c["id"] for c in _session(man)["chapters"]]
        check("manual: 返答の行番号はプロンプトを作ったときの行で読む", got == want, f"期待{want} 結果{got}")
    else:
        check("manual: 2回目の荒編集", False, _tail(p))
    return work


def t_retime_unit(check, out: Path):
    """出力 fps を素材と違う値にしても、チャプターは発言の位置に合う"""
    from fractions import Fraction

    import rc_chapters
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    from rc_transcribe import Chunk
    sess = _session(out)
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    by_id = {c["id"]: Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))}
    # 4秒ずつ残す区間を600個並べた長い素材と同じ状況を、短い素材の細かい区間で作る
    keep = [(t, t + 0.25) for t in [x * 0.4 for x in range(140)] if t + 0.25 < src.duration]
    tl30 = Timeline(src, keep, Fraction(30))
    tl_src = Timeline(src, keep, src.main.fps)
    chs = [{"t": 0.0, "title": "a", "id": 1}, {"t": 0.0, "title": "b", "id": 9}]
    chs[1]["t"] = round(rc_chapters.chapter_time(tl_src, by_id[9]), 2)
    r = rc_chapters.retime(chs, tl30, by_id)
    want = round(rc_chapters.chapter_time(tl30, by_id[9]), 2)
    check("チャプター: 出力fpsのタイムラインで時刻を求め直す", abs(r[1]["t"] - want) < 0.01 and r[0]["t"] == 0.0,
          f"{chs[1]['t']} → {r[1]['t']}（期待 {want}）")
    old = rc_chapters.retime([{"t": 0.0, "title": "x"}, {"t": 12.3, "title": "y"}], tl30, by_id)
    check("チャプター: IDのない古いチャプターはそのまま", old[1]["t"] == 12.3)


def t_finish_relative_fps(check, work: Path, fx: Path):
    """相対パスの出力フォルダ＋テンプレートの fps="30" で書き出す"""
    import re
    parent = fx / "fixes" / "rel"
    if parent.exists():
        shutil.rmtree(parent)
    parent.mkdir(parents=True)
    rel = parent / "rel test_roughcut"
    shutil.copytree(work, rel, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4"))
    tpl = parent / "tpl30"
    shutil.copytree(fx / "tpl", tpl)
    t = (tpl / "template.toml").read_text(encoding="utf-8")
    (tpl / "template.toml").write_text(re.sub(r'fps = "auto"', 'fps = "30"', t, count=1), encoding="utf-8")
    p = _run([str(ROOT / "finish.py"), "rel test_roughcut", "--template", str(tpl), "--plan", str(fx / "plan.json"),
              "--llm", "none", "--no-thumbs"], cwd=parent)
    ok = p.returncode == 0 and (rel / "C0001_完成.mp4").exists()
    check("書き出し: 出力フォルダが相対パス", ok, "" if ok else _tail(p))
    if not ok:
        return
    from fractions import Fraction
    fi = json.loads((rel / "final_info.json").read_text(encoding="utf-8"))
    sess = _session(rel)
    good = True
    for c, f in zip(sess["chapters"][1:], fi["chapters"][1:]):
        good &= abs(f["t"] - fi["intro_offset"] - _expected_t(rel, c["id"], Fraction(30))) < 0.02
    check("書き出し: fps違いでもチャプターは字幕と同じ時刻", good, str(fi["chapters"]))


# ---------------------------------------------------------------- YouTube
def t_youtube_meta(check, out: Path, fx: Path):
    import rc_youtube as Y
    yd = fx / "fixes" / "yt"
    if yd.exists():
        shutil.rmtree(yd)
    shutil.copytree(out, yd, ignore=shutil.ignore_patterns("work", "*.mp4", "thumbnails"))
    (yd / "youtube_meta.json").unlink(missing_ok=True)
    m = Y.default_meta(yd)
    m["title"] = "自分で直したタイトル"
    m["video_id"] = "OLDVIDEO001"
    m["description"] = "自分で書いた説明\n\n" + Y.chapter_block(m["chapters"]) + "\n\n#マキタ"
    Y.save_meta(yd, m)
    same = Y.default_meta(yd)
    check("YouTube: 作り直していなければ保存した内容のまま", same["title"] == m["title"] and same["video_id"] == "OLDVIDEO001")

    # 完成動画を作り直した（カットを変えてチャプターの時刻が変わった）
    fi = json.loads((yd / "final_info.json").read_text(encoding="utf-8"))
    fi["chapters"] = [{"t": 0.0, "title": "オープニング"}, {"t": 11.0, "title": "スペック"},
                      {"t": 21.0, "title": "実際に使ってみた"}]
    fi["duration"] = 40.0
    (yd / "final_info.json").write_text(json.dumps(fi, ensure_ascii=False), encoding="utf-8")
    new = Y.default_meta(yd)
    check("YouTube: タイトルなど人が直した欄は引き継ぐ", new["title"] == "自分で直したタイトル")
    check("YouTube: 概要欄のチャプターを今の動画に合わせる",
          "00:11 スペック" in new["description"] and "00:21 実際に使ってみた" in new["description"]
          and new["description"].startswith("自分で書いた説明") and new["description"].endswith("#マキタ"),
          new["description"].replace("\n", " / "))
    check("YouTube: 前の動画IDは使わない", new["video_id"] == "" and new.get("previous_video_id") == "OLDVIDEO001"
          and bool(new.get("notice")))
    Y.save_meta(yd, new)
    check("YouTube: 保存し直すと目印が今の動画になる", not Y.default_meta(yd).get("notice"))
    ai = Y.default_meta(yd, use_saved=False)
    check("YouTube: AIの案に戻す", ai["title"] != "自分で直したタイトル")
    return yd


def _fake_youtube(responses):
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence
    http = HttpMockSequence(responses)
    return build("youtube", "v3", http=http, static_discovery=True, developerKey="x")


def t_youtube_not_found(check, yd: Path):
    import rc_youtube as Y
    import youtube
    ch = json.dumps({"items": [{"id": "UC1", "snippet": {"title": "テストch"},
                                "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    pl = json.dumps({"items": [{"contentDetails": {"videoId": "AAA"}}, {"contentDetails": {"videoId": "BBB"}}]})
    vids = json.dumps({"items": [
        {"id": "AAA", "snippet": {"title": "タイトルを変えた動画"}, "status": {"privacyStatus": "private"}},
        {"id": "BBB", "snippet": {"title": "別の動画"}, "status": {"privacyStatus": "public"}}]})
    ok200 = {"status": "200"}
    svc = _fake_youtube([(ok200, ch), (ok200, ch), (ok200, pl), (ok200, vids)])
    orig, argv = Y.get_service, sys.argv
    Y.get_service = lambda interactive=True: svc
    sys.argv = ["youtube.py", str(yd), "--apply"]
    code = None
    try:
        youtube.main()
    except SystemExit as e:
        code = e.code
    finally:
        Y.get_service, sys.argv = orig, argv
    cand = yd / "youtube_candidates.json"
    ups = json.loads(cand.read_text(encoding="utf-8")) if cand.exists() else []
    check("YouTube: 見つからないとき候補の一覧を保存", bool(code) and [u["id"] for u in ups] == ["AAA", "BBB"],
          str(code)[:80])
    check("YouTube: 画面向けの案内", "一覧から動画を選ぶ" in str(code))

    # 動画IDを指定すれば、探さずにその動画へ設定する
    upd = json.dumps({"id": "AAA", "snippet": {"title": "x"}})
    svc = _fake_youtube([(ok200, ch), (ok200, upd)])
    Y.get_service = lambda interactive=True: svc
    m = Y.default_meta(yd)
    m["thumbnail"] = ""
    Y.save_meta(yd, m)
    sys.argv = ["youtube.py", str(yd), "--apply", "--video-id", "AAA"]
    try:
        youtube.main()
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        Y.get_service, sys.argv = orig, argv
    check("YouTube: --video-id の動画に設定して保存", code == 0 and Y.default_meta(yd)["video_id"] == "AAA", str(code))


# ---------------------------------------------------------------- 小さな部品
def t_units(check, fx: Path):
    # BOM 付き template.toml
    from rc_template import Template
    tdir = fx / "fixes" / "bom"
    if tdir.exists():
        shutil.rmtree(tdir)
    shutil.copytree(fx / "tpl", tdir)
    raw = (tdir / "template.toml").read_bytes()
    (tdir / "template.toml").write_bytes(b"\xef\xbb\xbf" + raw)
    try:
        ok = Template(tdir)["video"]["width"] == 1920
    except BaseException as e:  # noqa: BLE001
        ok = False
        print(e)
    check("テンプレート: BOM付きUTF-8でも読める", ok)

    # ドラッグ＆ドロップの並べ替えと、& を含む名前
    from rc_drop import drop_inputs, parse_cmdline, sort_inputs
    got = [p.name for p in sort_inputs([Path("C0002.MP4"), Path("C0001.MP4"), Path("C0010.MP4"), Path("C0003.MP4")])]
    check("ドロップ: 名前の自然な順", got == ["C0001.MP4", "C0002.MP4", "C0003.MP4", "C0010.MP4"], str(got))
    cl = r'C:\Windows\system32\cmd.exe /c ""C:\tools\AI 動画\roughcut_drop.bat" C:\撮影\Q&A.mp4 "D:\撮影 2\C0001.MP4""'
    check("ドロップ: & とスペースを含むパス", parse_cmdline(cl) == [r"C:\撮影\Q&A.mp4", r"D:\撮影 2\C0001.MP4"],
          str(parse_cmdline(cl)))
    files = drop_inputs([r"C:\撮影\Q"], cl, exists=lambda p: True)
    check("ドロップ: 切れた名前を元に戻して並べる", [str(p) for p in files] == [r"C:\撮影\Q&A.mp4", r"D:\撮影 2\C0001.MP4"]
          or [p.name for p in files] == ["C0001.MP4", "Q&A.mp4"], str(files))
    check("ドロップ: コマンドプロンプトから直接", parse_cmdline(r"C:\Windows\system32\cmd.exe") is None)

    # 章カードを避けてずらしたテロップと効果音
    import finish
    tel = [(10.0, 15.0, "あ", "emphasis", "pop.wav"), (30.0, 31.0, "い", "info", "ding.wav")]
    out = finish.avoid_chapter_cards(tel, [(9.9, "章"), (29.9, "章2")], 2.5)
    check("効果音: テロップと一緒にずれる・消えたテロップの音は残らない",
          len(out) == 1 and abs(out[0][0] - 12.5) < 1e-6 and out[0][4] == "pop.wav", str(out))

    # サムネイル案の AI 使用
    key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        a = finish.thumb_llm("api")
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        b = finish.thumb_llm("api")
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    check("サムネイル: --llm api ならAIで選ぶ（キーがなければ自動）", a == "none" and b == "api")

    # 作業フォルダが使用中で消せないとき
    d = fx / "fixes" / "busywork"
    d.mkdir(parents=True, exist_ok=True)
    orig_rm, orig_sleep = finish.shutil.rmtree, finish.time.sleep
    finish.shutil.rmtree = lambda *a, **k: (_ for _ in ()).throw(PermissionError(32, "使用中"))
    finish.time.sleep = lambda s: None
    try:
        finish.clear_work(d)
        msg = ""
    except SystemExit as e:
        msg = str(e.code)
    finally:
        finish.shutil.rmtree, finish.time.sleep = orig_rm, orig_sleep
    check("書き出し: 作業フォルダが使用中なら分かりやすく止める", "作業フォルダを消せませんでした" in msg)

    # Ctrl+C などで止まったとき、run_progress は FFmpeg を止める
    if os.name != "nt":
        import rc_render
        fake = fx / "fixes" / "fake_ffmpeg.sh"
        fake.write_text("#!/bin/sh\necho out_time_us=1000000\nexec sleep 60\n", encoding="utf-8")
        fake.chmod(0o755)
        procs = []
        orig_popen = rc_render.subprocess.Popen

        class P(orig_popen):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                procs.append(self)
        rc_render.subprocess.Popen = P
        import builtins
        orig_print = builtins.print

        def boom(*a, **k):
            raise KeyboardInterrupt
        rc_render.print = boom
        try:
            rc_render.run_progress([str(fake), "x"], 10.0, what="テスト")
        except KeyboardInterrupt:
            pass
        finally:
            rc_render.subprocess.Popen = orig_popen
            del rc_render.print
            builtins.print = orig_print
        check("書き出し: 途中で止まったら FFmpeg も止める", bool(procs) and procs[0].poll() is not None)


def t_outdoor_markers(check, out: Path, fx: Path):
    od = _copy_out(out, fx / "fixes" / "outdoor")
    d = json.loads((fx / "claude_cuts.json").read_text(encoding="utf-8"))
    chunks = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    d["manual"] = {"cut": [3], "keep": [c["id"] for c in chunks if c["id"] != 3]}   # GUI と同じ渡し方
    (od / "decisions_edited.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    p = _rough(fx, od, "--mode", "outdoor", "--llm", "none", "--decisions", str(od / "decisions_edited.json"))
    names = [m["name"] for m in _session(od)["markers"]] if p.returncode == 0 else []
    check("屋外モード: 反映してもカット候補の印が残る", sum(n.startswith("カット候補") for n in names) == 3,
          str(names) if p.returncode == 0 else _tail(p))


# ---------------------------------------------------------------- GUI（画面なしで別プロセス）
GUI_CODE = r'''
import os, sys, json, time, shutil
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
res = {}
shown = []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warning", a[2] if len(a) > 2 else "")))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[2] if len(a) > 2 else "")))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
w = gui.MainWindow(); w.resize(1300, 860); w.show(); w.create.llm.setCurrentIndex(w.create.llm.findData("api", 257))  # 「Claude APIで自動」
# 拡張（gui_ext_*.py）が項目を足したメニューだけ出す（何も足していないメニューは出さない）
res["menus_hidden"] = all(m.menuAction().isVisible() == bool(m.actions()) for m in (w.tools_menu, w.help_menu))
out = Path(OUT)
w.review.load(out)
rv = w.review
res["summary"] = rv.summary.text()

# --- 演出の表の手直しが、カットの反映（読み込み直し）で消えない ---
rv.tabs.setCurrentIndex(1)
rv.t_telop.item(0, 2).setText("手で直したテロップ")
rv.t_telop.item(1, 0).setCheckState(Qt.Unchecked)
rv.t_telop.cellWidget(0, 4).setCurrentIndex(rv.t_telop.cellWidget(0, 4).findData("whoosh"))
rv.b_apply.setText("カットの変更を反映 ●")
calls = []
real_start = w.runner.start
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    if on_done:
        QTimer.singleShot(0, lambda: on_done(True))   # 荒編集が成功したことにする
    return True
w.runner.start = fake_start
rv.render(preview=True)   # 反映 → 読み込み直し → 試し書き出し
for _ in range(20):
    app.processEvents(); time.sleep(0.02)
res["telop_text"] = rv.t_telop.item(0, 2).text()
res["telop_checked"] = [rv.t_telop.item(r, 0).checkState() == Qt.Checked for r in range(rv.t_telop.rowCount())]
res["telop_se"] = rv.t_telop.cellWidget(0, 4).currentData()
edited = json.loads((out / "plan_edited.json").read_text(encoding="utf-8"))
res["edited_texts"] = [t["text"] for t in edited["telops"]]
res["edited_off"] = [t["text"] for t in edited["_off"]["telops"]]
res["render_args"] = [a for s, a in calls if s == "finish.py"]
w.tabs.setCurrentWidget(rv)
rv.tabs.setCurrentIndex(1)
for _ in range(5):
    app.processEvents()
w.grab().save(str(out / "gui_fixes_plan.png"))
w.tabs.setCurrentWidget(w.create)
w.runner.busy_changed.emit(True)
w.grab().save(str(out / "gui_fixes_busy.png"))
w.runner.busy_changed.emit(False)

# --- 開き直しても手直しが残る ---
w2 = gui.MainWindow(); w2.review.load(out)
res["reopen_text"] = w2.review.t_telop.item(0, 2).text()
res["reopen_checked"] = [w2.review.t_telop.item(r, 0).checkState() == Qt.Checked for r in range(w2.review.t_telop.rowCount())]
w2.close()

# --- APIキーがないときは、AIを使う処理の前に知らせる ---
w.settings.setValue("api_key", "")
os.environ.pop("ANTHROPIC_API_KEY", None)
w.create.llm.setCurrentIndex(w.create.llm.findData("api", 257))
# 開き直した画面（w2）が「AI」の欄の初期値（サブスク）を設定に書くので、w の選択（Claude API）を設定に戻す
# （戻さないと、Claude Code が入っている PC では「サブスク」として確認を通ってしまう）
w.create._llm_changed()
calls.clear(); shown.clear()
rv.make_plan()
res["key_check"] = (not calls) and any("APIキー" in s for _, s in shown)
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
w.runner.start = real_start

def wait_idle(sec=20):
    t0 = time.time()
    while w.runner.busy and time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.05)

# --- 中止で孫プロセスまで止まる。中止はエラー扱いしない ---
pidfile = out / "child.pid"
pidfile.unlink(missing_ok=True)
script = out / "spawn_child.py"
script.write_text("import subprocess,sys,time\n"
                  "p=subprocess.Popen(['sleep','300'])\n"
                  f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
                  "print('■ 子プロセスを起動しました', flush=True)\n"
                  "time.sleep(300)\n", encoding="utf-8")
shown.clear()
w.runner.start(str(script), [], "テスト")
res["busy_disables"] = (not rv.b_plan.isEnabled()) and (not w.create.btn_rough.isEnabled())
t0 = time.time()
while not pidfile.exists() and time.time() - t0 < 20:
    app.processEvents(); time.sleep(0.05)
child = int(pidfile.read_text()) if pidfile.exists() else 0
w.runner.stop()
wait_idle()
time.sleep(0.5)
alive = False
if child:
    try:
        with open(f"/proc/{child}/stat") as f:
            alive = f.read().split()[2] != "Z"
    except FileNotFoundError:
        alive = False
res["child_killed"] = bool(child) and not alive
res["stop_no_error"] = not shown and w.runner.stage.text() == "中止しました"
res["buttons_back"] = rv.b_plan.isEnabled() and w.create.btn_rough.isEnabled()

# --- エラーの理由と、画面での直し方を表示する ---
err = out / "fail.py"
err.write_text("import sys\nprint('■ 準備')\n"
               "sys.exit('[エラー] ANTHROPIC_API_KEY が設定されていません。--llm manual なら API キーなしで使えます。')\n",
               encoding="utf-8")
shown.clear()
w.runner.start(str(err), [], "テスト2")
wait_idle()
msg = shown[-1][1] if shown else ""
res["err_dialog"] = "ANTHROPIC_API_KEY" in msg and "APIキーを設定" in msg
res["err_stage"] = w.runner.stage.text()

# --- UTF-8 の文字の途中で区切られても化けない ---
import codecs
dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
b = "■ 本編の書き出し".encode("utf-8")
res["utf8_split"] = (dec.decode(b[:2]) + dec.decode(b[2:])) == "■ 本編の書き出し"

# --- YouTube 画面：動画ID欄と「AIの案に戻す」 ---
dlg = gui.YouTubeDialog(rv)
res["yt_vid_field"] = hasattr(dlg, "vid")
dlg.resize(900, 820); dlg.show()
for _ in range(5):
    app.processEvents()
dlg.grab().save(str(out / "gui_fixes_youtube.png"))
dlg.reject()

# --- サムネイル画面の案内 ---
td = gui.ThumbDialog(rv, out / "thumbnails")
td.show()
for _ in range(5):
    app.processEvents()
td.grab().save(str(out / "gui_fixes_thumbs.png"))
labels = [c.text() for c in td.findChildren(gui.QLabel)]
res["thumb_text"] = any("YouTubeへ" in t for t in labels)
res["ask_off_without_api"] = not td.request.isEnabled() and not td.b_ask.isEnabled()
td.reject()

# --- サムネイル画面から、AIに指示して作り直す ---
w.create.llm.setCurrentIndex(w.create.llm.findData("api", 257))
(out / "thumbnails").mkdir(exist_ok=True)
(out / "thumbnails" / "requests.json").write_text(json.dumps(["文字を大きく"], ensure_ascii=False), encoding="utf-8")
td = gui.ThumbDialog(rv, out / "thumbnails")
asked = []
real_thumbs = rv.thumbnails
rv.thumbnails = lambda **k: asked.append(k)
td.request.setText("  案2は文字を左に ")
td.b_ask.click()
rv.thumbnails = real_thumbs
res["ask_made"] = any("反映ずみの指示: 文字を大きく" in c.text() for c in td.findChildren(gui.QLabel))
res["ask_call"] = asked
started = []
real_start2, real_key = w.runner.start, w.check_api_key
w.runner.start = lambda script, args, *a, **k: started.append([script] + list(args))
w.check_api_key = lambda *a, **k: True
rv.thumbnails(request="案2は文字を左に")
w.runner.start, w.check_api_key = real_start2, real_key
res["ask_args"] = started[-1] if started else []
(out / "thumbnails" / "requests.json").unlink()
w.create.llm.setCurrentIndex(w.create.llm.findData("none"))
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, fx: Path):
    gd = fx / "fixes" / "gui"
    if gd.exists():
        shutil.rmtree(gd)
    shutil.copytree(out, gd, ignore=shutil.ignore_patterns("work"))
    shutil.copy(fx / "plan.json", gd / "plan_used.json")
    (gd / "plan_edited.json").unlink(missing_ok=True)
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("GUI: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("GUI: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("GUI: 空のメニューは出さない", r["menus_hidden"])
    check("GUI: 要約に実際のファイル名", "C0001_荒編集.xml" in r["summary"] and "○○" not in r["summary"], r["summary"])
    check("GUI: 反映しても演出の手直しが残る（文字）", r["telop_text"] == "手で直したテロップ", r["telop_text"])
    check("GUI: 反映しても演出の手直しが残る（チェック）", r["telop_checked"][:2] == [True, False], str(r["telop_checked"]))
    check("GUI: 反映しても演出の手直しが残る（効果音）", r["telop_se"] == "whoosh", r["telop_se"])
    check("GUI: 書き出すプランに手直しが入る", r["edited_texts"][0] == "手で直したテロップ"
          and r["edited_off"] == ["重さ 1.2kg"], str(r["edited_texts"]) + str(r["edited_off"]))
    ra = r["render_args"][-1] if r["render_args"] else []
    check("GUI: 書き出しに --llm を渡す（サムネイル案用）", "--llm" in ra, str(ra))
    check("GUI: 開き直しても手直しが残る", r["reopen_text"] == "手で直したテロップ"
          and r["reopen_checked"][:2] == [True, False])
    check("GUI: APIキーの確認（確認・仕上げタブ）", r["key_check"])
    check("GUI: 処理中はボタンを押せない", r["busy_disables"] and r["buttons_back"])
    if os.name != "nt":
        check("GUI: 中止で孫プロセス（FFmpegなど）も止まる", r["child_killed"])
    check("GUI: 中止はエラー扱いしない", r["stop_no_error"])
    check("GUI: エラーの理由と画面での直し方を表示", r["err_dialog"] and "ANTHROPIC_API_KEY" in r["err_stage"],
          r["err_stage"])
    check("GUI: UTF-8の区切れ目で文字化けしない", r["utf8_split"])
    check("GUI: YouTube画面に動画ID欄", r["yt_vid_field"])
    check("GUI: サムネイル画面の案内", r["thumb_text"])
    check("GUI: サムネイル画面のAIへの指示は、AIが「Claude APIで自動」のときだけ使える", r["ask_off_without_api"])
    check("GUI: サムネイル画面から、AIに指示して作り直す（反映ずみの指示も表示）",
          r["ask_call"] == [{"request": "案2は文字を左に"}] and r["ask_made"], str(r["ask_call"]))
    a = r["ask_args"]
    check("GUI: 指示は thumbs.py --request で渡す", a[:1] == ["thumbs.py"] and "--request" in a
          and a[a.index("--request") + 1] == "案2は文字を左に" and "--text" not in a, str(a))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    (fx / "fixes").mkdir(exist_ok=True)
    t_sync(check)
    t_units(check, fx)
    if not (out / "session.json").exists():
        check("修正のテスト（荒編集の結果が必要）", False, "先に荒編集が成功している必要があります")
        return
    t_retime_unit(check, out)
    work = t_chapters(check, out, fx)
    if work:
        t_finish_relative_fps(check, work, fx)
    t_outdoor_markers(check, out, fx)
    if (out / "final_info.json").exists():
        yd = t_youtube_meta(check, out, fx)
        t_youtube_not_found(check, yd)
    t_gui(check, out, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    if not (out / "final_info.json").exists():
        print("（先に tests/smoke_test.py を実行すると、全部のテストができます）")
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
