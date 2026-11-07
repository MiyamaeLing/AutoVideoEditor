"""完成動画の区間を書き出す機能（rc_clip.py / clip.py / gui_ext_clip.py）のテスト。

- 時刻の読み方（1:23 / 1:23.5 / 全角 / 1分23秒）と、ファイル名・章の区間
- 書き出した動画の長さ・映像の位置・映像と音のずれ（読み始めを映像のキーフレームに重ねて、FFmpeg 9 の音ずれも確かめる）
- 字幕（SRT）を区間に合わせてずらす
- コマンド（clip.py）と画面（「区間を書き出す…」）
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
FLASHES = [2.0, 5.0, 9.0]   # 偽の完成動画で、フラッシュと「ピッ」が同時に出る時刻


def make_result(d: Path) -> tuple[Path, Path]:
    """偽の結果フォルダ（session.json・完成動画・字幕・章）。(フォルダ, 完成動画)"""
    import test_enhance as TE
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    video = d / "C0001_完成.mp4"
    TE._make_flash_clip(video, 12.0, FLASHES, 3)   # 30fps・15コマごとにキーフレーム（0.5秒ごと）
    video.with_suffix(".wav").unlink(missing_ok=True)
    (d / "session.json").write_text(json.dumps({"stem": "C0001"}), encoding="utf-8")
    (d / "final_info.json").write_text(json.dumps({"duration": 12.0, "chapters": [
        {"t": 0.0, "title": "オープニング"}, {"t": 1.0, "title": "スペック"}, {"t": 6.0, "title": "まとめ"},
        {"t": 11.8, "title": "短すぎる章"}]}, ensure_ascii=False), encoding="utf-8")
    (d / "C0001_完成.srt").write_text(
        "1\n00:00:00,500 --> 00:00:01,500\nまたぐ字幕\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n1行目\n2行目\n\n"
        "3\n00:00:07,000 --> 00:00:08,000\n区間の外\n", encoding="utf-8")
    return d, video


# ---------------------------------------------------------------- 部品
def t_units(check, d: Path, video: Path):
    import rc_clip as C
    ok = [C.parse_time(x) for x in ("83", "1:23", "1:23.5", "1:02:03", "１：２３", "1分23秒", "1分", "23.5秒", " 0:05 ")]
    check("区間: 時刻の読み方（1:23 / 1:23.5 / 全角 / 1分23秒）", ok == [83, 83, 83.5, 3723, 83, 83, 60, 23.5, 5], str(ok))
    bad = []
    for x in ("", "abc", "1:2:3:4", "1.5:20", "-3", "1:23:"):
        try:
            C.parse_time(x)
            bad.append(x)
        except ValueError:
            pass
    check("区間: 読めない時刻はエラーにする", not bad, str(bad))
    check("区間: 時刻の表示", [C.fmt_time(t) for t in (5, 83.44, 3723)] == ["0:05.0", "1:23.4", "1:02:03.0"])
    check("区間: ファイル名（clips に、開始と終了の時刻入り）",
          C.clip_path(video, 83.9, 130).relative_to(d).as_posix() == "clips/C0001_完成_1m23s-2m10s.mp4"
          and C.clip_path(video, 3723, 3725).name == "C0001_完成_1h02m03s-1h02m05s.mp4", str(C.clip_path(video, 83.9, 130)))
    chs = C.chapters(d)
    check("区間: 章の区間（次の章の頭まで・短すぎる章は除く）",
          [(c["title"], c["start"], c["end"]) for c in chs]
          == [("オープニング", 0.0, 1.0), ("スペック", 1.0, 6.0), ("まとめ", 6.0, 11.8)], str(chs))
    cues = C.clip_captions([(0.5, 1.5, "a"), (2.0, 3.0, "b"), (5.9, 6.5, "c"), (7.0, 8.0, "d")], 1.0, 6.0)
    check("区間: 字幕を区間に合わせてずらす（境目にまたがる字幕は切る）",
          cues == [(0.0, 0.5, "a"), (1.0, 2.0, "b"), (4.9, 5.0, "c")], str(cues))
    try:
        C.final_video(d.parent)
        e1 = ""
    except ValueError as e:
        e1 = str(e)
    empty = d.parent / "完成なし"
    empty.mkdir(exist_ok=True)
    (empty / "session.json").write_text(json.dumps({"stem": "X"}), encoding="utf-8")
    try:
        C.final_video(empty)
        e2 = ""
    except ValueError as e:
        e2 = str(e)
    check("区間: 結果フォルダでない・完成動画がないときは分かるように止める",
          "session.json" in e1 and "完成動画がまだありません" in e2, f"{e1} / {e2}")


def t_export(check, d: Path, video: Path):
    import rc_clip as C
    import test_enhance as TE
    # 読み始め 1.0秒は映像のキーフレームにちょうど重なる（FFmpeg 9 で音が早まっていた所）
    res = C.export(video, 1.0, 6.0)
    out = res["video"]
    import rc_media
    dur = rc_media.probe(out).duration if out.exists() else 0
    check("区間: 書き出した動画の長さ", abs(dur - 5.0) < 0.06, f"{dur:.3f}秒")
    vt = TE._flash_times(out, 30.0) if out.exists() else []
    want = [t - 1.0 for t in FLASHES if 1.0 <= t < 6.0]
    ok_v = len(vt) == len(want) and all(abs(a - b) < 0.02 for a, b in zip(vt, want))
    check("区間: 映像は指定した所から始まる（コマ単位）", ok_v, f"{[round(t, 3) for t in vt]} / 予定 {want}")
    at = TE._onsets(TE._read_f32(out), want, 0.25) if ok_v else []
    diff = [abs(a - v) * 1000 for a, v in zip(at, vt)]
    check("区間: 映像と音がずれない（読み始めがキーフレームでも 10ms 以内）", ok_v and diff and max(diff) < 10,
          f"ずれ {[round(x, 1) for x in diff]} ms")
    srt = res["srt"]
    text = srt.read_text(encoding="utf-8-sig") if srt else ""
    check("区間: 字幕（SRT）も区間に合わせて作る（2行の字幕もそのまま）",
          srt is not None and "00:00:01,000 --> 00:00:02,000\n1行目\n2行目" in text and "区間の外" not in text
          and "00:00:00,000 --> 00:00:00,500\nまたぐ字幕" in text, text[:200])
    check("区間: 書きかけのファイルを残さない", not list(out.parent.glob("*書き出し中*")))
    bad = []
    for s, e in ((5.0, 4.0), (3.0, 3.2), (13.0, 14.0)):
        try:
            C.export(video, s, e, d / "clips" / "bad.mp4")
            bad.append((s, e))
        except ValueError:
            pass
    check("区間: おかしい区間（逆・短すぎる・動画の外）は書き出さない",
          not bad and not (d / "clips" / "bad.mp4").exists(), str(bad))
    end = C.export(video, 10.0, 99.0, d / "clips" / "end.mp4")
    check("区間: 終了が動画の長さを超えたら、動画の終わりまで", abs(end["duration"] - 2.0) < 0.05, f'{end["duration"]:.2f}')


def t_cli(check, d: Path):
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    run = lambda *a: subprocess.run([PY, "clip.py", str(d), *a], cwd=ROOT, capture_output=True, text=True,   # noqa: E731
                                    encoding="utf-8", errors="replace", env=e, timeout=300)
    p = run("--list")
    check("clip.py: 章の一覧", p.returncode == 0 and "2. 0:01.0〜0:06.0  スペック" in p.stdout, p.stdout[-300:])
    p = run("--chapter", "3", "--no-srt")
    made = d / "clips" / "C0001_完成_0m06s-0m11s.mp4"
    check("clip.py: 章をまるごと書き出す", p.returncode == 0 and made.exists() and not made.with_suffix(".srt").exists(),
          (p.stdout + p.stderr)[-300:])
    p = run("--start", "1:00", "--end", "1:05")
    check("clip.py: 動画の外はエラーで止める", p.returncode != 0 and "[エラー]" in p.stderr, p.stderr[-200:])
    p = run("--start", "1")
    check("clip.py: 終了がないときは使い方を知らせる", p.returncode != 0 and "--end" in p.stderr, p.stderr[-200:])


# ---------------------------------------------------------------- 画面（画面なしで別プロセス）
GUI_CODE = r'''
import os, sys, json, time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
errs = []
sys.excepthook = lambda t, v, tb: errs.append(repr(v))
import gui
import gui_ext_clip as G
app = QApplication([])
asked = []
QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[1]) or QMessageBox.No)
QMessageBox.information = staticmethod(lambda *a, **k: None)
def pump(sec=0.3):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents(); time.sleep(0.02)
res = {}
video, d = Path(VIDEO), Path(RESULT)
w = gui.MainWindow(); w.show(); pump()
ext = w.clip_ext
res["btn_off_without_result"] = not ext.btn.isEnabled()
if FULL and Path(FULL, "session.json").exists():
    w.review.load(Path(FULL)); pump()
    res["btn_on_with_final"] = ext.btn.isEnabled() == (ext.final() is not None) and ext.final() is not None
dlg = G.ClipDialog(ext, video, d); dlg.show(); pump(1.0)
res["chapters"] = [dlg.chapter.itemText(i) for i in range(1, dlg.chapter.count())]
dlg.chapter.setCurrentIndex(2); dlg._pick_chapter(2); pump()
res["chapter_range"] = [dlg.start_edit.text(), dlg.end_edit.text(), dlg.len_label.text()]
dlg.end_edit.setText("0:00.5"); pump(0.1)
res["bad_blocked"] = (not dlg.b_export.isEnabled()) and dlg.status.property("role") == "notice"
dlg.start_edit.setText("０：０１"); dlg.end_edit.setText("6"); pump(0.1)
res["fullwidth_ok"] = dlg.range() == (1.0, 6.0) and dlg.b_export.isEnabled()
# 書き出す（本当に clip.py を動かす）
dlg.export(); pump(0.5)
t0 = time.time()
while w.runner.busy and time.time() - t0 < 180:
    pump(0.2)
pump(0.5)
res["made"] = dlg.made.name if dlg.made else ""
res["made_exists"] = bool(dlg.made and dlg.made.exists() and dlg.made.with_suffix(".srt").exists())
res["status"] = dlg.status.text()
res["play_enabled"] = dlg.b_play_made.isEnabled()
# 同じ区間をもう一度 →「上書きしますか？」で「いいえ」なら書き出さない
before = dlg.made.stat().st_mtime if dlg.made else 0
dlg.export(); pump(0.5)
res["overwrite_asked"] = bool(asked) and not w.runner.busy and (dlg.made.stat().st_mtime == before if dlg.made else False)
# 閉じると動画ファイルを手放す（Windows では開いたままだと、完成動画を書き出し直せない）
dlg.reject(); pump(0.5)
tmp = video.with_name("移動テスト.mp4")
try:
    os.replace(video, tmp); os.replace(tmp, video); res["released"] = True
except OSError as e:
    res["released"] = repr(e)
# 開き直すと、前に選んだ区間のまま
dlg2 = G.ClipDialog(ext, video, d); res["remember"] = [dlg2.start_edit.text(), dlg2.end_edit.text()]; dlg2.reject(); pump(0.2)
w.close(); pump(0.2)
res["errors"] = errs
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, d: Path, video: Path, full: Path | None):
    shutil.rmtree(d / "clips", ignore_errors=True)   # 1回目は上書きの確認なしで書き出す（2回目で確認が出るかを見る）
    cfg = d.parent / "appdata"
    code = (f"ROOT = {str(ROOT)!r}\nVIDEO = {str(video)!r}\nRESULT = {str(d)!r}\n"
            f"FULL = {str(full) if full else ''!r}\n" + GUI_CODE)
    env = dict(os.environ, APPDATA=str(cfg), XDG_CONFIG_HOME=str(cfg), AVE_SETTINGS_DIR=str(cfg),
               PYTHONIOENCODING="utf-8", AVE_UPDATE_CHECK="0")
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=400)
    except subprocess.TimeoutExpired:
        check("区間（画面）: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("区間（画面）: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("区間（画面）: 結果を開いていなければボタンは押せない", r["btn_off_without_result"])
    if "btn_on_with_final" in r:
        check("区間（画面）: 完成動画がある結果を開くとボタンが押せる", r["btn_on_with_final"])
    check("区間（画面）: 章から選べる", len(r["chapters"]) == 3 and "スペック" in r["chapters"][1], str(r["chapters"]))
    check("区間（画面）: 章を選ぶと開始・終了・長さが入る", r["chapter_range"] == ["0:01.0", "0:06.0", "長さ 0:05.0"],
          str(r["chapter_range"]))
    check("区間（画面）: おかしい区間は書き出せず、理由を出す", r["bad_blocked"])
    check("区間（画面）: 全角の時刻も読める", r["fullwidth_ok"])
    check("区間（画面）: 書き出すと動画と字幕ができる",
          r["made"] == "C0001_完成_0m01s-0m06s.mp4" and r["made_exists"] and r["play_enabled"] and "できました" in r["status"],
          f'{r["made"]} / {r["status"]}')
    check("区間（画面）: 同じ区間は上書きしてよいか聞く", r["overwrite_asked"])
    check("区間（画面）: 閉じると動画ファイルを手放す（書き出し直せる）", r["released"] is True, str(r["released"]))
    check("区間（画面）: 開き直すと前に選んだ区間のまま", r["remember"] == ["0:01.0", "0:06.0"], str(r["remember"]))
    check("区間（画面）: エラーが出ない", not r["errors"], str(r["errors"]))


def run_all(check, out: Path, fx: Path):
    base = fx / "clip"
    d, video = make_result(base / "C0001_roughcut")
    t_units(check, d, video)
    t_export(check, d, video)
    t_cli(check, d)
    full = out if out and (out / "session.json").exists() and list(out.glob("*_完成.mp4")) else None
    t_gui(check, d, video, full)


if __name__ == "__main__":
    import make_fixtures
    res = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    print(f"\n{sum(res)}/{len(res)} 件 成功")
    sys.exit(0 if all(res) else 1)
