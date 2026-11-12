"""途中からの再開・まとめて処理・困ったときの情報・自動更新のテスト（GPU・APIキーなしで動く）。

    python tests/test_ops.py              # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 文字起こしを区切りごとに途中保存し、止まっても続きから再開する（Whisper は偽物に差し替え）
  - 区切って文字起こししても、まとめて文字起こししたときと同じ結果になる
  - 仕上げの本編書き出しで、できているパートを使い回す（壊れたパートは作り直す）
  - まとめて処理：順番に処理・エラーでも次へ進む・続きから再開・二重起動しない・結果の一覧
  - 困ったときの情報：環境が入っている・APIキーやユーザー名が伏せてある・zip ができる
  - 自動更新：自分で直したテンプレート・.venv・自分の拡張を残す・バックアップから戻せる・git のフォルダは触らない
  - 画面：ヘルプのメニュー・まとめて処理のタブ・新しい版のお知らせ（画面なしで動かして、画像も保存）
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable


def _env(fx: Path, **extra) -> dict:
    e = dict(os.environ, PYTHONIOENCODING="utf-8", APPDATA=str(fx / "ops" / "appdata"),
             XDG_CONFIG_HOME=str(fx / "ops" / "appdata"), AVE_SETTINGS_DIR=str(fx / "ops" / "appdata"), AVE_UPDATE_CHECK="0")
    e.pop("ANTHROPIC_API_KEY", None)
    e.update(extra)
    return e


def _run(args: list[str], fx: Path, cwd: Path = ROOT, timeout: int = 900, **env) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=_env(fx, **env), timeout=timeout)


def _tail(p: subprocess.CompletedProcess, n: int = 15) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- 文字起こしの再開
class _W:
    def __init__(self, s, e, w):
        self.start, self.end, self.word = s, e, w


class _S:
    def __init__(self, s, e, text):
        self.start, self.end, self.text = s, e, text
        self.words = [_W(s, e, text)]
        self.no_speech_prob, self.avg_logprob, self.compression_ratio = 0.1, -0.2, 1.2


class FakeWhisper:
    """音のかたまりを1つの発言として返す偽の Whisper。fail_at 回目の呼び出しで止まる"""

    def __init__(self, fail_at: int | None = None):
        self.calls = 0
        self.fail_at = fail_at
        self.lengths = []

    def transcribe(self, audio, **_kw):
        import numpy as np
        self.calls += 1
        self.lengths.append(len(audio) / 16000)
        if self.fail_at and self.calls >= self.fail_at:
            raise KeyboardInterrupt("テスト用の停止（電源が落ちた想定）")
        n = 320
        fr = len(audio) // n
        rms = np.sqrt((audio[:fr * n].reshape(fr, n) ** 2).mean(axis=1)) if fr else np.zeros(0)
        on = rms > 0.02
        segs, i = [], 0
        while i < fr:
            if on[i]:
                j = i
                while j < fr and (on[j] or (j + 8 < fr and on[j:j + 8].any())):
                    j += 1
                s, e = i * n / 16000, j * n / 16000
                segs.append(_S(round(s, 3), round(e, 3), f"音{len(segs)}"))
                i = j
            else:
                i += 1
        return iter(segs), None


def _transcribe_with(fake, src, cache, window, overlap=3.0):
    import rc_transcribe as T
    orig = (T.load_model, T.WINDOW_SEC, T.OVERLAP_SEC)
    T.load_model = lambda name, dev: (fake, "cpu")
    T.WINDOW_SEC, T.OVERLAP_SEC = window, overlap
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            segs = T.transcribe(src, cache, "fake-model", "cpu", [], 8)
        return segs, buf.getvalue()
    finally:
        T.load_model, T.WINDOW_SEC, T.OVERLAP_SEC = orig


def t_transcribe_resume(check, fx: Path):
    import rc_transcribe as T
    from rc_media import SourceSet, probe
    d = fx / "ops" / "文字起こし 再開"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    src = SourceSet([probe(fx / "C0001.MP4"), probe(fx / "C0002.MP4")])

    whole, _ = _transcribe_with(FakeWhisper(), src, d / "whole.json", 1000.0)
    full_fake = FakeWhisper()
    win, log = _transcribe_with(full_fake, src, d / "win.json", 8.0)
    pairs = list(zip(whole, win))
    same = len(whole) == len(win) and all(abs(a["start"] - b["start"]) < 0.05 and abs(a["end"] - b["end"]) < 0.05
                                          for a, b in pairs)
    check("再開: 区切って文字起こししても結果が同じ", same and len(whole) > 10,
          f"まとめて{len(whole)} / 区切り{len(win)}")
    check("再開: 一度に読む音声は区切りの長さだけ（メモリ節約）", max(full_fake.lengths) <= 8.0 + 3.0 + 0.05,
          f"最大 {max(full_fake.lengths):.1f}秒")
    check("再開: 区切りの境目の発言が2回出ない", len({round(s['start'], 1) for s in win}) == len(win))

    # 4回目の区切りで止まる → 途中保存が残る
    cache = d / "transcript.json"
    try:
        _transcribe_with(FakeWhisper(fail_at=4), src, cache, 8.0)
        stopped = False
    except KeyboardInterrupt:
        stopped = True
    part = T.partial_path(cache)
    nlines = len(part.read_text(encoding="utf-8").splitlines()) if part.exists() else 0
    check("再開: 止まったときに途中保存が残る", stopped and nlines == 3 and not cache.exists(), f"{nlines}行")
    # 書きかけの壊れた行があっても読める
    with open(part, "a", encoding="utf-8") as fp:
        fp.write('{"fkey": "abc", "from": 0, "to": 5, "seg')
    fake2 = FakeWhisper()
    res, log = _transcribe_with(fake2, src, cache, 8.0)
    same2 = len(res) == len(win) and all(abs(a["start"] - b["start"]) < 1e-6 for a, b in zip(res, win))
    check("再開: 続きから再開して、止まらなかったときと同じ結果", same2 and "途中から再開" in log,
          f"{len(res)} / {len(win)}")
    check("再開: 終わっている区切りはやり直さない", fake2.calls == full_fake.calls - 3,
          f"再開後 {fake2.calls}回 / 全部 {full_fake.calls}回")
    check("再開: 完了したら transcript.json を作り、途中保存は消す",
          cache.exists() and not part.exists() and json.loads(cache.read_text(encoding="utf-8"))["segments"] == res)

    # 1本目が終わった所で止まった → 1本目は使い回し、2本目だけ
    cache3 = d / "t3.json"
    n_first = None
    fk = FakeWhisper()
    _transcribe_with(fk, SourceSet([src.files[0]]), d / "count.json", 8.0)
    n_first = fk.calls
    try:
        _transcribe_with(FakeWhisper(fail_at=n_first + 1), src, cache3, 8.0)
    except KeyboardInterrupt:
        pass
    fake3 = FakeWhisper()
    res3, log3 = _transcribe_with(fake3, src, cache3, 8.0)
    check("再開: ファイルごとの途中保存（終わったファイルは使い回す）",
          "C0001.MP4 は途中保存した結果を使います" in log3 and fake3.calls == full_fake.calls - n_first
          and len(res3) == len(win), f"{fake3.calls}回")
    # roughcut.py --retranscribe は途中保存も消す
    T.partial_path(cache3).write_text("x\n", encoding="utf-8")
    T.clear_partial(cache3)
    check("再開: やり直しのときは途中保存を消す", not T.partial_path(cache3).exists())

    # 音声の読み込み（素材の途中から）が正しい時刻か
    import numpy as np
    a = T.read_audio(fx / "C0001.MP4", 10.0, 5.0)
    whole_a = T.read_audio(fx / "C0001.MP4", 0.0, 30.0)
    ref = whole_a[160000:160000 + len(a)]
    corr = float(np.corrcoef(a[:70000], ref[:70000])[0, 1]) if len(a) > 70000 else 0
    check("再開: 途中から読んだ音声の時刻が合う", abs(len(a) / 16000 - 5.0) < 0.05 and corr > 0.95, f"相関 {corr:.3f}")


# ---------------------------------------------------------------- 書き出し済みパーツの使い回し
def t_render_parts(check, fx: Path):
    import finish
    import rc_ops
    import rc_render
    d = fx / "ops" / "パーツ 使い回し"
    if d.exists():
        shutil.rmtree(d)
    work = d / "work"
    work.mkdir(parents=True)
    fps = Fraction(30000, 1001)
    segs = [rc_render.Seg(fx / "C0001.MP4", t, 30) for t in (0, 2, 4, 6, 8)]
    _, inter, _ = rc_render.pick_encoder("libx264", 23)
    orig = rc_render.BATCH
    rc_render.BATCH = 2
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            body = rc_render.render_body(segs, 320, 180, fps, work, inter)
        parts = sorted(work.glob("part_*.mkv"))
        check("パーツ: 書き終わりの印", all(Path(str(p) + ".done").exists() for p in parts))
        check("パーツ: 書き出したパーツに中身の印が付く", len(parts) == 3 and all(rc_ops.PART_RE.match(p.name) for p in parts),
              str([p.name for p in parts]))
        m0 = parts[0].stat().st_mtime_ns
        # 止まった想定：2つ目は書きかけ（tmp_）、3つ目は途中で切れたファイル
        os.replace(parts[1], work / f"tmp_{parts[1].name}")
        parts[2].write_bytes(parts[2].read_bytes()[:2000])
        (work / "mix.wav").write_bytes(b"x")
        finish.clear_work(work)
        left = sorted(p.name for p in work.iterdir())
        check("パーツ: 作業フォルダを片付けても、できたパーツは残す",
              left == sorted([parts[0].name, parts[0].name + ".done", parts[2].name, parts[2].name + ".done"]),
              str(left))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            body = rc_render.render_body(segs, 320, 180, fps, work, inter)
        log = buf.getvalue()
        reused = log.count("前回書き出した分を使います")
        check("パーツ: できているパートは使い回し、書きかけ・壊れたものは作り直す",
              reused == 1 and "本編の書き出し 1/3（前回" in log and parts[0].stat().st_mtime_ns == m0, log.strip())
        dur = _duration(body)
        check("パーツ: つないだ本編の長さが正しい", abs(dur - 150 / 30000 * 1001) < 0.1, f"{dur:.3f}s")
        # カットが変わったら、そのパートだけ作り直し、使わなくなったパーツは消す
        segs2 = segs[:2] + [rc_render.Seg(fx / "C0001.MP4", 12, 30)] + segs[3:]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_render.render_body(segs2, 320, 180, fps, work, inter)
        log = buf.getvalue()
        n_parts = len(list(work.glob("part_*.mkv")))
        check("パーツ: 変わったパートだけ作り直す", log.count("前回書き出した分を使います") == 2 and n_parts == 3,
              f"{log.strip()} / {n_parts}個")
    finally:
        rc_render.BATCH = orig


def t_finish_resume(check, out: Path, fx: Path):
    d = fx / "ops" / "仕上げ 再開"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(out, d, ignore=shutil.ignore_patterns("work", "*.mp4", "thumbnails"))
    args = ["finish.py", str(d), "--template", str(fx / "tpl"), "--plan", str(fx / "plan.json"), "--llm", "none",
            "--no-thumbs", "--keep-work"]
    p1 = _run(args, fx)
    p2 = _run(args, fx) if p1.returncode == 0 else p1
    mp4 = d / "C0001_完成.mp4"
    check("仕上げ: 2回目は書き出し済みの本編パーツを使い回す",
          p2.returncode == 0 and "前回書き出した分を使います" in p2.stdout and _duration(mp4) > 5, _tail(p2, 8))


# ---------------------------------------------------------------- まとめて処理
def t_queue(check, fx: Path):
    import rc_ops
    from rc_ops_queue import Queue, lock_path
    d = fx / "ops" / "まとめて 処理"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    qf = d / "queue.json"
    qa, qb = d / "動画A_roughcut", d / "動画B_roughcut"
    for o in (qa, qb):
        o.mkdir()
        shutil.copy(fx / "transcript.json", o / "transcript.json")   # 文字起こしは済んでいる想定（Whisperなし）
    broken = d / "壊れた 動画.mp4"
    broken.write_text("これは動画ではありません", encoding="utf-8")
    tpl = str(fx / "tpl")
    base = ["batch.py", "--queue", str(qf), "add"]
    p = _run(base + [str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--join", "--goal", "thumbs", "--llm", "none",
                     "--cam2", str(fx / "phone.mp4"), "--template", tpl, "--out", str(qa)], fx)
    p2 = _run(base + [str(broken), "--goal", "finish", "--llm", "none", "--template", tpl, "--glossary", "マキタ、ドリル"], fx)
    p3 = _run(base + [str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--join", "--goal", "rough", "--llm", "none",
                      "--mode", "outdoor", "--out", str(qb)], fx)
    pm = _run(base + [str(fx / "C0001.MP4"), "--llm", "manual"], fx)
    items = Queue(qf).load()
    check("まとめて: 順番待ちに追加", all(x.returncode == 0 for x in (p, p2, p3)) and len(items) == 3
          and items[0]["goal"] == "thumbs" and items[0]["cam2"] and items[1]["glossary"] == ["マキタ", "ドリル"],
          _tail(p) if p.returncode else str(len(items)))
    check("まとめて: 貼り付け（manual）は追加しない", pm.returncode != 0, _tail(pm, 3))

    # 実行中は二重に起動しない
    with rc_ops.SingleRun(lock_path(qf)) as ok:
        pl = _run(["batch.py", "--queue", str(qf), "run"], fx)
    check("まとめて: 二重に起動しない", ok and pl.returncode != 0 and "すでにほかで実行中" in pl.stdout + pl.stderr,
          _tail(pl, 3))

    pr = _run(["batch.py", "--queue", str(qf), "run"], fx)
    items = Queue(qf).load()
    st = [it["status"] for it in items]
    check("まとめて: 順番に処理し、エラーがあっても次へ進む", pr.returncode == 0 and st == ["done", "error", "done"],
          f"{st}" + _tail(pr, 20))
    a, bad, b = items
    check("まとめて: サムネイルまで（完成動画とサムネイル案）",
          (qa / "C0001_完成.mp4").exists() and a["result"].get("thumbnails") == 9 and a["result"].get("video"),
          json.dumps(a["result"], ensure_ascii=False))
    check("まとめて: 荒編集だけ（完成動画は作らない）",
          (qb / "C0001_荒編集.xml").exists() and not (qb / "C0001_完成.mp4").exists() and b["steps_done"] == ["rough"])
    check("まとめて: エラーの理由を残す", "[エラー]" in bad["message"] and "ffprobe" in bad["message"], bad["message"])
    check("まとめて: 動画ごとのログ（batch_log.txt）", (qa / "batch_log.txt").exists()
          and "仕上げ" in (qa / "batch_log.txt").read_text(encoding="utf-8"))
    pli = _run(["batch.py", "--queue", str(qf), "list"], fx)
    check("まとめて: 結果の一覧", "[完了]" in pli.stdout and "[エラー]" in pli.stdout and "C0001_完成.mp4" in pli.stdout,
          _tail(pli, 12))

    # 途中で止まった（荒編集は済み・仕上げの途中）→ 続きから
    q = Queue(qf)
    q.update(b["id"], status="running", goal="finish", template=tpl)
    q.remove([bad["id"]])
    pr2 = _run(["batch.py", "--queue", str(qf), "run"], fx)
    b2 = Queue(qf).get(b["id"])
    check("まとめて: 止まったものは続きから（終わった工程は飛ばす）",
          pr2.returncode == 0 and b2["status"] == "done" and "前回終わっているので飛ばします" in pr2.stdout
          and (qb / "C0001_完成.mp4").exists() and pr2.stdout.count("文字起こし") == 0, _tail(pr2, 10))
    # ショートまで: 荒編集 → 演出プラン → 素材全体を1本の縦型ショート（横長の完成動画は作らない）
    qc = d / "動画C_roughcut"
    qc.mkdir()
    shutil.copy(fx / "transcript.json", qc / "transcript.json")
    ps = _run(base + [str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--join", "--goal", "short", "--llm", "none",
                      "--template", tpl, "--out", str(qc)], fx)
    pr3 = _run(["batch.py", "--queue", str(qf), "run"], fx)
    c = Queue(qf).load()[-1]
    check("まとめて: ショートまで（縦型ショートだけ作り、演出プランも作る）",
          ps.returncode == 0 and pr3.returncode == 0 and c["status"] == "done"
          and c["steps_done"] == ["rough", "plan", "short"] and (qc / "C0001_ショート.mp4").exists()
          and not (qc / "C0001_完成.mp4").exists() and (qc / "plan_used.json").exists()
          and str(c["result"].get("video", "")).endswith("_ショート.mp4") and c["result"].get("final_sec"),
          f"{c.get('steps_done')} {c.get('message')}" + _tail(pr3, 8))
    n = Queue(qf).clear_done()
    check("まとめて: 終わったものを片付ける", n == 3 and Queue(qf).load() == [])


# ---------------------------------------------------------------- 困ったときの情報
def t_support(check, fx: Path, out: Path):
    import rc_ops
    secret = "sk-ant-api03-TESTSECRETabcdef123456"
    home = str(Path.home())
    settings = {"api_key": secret, "mode": "talk", "last_dir": os.path.join(home, "動画")}
    log = (f"■ 荒編集\n[エラー] Claude API: 401 invalid x-api-key {secret}\n"
           f"ANTHROPIC_API_KEY={secret}\n素材: {home}/動画/C0001.MP4\n"
           '{"refresh_token": "1//0gTESTREFRESHTOKENabcdefghijkl", "token": "ya29.TESTACCESS"}\n')
    text, files = rc_ops.collect(ROOT, log, settings, out, {"最後のエラー": f"x {secret}"})
    allt = text + "".join(files.values())
    check("困ったとき: APIキー・トークンを伏せる", "TESTSECRET" not in allt and "TESTREFRESH" not in allt
          and "TESTACCESS" not in allt and "設定済み（伏せています）" in text)
    check("困ったとき: ユーザー名（ホームフォルダ）を伏せる", (home + "/動画") not in allt and "%USERPROFILE%" in allt)
    need = ["OS:", "Python:", "GPU:", "FFmpeg:", "文字起こしのGPU:", "このツールの版:", "メモリ:", "ライブラリ:"]
    check("困ったとき: 環境（OS・GPU・FFmpeg・Python）が入る", all(k in text for k in need),
          str([k for k in need if k not in text]))
    zp = rc_ops.save_zip(files, fx / "ops" / "困ったとき")
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        inner = z.read("困ったときの情報.txt").decode("utf-8")
    check("困ったとき: zip に保存", "ログ/画面のログ.txt" in names and "設定.json" in names and "## 環境" in inner,
          str(names))
    p = _run(["support_info.py", "--out", str(fx / "ops" / "困ったとき CLI")], fx)
    check("困ったとき: コマンド（support_info.py）", p.returncode == 0
          and list((fx / "ops" / "困ったとき CLI").glob("*.zip")), _tail(p, 5))


# ---------------------------------------------------------------- 自動更新
def _repo_files() -> list[str]:
    p = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    return [f for f in p.stdout.splitlines() if f and (ROOT / f).is_file()]


def _make_zip(path: Path, files: dict[str, bytes], sha: str):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AutoVideoEditor-main/", b"")
        for rel, data in files.items():
            z.writestr("AutoVideoEditor-main/" + rel, data)
        z.comment = sha.encode()


def _install(dst: Path, files: dict[str, bytes]):
    for rel, data in files.items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def t_update(check, fx: Path):
    import rc_ops_update as U
    d = fx / "ops" / "更新 テスト"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    old = {f: (ROOT / f).read_bytes() for f in _repo_files() if not f.startswith("tests/_fixtures")}
    new = dict(old)
    new["gui.py"] = old["gui.py"] + "\n# 新しい版の印\n".encode()
    new["templates/default/template.toml"] = old["templates/default/template.toml"] + b"\n# upstream change\n"
    new["templates/default/se/pop.wav"] = old["templates/default/se/pop.wav"] + b"\0\0"
    new["新しい機能.py"] = "print('new')\n".encode()
    new["requirements.txt"] = old["requirements.txt"] + b"\n# new dependency\n"
    zp = d / "AutoVideoEditor-main.zip"
    _make_zip(zp, new, "b" * 40)

    # --- 1. 版の記録がない（zip を展開しただけ）。自分でテンプレートを直している ---
    app = d / "アプリ 1"
    _install(app, old)
    user_toml = old["templates/default/template.toml"] + "\n# 自分で直した色\n".encode()
    (app / "templates/default/template.toml").write_bytes(user_toml)
    (app / "templates/mine").mkdir(parents=True)
    (app / "templates/mine/template.toml").write_text("# 自分のテンプレート\n", encoding="utf-8")
    (app / "gui_ext_mine.py").write_text("# 自分の拡張\n", encoding="utf-8")
    (app / ".venv").mkdir()
    (app / ".venv/marker").write_text("venv", encoding="utf-8")
    p = _run([str(ROOT / "update.py"), "--app-dir", str(app), "--zip", str(zp), "--yes", "--no-deps"], fx, cwd=app)
    ok = p.returncode == 0 and "UPDATED=1" in p.stdout
    check("更新: zip から上書き", ok and (app / "gui.py").read_bytes() == new["gui.py"]
          and (app / "新しい機能.py").exists(), _tail(p))
    check("更新: 自分で直したテンプレートは残す", (app / "templates/default/template.toml").read_bytes() == user_toml
          and "そのまま: templates/default/template.toml" in p.stdout)
    check("更新: 自分のテンプレート・拡張・.venv は消さない", (app / "templates/mine/template.toml").exists()
          and (app / "gui_ext_mine.py").exists() and (app / ".venv/marker").exists())
    vf = json.loads((app / U.VERSION_FILE).read_text(encoding="utf-8")) if (app / U.VERSION_FILE).exists() else {}
    check("更新: 版を記録", vf.get("sha") == "b" * 40)
    check("更新: ライブラリが変わったことを知らせる", "setup.bat をもう一度実行" in p.stdout)
    bdirs = list((app / U.BACKUP_DIR).iterdir()) if (app / U.BACKUP_DIR).exists() else []
    check("更新: 上書き前のファイルをバックアップ", len(bdirs) == 1 and (bdirs[0] / "gui.py").read_bytes() == old["gui.py"]
          and (bdirs[0] / U.NEW_COPY / "templates/default/template.toml").exists())

    # --- 2. 元に戻す ---
    p = _run([str(ROOT / "update.py"), "--app-dir", str(app), "--rollback"], fx, cwd=app)
    check("更新: 元に戻せる", p.returncode == 0 and (app / "gui.py").read_bytes() == old["gui.py"]
          and not (app / "新しい機能.py").exists() and not (app / U.VERSION_FILE).exists()
          and (app / "templates/default/template.toml").read_bytes() == user_toml, _tail(p, 5))

    # --- 3. セットアップで印を記録してあれば、直していないテンプレートは新しくなる ---
    app2 = d / "アプリ 2"
    _install(app2, old)
    p0 = _run([str(ROOT / "update.py"), "--app-dir", str(app2), "--record-manifest"], fx, cwd=app2)
    pop = app2 / "templates/default/se/pop.wav"
    pop.write_bytes(b"RIFF my own sound")   # 自分の効果音に差し替えた
    p = _run([str(ROOT / "update.py"), "--app-dir", str(app2), "--zip", str(zp), "--yes", "--no-deps"], fx, cwd=app2)
    check("更新: 直していないテンプレートは新しい版に",
          p0.returncode == 0 and p.returncode == 0 and
          (app2 / "templates/default/template.toml").read_bytes() == new["templates/default/template.toml"], _tail(p))
    check("更新: 差し替えた効果音は残す", pop.read_bytes() == b"RIFF my own sound")
    # 2回目の更新（印は前回の更新で記録）：今度はテンプレートを直していたら残す
    new2 = dict(new)
    new2["templates/default/template.toml"] = new["templates/default/template.toml"] + b"# v3\n"
    new2["gui.py"] = new["gui.py"] + b"# v3\n"
    zp2 = d / "v3.zip"
    _make_zip(zp2, new2, "c" * 40)
    (app2 / "templates/default/template.toml").write_bytes(new["templates/default/template.toml"] + b"# mine\n")
    p = _run([str(ROOT / "update.py"), "--app-dir", str(app2), "--zip", str(zp2), "--yes", "--no-deps"], fx, cwd=app2)
    check("更新: 2回目の更新でも、直したテンプレートは残す",
          p.returncode == 0 and (app2 / "gui.py").read_bytes() == new2["gui.py"]
          and (app2 / "templates/default/template.toml").read_bytes().endswith(b"# mine\n"), _tail(p))

    # --- 4. git で管理しているフォルダは触らない ---
    app3 = d / "アプリ 3"
    _install(app3, {"gui.py": old["gui.py"]})
    (app3 / ".git").mkdir()
    p = _run([str(ROOT / "update.py"), "--app-dir", str(app3), "--zip", str(zp), "--yes"], fx, cwd=app3)
    check("更新: git のフォルダは自動更新しない", p.returncode != 0 and "git pull" in p.stdout + p.stderr
          and (app3 / "gui.py").read_bytes() == old["gui.py"])

    # --- 5. 危ない zip・関係ない zip ---
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("x/gui.py", b"")
        z.writestr("x/requirements.txt", b"")
        z.writestr("x/roughcut.py", b"")
        z.writestr("x/../../evil.py", b"")
    try:
        U.read_zip(bad.getvalue())
        rejected = False
    except U.UpdateError:
        rejected = True
    try:
        U.read_zip(b"not a zip")
        rejected2 = False
    except U.UpdateError:
        rejected2 = True
    check("更新: 危ないパス・壊れた zip は使わない", rejected and rejected2)

    # --- 6. 新しい版の確認（GitHub への通信は偽物） ---
    calls = []

    def fake_get(url, timeout=15, progress=None):
        calls.append(url)
        if "api.github.com" in url:
            return json.dumps({"sha": "d" * 40, "commit": {"committer": {"date": "2030-01-02T03:04:05Z"},
                                                           "message": "新しい機能を追加\n\n詳細"}}).encode()
        return zp.read_bytes()
    orig = U._http_get
    U._http_get = fake_get
    try:
        newer, loc, rem = U.check(app2)
        same = U.is_newer({"sha": "d" * 40, "date": ""}, rem)
        files_old = U.is_newer({"sha": "", "date": "2029-12-01T00:00:00+00:00"}, rem)
        files_new = U.is_newer({"sha": "", "date": "2030-01-02T00:00:00+00:00"}, rem)
    finally:
        U._http_get = orig
    check("更新: 新しい版があるかの確認", newer and rem["message"] == "新しい機能を追加" and not same
          and files_old and not files_new and "MiyamaeLing/AutoVideoEditor/commits/main" in calls[0], str(calls))
    # zip のダウンロード経由（update.py をそのまま動かし、urllib を偽物に差し替える）
    app4 = d / "アプリ 4"
    _install(app4, old)
    code = (f"import sys, json; sys.path.insert(0, {str(ROOT)!r}); sys.argv=['update.py','--app-dir',{str(app4)!r},'--yes','--no-deps']\n"
            "import rc_ops_update as U\n"
            f"zb = open({str(zp)!r},'rb').read()\n"
            "def g(url, timeout=15, progress=None):\n"
            "    if 'api.github.com' in url:\n"
            "        return json.dumps({'sha':'b'*40,'commit':{'committer':{'date':'2030-01-01T00:00:00Z'},'message':'m'}}).encode()\n"
            "    assert url.endswith('/archive/refs/heads/main.zip'), url\n"
            "    progress and progress(100)\n"
            "    return zb\n"
            "U._http_get = g\n"
            "import runpy; runpy.run_path(" + repr(str(ROOT / "update.py")) + ", run_name='__main__')\n")
    p = subprocess.run([PY, "-c", code], cwd=app4, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=_env(fx))
    check("更新: GitHub から zip を取ってきて更新（通信は偽物）", p.returncode == 0 and "ダウンロード 100%" in p.stdout
          and (app4 / "新しい機能.py").exists(), _tail(p))

    # --- 7. ライブラリの入れ直しが必要か ---
    app5 = d / "アプリ 5"
    _install(app5, old)
    files, sha = U.read_zip(zp.read_bytes())
    plan = U.apply(files, app5, sha=sha, log=lambda *_: None)
    files_same = dict(files)
    files_same["gui.py"] = files["gui.py"] + b"#\n"
    plan2 = U.apply(files_same, app5, sha="e" * 40, log=lambda *_: None)
    check("更新: requirements.txt が変わったときだけライブラリを入れ直す",
          plan["requirements_changed"] and not plan2["requirements_changed"] and sha == "b" * 40)


# ---------------------------------------------------------------- 画面
GUI_CODE = r'''
import os, sys, json, time, shutil
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
res = {}
shown = []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warning", a[2] if len(a) > 2 else "")))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[2] if len(a) > 2 else "")))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
out = Path(OUT); fx = Path(FX)
res["ext"] = w.extensions
res["help"] = [a.text() for a in w.help_menu.actions()]
res["help_visible"] = w.help_menu.menuAction().isVisible()
tabs = [w.tabs.tabText(i) for i in range(w.tabs.count())]
res["tabs"] = tabs

def wait(cond, sec=60):
    t0 = time.time()
    while not cond() and time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.05)
    return cond()

# --- まとめて処理：作成タブから追加 ---
qp = w.queue_page
c = w.create
c.files.add([str(fx / "C0001.MP4"), str(fx / "C0002.MP4")])
c.llm.setCurrentIndex(c.llm.findData("manual"))
shown.clear()
res["manual_rejected"] = (qp.add_from_create("rough") is False) and any("claude.ai" in s for _, s in shown)
c.llm.setCurrentIndex(c.llm.findData("none"))
c.glossary.setText("マキタ、ドリル")
res["added"] = qp.add_from_create("rough")
res["create_cleared"] = c.files.count() == 0
items = qp.q.load()
res["item"] = items[-1] if items else {}
# 出力フォルダに文字起こしを置いておく（Whisperなしで動かすため）
o = Path(items[-1]["out"])
qp.q.update(items[-1]["id"], out=str(out / "画面から_roughcut"), glossary=[])   # 固有名詞があると文字起こしをやり直すため
(out / "画面から_roughcut").mkdir(parents=True, exist_ok=True)
shutil.copy(fx / "transcript.json", out / "画面から_roughcut" / "transcript.json")
qp.refresh()
w.tabs.setCurrentWidget(qp)
for _ in range(5): app.processEvents()
w.grab().save(str(out / "gui_ops_queue.png"))
shown.clear()
res["started"] = qp.run()
res["running_text"] = qp.b_run.text()
wait(lambda: not w.runner.busy, 240)
for _ in range(5): app.processEvents()
items = qp.q.load()
res["status"] = items[-1]["status"]
res["cell"] = qp.table.item(qp.table.rowCount() - 1, 3).text()
res["msg"] = qp.table.item(qp.table.rowCount() - 1, 5).text()
res["done_dialog"] = any("まとめて処理が終わりました" in s for _, s in shown)
qp.table.selectRow(qp.table.rowCount() - 1)
res["open_enabled"] = qp.b_open.isEnabled()
w.grab().save(str(out / "gui_ops_queue_done.png"))
qp.open_result()
res["opened_review"] = w.tabs.currentWidget() is w.review and w.review.out == out / "画面から_roughcut"

# --- 困ったときの情報をコピー ---
import gui_ext_ops
w.settings.setValue("api_key", "sk-ant-api03-GUISECRET999999")
w.runner.log.appendPlainText("テスト用のログ sk-ant-api03-GUISECRET999999")
gui_ext_ops.copy_support_info(w)
wait(lambda: getattr(w, "_ops_last_support", None) is not None, 120)
clip = QGuiApplication.clipboard().text()
res["clip_ok"] = "困ったときの情報" in clip and "GUISECRET" not in clip and "設定済み（伏せています）" in clip
res["zip_ok"] = bool(getattr(w, "_ops_last_support", None)) and Path(w._ops_last_support[1]).exists()
dlg = getattr(w, "_ops_support_dialog", None)
if dlg:
    for _ in range(5): app.processEvents()
    dlg.grab().save(str(out / "gui_ops_support.png"))
    dlg.close()
w.settings.setValue("api_key", "")

# --- 起動時のお知らせ（通信は偽物） ---
import rc_ops_update
os.environ["AVE_UPDATE_CHECK"] = "1"
rem = {"sha": "f" * 40, "date": "2030-01-02T03:04:05Z", "message": "まとめて処理を追加"}
rc_ops_update.check = lambda *a, **k: (True, {"sha": "", "date": ""}, rem)
gui_ext_ops.check_on_startup(w)
wait(lambda: getattr(w, "_ops_banner", None) is not None, 20)
for _ in range(5): app.processEvents()
res["banner"] = bool(getattr(w, "_ops_banner", None)) and w._ops_banner.isVisible()
w.tabs.setCurrentWidget(w.create)
w.grab().save(str(out / "gui_ops_banner.png"))

# --- 最新版に更新（git のフォルダでないとき） ---
calls = []
tmpapp = out / "更新先"
tmpapp.mkdir(exist_ok=True)
gui_ext_ops.APP_DIR = tmpapp
real_start = w.runner.start
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    return True
w.runner.start = fake_start
gui_ext_ops.start_update(w)
wait(lambda: bool(calls), 20)
res["update_call"] = calls[-1] if calls else None
w.runner.start = real_start
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, out: Path, fx: Path):
    gd = fx / "ops" / "画面"
    if gd.exists():
        shutil.rmtree(gd)
    gd.mkdir(parents=True)
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\nFX = {str(fx)!r}\n" + GUI_CODE
    env = _env(fx, APPDATA=str(gd / "appdata"), XDG_CONFIG_HOME=str(gd / "appdata"), AVE_SETTINGS_DIR=str(gd / "appdata"))
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=600)
    except subprocess.TimeoutExpired:
        check("画面(運用): テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("画面(運用): テスト", False, p.stderr[-2000:])
        return
    r = json.loads(line[7:])
    check("画面: 拡張（ヘルプ・まとめて処理）を読み込む", {"gui_ext_ops", "gui_ext_queue"} <= set(r["ext"]), str(r["ext"]))
    check("画面: ヘルプのメニュー", r["help_visible"] and "困ったときの情報をコピー" in r["help"]
          and "最新版に更新…" in r["help"], str(r["help"]))
    check("画面: まとめて処理のタブ", "まとめて処理" in r["tabs"])
    check("画面: 貼り付け（manual）では追加しない", r["manual_rejected"])
    it = r["item"]
    check("画面: 作成タブの素材と設定で順番待ちに入れる", r["added"] and r["create_cleared"] and len(it.get("inputs", [])) == 2
          and it.get("goal") == "rough" and it.get("llm") == "none" and it.get("glossary") == ["マキタ", "ドリル"], str(it))
    check("画面: まとめて処理を実行して結果を表示", r["started"] and r["status"] == "done" and r["cell"] == "完了"
          and "C0001_荒編集.xml" in r["msg"] and r["done_dialog"], f"{r['status']} {r['cell']} {r['msg']}")
    check("画面: 結果を開く（確認・仕上げタブ）", r["open_enabled"] and r["opened_review"])
    check("画面: 困ったときの情報をクリップボードへ（キーは伏せる）", r["clip_ok"] and r["zip_ok"])
    check("画面: 起動時に新しい版をお知らせ", r["banner"])
    check("画面: 最新版に更新（update.py を実行）", r["update_call"] == ["update.py", ["--yes"]], str(r["update_call"]))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    (fx / "ops").mkdir(exist_ok=True)
    t_transcribe_resume(check, fx)
    t_render_parts(check, fx)
    t_queue(check, fx)
    t_support(check, fx, out)
    t_update(check, fx)
    if (out / "session.json").exists():
        t_finish_resume(check, out, fx)
    t_gui(check, out, fx)


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
