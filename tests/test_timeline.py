"""タイムライン表示と画面のデザイン（ダークモード・案内）のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_timeline.py         # 単独で実行（先に tests/smoke_test.py を1回実行しておく）
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

確認すること:
  - 波形を FFmpeg で取り出せる・キャッシュから読める・長い素材でもメモリを使いすぎない
  - 画面の幅に合わせた間引き、残す区間の計算、目盛りの間隔
  - GUI: 拡張口が残っている、タイムラインに 残す/カット/要確認/見どころ/章 が出る、
         クリックで再生と一覧の行の選択、行を選ぶとタイムラインも動く、拡大縮小・スクロール、
         ダブルクリックで残す/カットの切り替え、3時間の素材でも描画が軽い、
         ダークモードの切り替えと保存、文字の大きさ、初めての案内
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


# ---------------------------------------------------------------- 波形（Qt なし）
def t_waveform(check, fx: Path):
    import numpy as np
    import tracemalloc
    import rc_waveform as W

    work = fx / "timeline" / "wave"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # 1) 実際の素材から取り出す（素材 C0001 は 0.5秒までは無音、そのあとに音のかたまり）
    src = fx / "C0001.MP4"
    t0 = time.time()
    a = W.get(work, src, 30.0)
    took = time.time() - t0
    ok = a is not None and abs(a.size - 30 * W.RATE) <= W.RATE and a[: W.RATE // 4].max() < 10 and a.max() > 100
    check("タイムライン: 波形を取り出せる", ok, f"{a.size if a is not None else None}点 {took:.1f}秒")
    cache = W.cache_path(work, src)
    check("タイムライン: 波形をキャッシュに保存", cache.exists() and cache.stat().st_size < 5000, str(cache.name))
    t0 = time.time()
    b = W.get(work, src, 30.0)
    check("タイムライン: 2回目はキャッシュから読む", b is not None and np.array_equal(a, b) and time.time() - t0 < 0.5)

    # 2) 日本語とスペースを含む名前でも読める
    jp = work / "テスト 素材 (1).mp4"
    shutil.copy(src, jp)
    c = W.get(work, jp, 30.0)
    check("タイムライン: 日本語・スペースを含む名前", c is not None and np.array_equal(a, c))

    # 3) 長い素材（20分）でも、メモリに載せるのは少しずつ（音声そのままなら 19MB になる）
    long = work / "long.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:sample_rate=8000:duration=1200",
                    "-c:a", "aac", "-b:a", "24k", str(long)], check=True)
    tracemalloc.start()
    d = W.extract(long, 1200.0)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("タイムライン: 長い素材でもメモリを使いすぎない", d is not None and abs(d.size - 1200 * W.RATE) < W.RATE
          and peak < 6_000_000, f"{peak / 1e6:.1f}MB（20分）")

    # 4) 途中で止められる
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 2
    check("タイムライン: 波形の取り出しを途中で止められる", W.extract(long, 1200.0, should_stop=stop) is None)

    # 5) 画面の幅に合わせた間引き（素直に計算したものと同じになる）
    rng = np.random.default_rng(5)
    wave = rng.integers(0, 255, 50_000).astype(np.uint8)
    good = True
    for t_start, spp, width in ((0, 1.0, 900), (12.3, 0.013, 700), (900, 0.5, 400), (995, 0.2, 300), (-3, 2.0, 50)):
        got = W.peaks_for_view(wave, t_start, spp, width)
        for x in range(width):
            lo = int(np.floor((t_start + spp * x) * W.RATE))
            hi = max(int(np.ceil((t_start + spp * (x + 1)) * W.RATE)), lo + 1)
            lo_c, hi_c = max(lo, 0), min(hi, wave.size)
            want = wave[lo_c:hi_c].max() if hi_c > lo_c else 0
            if spp * W.RATE <= 2 and hi_c > lo_c:
                want = max(wave[min(lo_c, wave.size - 1)], wave[min(hi_c - 1, wave.size - 1)])
            if got[x] != want and not (spp * W.RATE > 2 and x == 0 and lo < 0):
                good = False
    check("タイムライン: 画面の幅に合わせた間引き", good)

    # 6) 残す区間（荒編集の結果 + 画面での未反映の変更）
    base = [(0.85, 6.25), (17.85, 22.25), (25.85, 29.75)]
    k = W.keep_intervals(base, cut_now=[(3.4, 6.0)], keep_now=[(8.0, 10.0), (21.0, 23.0)])
    check("タイムライン: 残す区間の計算", k == [(0.85, 3.4), (6.0, 6.25), (8.0, 10.0), (17.85, 23.0), (25.85, 29.75)],
          str(k))
    check("タイムライン: 目盛りの間隔", W.tick_step(0.01) == 1 and W.tick_step(1.0) == 120 and W.tick_step(100) >= 3600)


# ---------------------------------------------------------------- GUI（画面なしで別プロセス）
GUI_CODE = r'''
import os, sys, json, time, faulthandler
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
faulthandler.dump_traceback_later(150, exit=True)   # 止まったら、どこで止まったかを出して終わる
sys.path.insert(0, ROOT)
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt, QPoint, QSettings
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
import gui
app = QApplication([])
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
res = {}
out = Path(OUT)
shots = Path(SHOTS)

def pump(n=10):
    for _ in range(n):
        app.processEvents(); time.sleep(0.01)

w = gui.MainWindow(); w.resize(1280, 800); w.show(); pump()
res["ext_loaded"] = "gui_ext_timeline" in w.extensions
res["hooks"] = all(hasattr(x, a) for x, a in ((w, "tools_menu"), (w, "help_menu"), (w, "tabs"), (w.review, "ext_bar")))
# 拡張（gui_ext_*.py）が項目を足したメニューだけ出す（何もないメニューは出さない）
res["menus_hidden"] = all(m.menuAction().isVisible() == bool(m.actions()) for m in (w.tools_menu, w.help_menu))
res["view_menu"] = w.appearance.view_menu.title()
res["guide_first"] = w.appearance.guide is not None and w.appearance.guide.isVisible()
w.grab().save(str(shots / "tl_create_light.png"))

rv = w.review
tl = w.timeline
cv = tl.canvas
rv.load(out)
w.tabs.setCurrentWidget(rv)
pump()
t0 = time.time()
while tl.panel.status.text() and time.time() - t0 < 60:
    pump(5)
res["status"] = tl.panel.status.text()
res["duration"] = cv.duration
res["n_chunks"] = int(cv.starts.size)
ids = [c["id"] for c in rv.chunks]
res["cut_ids"] = [ids[i] for i in np.flatnonzero(cv.cut)]
res["review_ids"] = [ids[i] for i in np.flatnonzero(cv.mark == 1)]
res["hil_ids"] = [ids[i] for i in np.flatnonzero(cv.mark == 2)]
res["chapters"] = [c[1] for c in cv.chapters]
res["loaded"] = sorted((round(a, 1), round(b, 1)) for a, b in cv.loaded)
res["wave_max"] = int(cv.wave.max())
res["cache_files"] = len(list((out / "waveform_cache").glob("*.npy")))
# 残す区間は session.json の keep と同じ
sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
res["keep_same"] = [list(map(lambda v: round(v, 2), k)) for k in cv.keep] == [[round(a, 2), round(b, 2)] for a, b in sess["keep"]]
w.grab().save(str(shots / "tl_review_light.png"))
tl.panel.grab().save(str(shots / "tl_panel_light.png"))

# --- クリックでその場所を再生し、一覧の行も選ぶ（発言9「はい、きれいに開きました」= 40〜45秒、2本目の素材）---
i9 = ids.index(9)
t = 42.0
x = int(cv.t2x(t)); y = cv.height() // 2
QTest.mouseClick(cv, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
pump(5)
res["click_row"] = rv.table.currentRow() == i9
res["click_sel"] = cv.sel == (rv.chunks[i9]["start"], rv.chunks[i9]["end"])
res["click_src"] = Path(rv.player.source().toLocalFile()).name if rv.player else "（再生部品なし）"
res["click_info"] = rv.play_info.text() if rv.player else ""
res["click_pos"] = round(rv.player.position() / 1000, 1) if rv.player else -1
if rv.player:
    rv.player.stop()

# --- 一覧の行を選ぶと、タイムラインも動く ---
cv.zoom(0.1)   # 拡大
pump()
res["zoomed"] = cv.view_len < cv.duration * 0.2
res["scroll_max"] = tl.panel.scroll.maximum()
i2 = ids.index(2)
rv.table.setCurrentCell(i2, 1)
pump()
res["row_sel"] = cv.sel == (rv.chunks[i2]["start"], rv.chunks[i2]["end"])
res["row_visible"] = cv.t0 <= rv.chunks[i2]["start"] <= cv.t0 + cv.view_len
# スクロールバーで動かす
before = cv.t0
tl.panel.scroll.setValue(tl.panel.scroll.maximum())
pump()
res["scrolled"] = cv.t0 > before and abs(cv.t0 + cv.view_len - cv.duration) < 0.5
# ホイールで縮小・全体表示
cv.fit()
res["fit"] = abs(cv.view_len - cv.duration) < 0.5 and cv.t0 == 0

# --- 演出の表の行を選んでも、タイムラインが動く ---
if rv.t_telop.rowCount():
    rv.t_telop.setCurrentCell(0, 2)
    pump()
    cid = rv.t_telop.item(0, 0).data(Qt.UserRole)
    c = rv.chunks[ids.index(cid)]
    res["plan_sel"] = cv.sel == (c["start"], c["end"])
else:
    res["plan_sel"] = True

# --- ダブルクリックで 残す/カット を切り替え（発言2を、カットにする）---
x2 = int(cv.t2x((rv.chunks[i2]["start"] + rv.chunks[i2]["end"]) / 2))
QTest.mouseDClick(cv, Qt.LeftButton, Qt.NoModifier, QPoint(x2, y))
pump()
res["toggle_table"] = rv.table.item(i2, 0).checkState() == Qt.Unchecked
res["toggle_canvas"] = bool(cv.cut[i2])
res["toggle_dirty"] = rv.b_apply.text().endswith("●") and bool(rv.b_apply.property("attention"))
mid = (rv.chunks[i2]["start"] + rv.chunks[i2]["end"]) / 2
res["toggle_keep"] = not any(a <= mid < b for a, b in cv.keep)
cv.toggle_requested.emit(i2)   # もう一度で元に戻る
pump()
res["toggle_back"] = rv.table.item(i2, 0).checkState() == Qt.Checked and any(a <= mid < b for a, b in cv.keep)

# --- 再生位置の線 ---
if rv.player:
    # クリックで2本目の素材（C0002）を開いているので、その5秒の位置 = 全体の35秒
    tl._position(5000)
    res["playhead"] = round(cv.playhead or 0, 1)
else:
    res["playhead"] = 35.0

# --- ダークモード（設定に保存）---
light_cut = gui.C_CUT.name()
w.appearance.set_mode("dark")
pump()
res["dark_saved"] = QSettings("ai-roughcut", "AutoVideoEditor").value("ui/theme")
res["dark_rows"] = gui.C_CUT.name() != light_cut and rv.table.item(ids.index(3), 0).background().color().name() == gui.C_CUT.name()
res["dark_window"] = app.palette().color(app.palette().ColorRole.Window).lightness() < 80
res["dark_btn"] = w.appearance.theme_btn.text()
w.grab().save(str(shots / "tl_review_dark.png"))
w.tabs.setCurrentWidget(w.create)
pump()
w.grab().save(str(shots / "tl_create_dark.png"))

# --- 文字の大きさ ---
w.appearance.set_font("large")
pump()
res["font_large"] = app.font().pointSize()
w.appearance.set_font("normal")

# --- 初めての案内：「わかった」で次から出さない ---
w.appearance.guide.ok_btn.click()
pump()
res["guide_hidden"] = not w.appearance.guide.isVisible()
w.close()
w2 = gui.MainWindow(); w2.show(); pump()
res["guide_second"] = w2.appearance.guide is None or not w2.appearance.guide.isVisible()
import rc_theme
res["dark_restored"] = rc_theme.tokens()["name"] == "dark" and w2.appearance.theme_actions.checkedAction().data() == "dark"
w2.appearance.show_guide()
res["guide_menu"] = w2.appearance.guide.isVisible() and w2.tabs.currentWidget() is w2.create
w2.appearance.set_mode("light")
w2.close()

# --- 3時間の素材でも軽い（発言3000個・波形54万点）---
import gui_ext_timeline as X
big = X.TimelinePanel(); big.resize(1200, 160); big.show(); pump()
c3 = big.canvas
dur = 3 * 3600.0
st = np.sort(np.random.default_rng(1).uniform(0, dur - 5, 3000)); en = st + 3.0
c3.set_data(dur, st, en, ["発言"] * 3000, [""] * 3000, np.arange(3000) % 4 == 0, (np.arange(3000) % 7 == 0).astype(np.int8),
            [(a, b) for a, b in zip(st[1::2], en[1::2])], [(i * 600.0, f"章{i}") for i in range(18)])
c3.put_wave(0.0, np.random.default_rng(2).integers(0, 255, int(dur * 50)).astype(np.uint8), True)
times = []
for z in (1.0, 0.2, 0.05, 0.01):
    c3.fit(); c3.zoom(z)
    t0 = time.perf_counter(); c3.grab(); times.append(time.perf_counter() - t0)
res["long_paint_ms"] = round(max(times) * 1000)
c3.fit(); c3.grab().save(str(shots / "tl_long.png"))
print("RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
sys.stderr.flush()
os._exit(0)   # 動画再生の部品が、画面なしの環境で終了時に止まることがあるので、すぐ終わる
'''


def t_gui(check, out: Path, fx: Path):
    gd = fx / "timeline" / "gui"
    if gd.exists():
        shutil.rmtree(gd)
    shutil.copytree(out, gd, ignore=shutil.ignore_patterns("work", "waveform_cache"))
    shutil.copy(fx / "plan.json", gd / "plan_used.json")
    (gd / "plan_edited.json").unlink(missing_ok=True)
    shots = fx / "timeline" / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    cfg = fx / "timeline" / "config"
    if cfg.exists():
        shutil.rmtree(cfg)   # 初めて使う状態（案内が出る・ライト表示）から始める
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\nSHOTS = {str(shots)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(cfg), XDG_CONFIG_HOME=str(cfg), PYTHONIOENCODING="utf-8")
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired as e:
        partial = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        check("タイムライン: GUIのテスト", False, "時間切れ\n" + partial[-1500:] + "\n" + err[-3000:])
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("タイムライン: GUIのテスト", False, (p.stdout[-1500:] + "\n" + p.stderr[-3000:]))
        return
    r = json.loads(line[7:])
    check("タイムライン: 拡張として読み込まれる", r["ext_loaded"])
    check("タイムライン: 拡張口（tools_menu・help_menu・tabs・ext_bar）が残っている", r["hooks"] and r["menus_hidden"])
    check("タイムライン: 「表示」メニュー", r["view_menu"] == "表示", r["view_menu"])
    check("タイムライン: 長さと発言の数", abs(r["duration"] - 60) < 1 and r["n_chunks"] == 12,
          f"{r['duration']:.1f}秒 {r['n_chunks']}個")
    check("タイムライン: カット・要確認・見どころの帯", r["cut_ids"] == [3, 4, 5, 7] and r["review_ids"] == [12]
          and r["hil_ids"] == [9], f"{r['cut_ids']} {r['review_ids']} {r['hil_ids']}")
    check("タイムライン: 章の印", r["chapters"] == ["オープニング", "スペック", "実際に使ってみた"], str(r["chapters"]))
    check("タイムライン: 残す区間が荒編集の結果と同じ", r["keep_same"])
    check("タイムライン: 2本の素材の波形を読み込む", r["loaded"] == [[0.0, 30.0], [30.0, 60.0]] and r["wave_max"] > 100
          and r["cache_files"] == 2 and r["status"] == "", f"{r['loaded']} {r['status']}")
    check("タイムライン: クリックで一覧の行を選ぶ", r["click_row"] and r["click_sel"])
    check("タイムライン: クリックした場所をプレーヤーで再生", r["click_src"] in ("C0002.MP4", "（再生部品なし）")
          and (r["click_info"].startswith(("00:41", "00:42")) or r["click_src"] == "（再生部品なし）"),
          f"{r['click_src']} {r['click_info']} 位置{r['click_pos']}")
    check("タイムライン: 拡大とスクロール", r["zoomed"] and r["scroll_max"] > 0 and r["scrolled"] and r["fit"],
          f"{r['zoomed']} {r['scroll_max']} {r['scrolled']} {r['fit']}")
    check("タイムライン: 一覧の行を選ぶとタイムラインも動く", r["row_sel"] and r["row_visible"] and r["plan_sel"])
    check("タイムライン: ダブルクリックで残す/カットを切り替え", r["toggle_table"] and r["toggle_canvas"] and r["toggle_keep"]
          and r["toggle_dirty"] and r["toggle_back"])
    check("タイムライン: 再生位置の線", abs(r["playhead"] - 35.0) < 0.2, str(r["playhead"]))
    check("タイムライン: 3時間の素材でも描画が軽い", r["long_paint_ms"] < 400, f"{r['long_paint_ms']}ms")
    check("デザイン: ダークモードに切り替えて保存", r["dark_saved"] == "dark" and r["dark_rows"] and r["dark_window"]
          and r["dark_btn"] == "ライト", str(r["dark_saved"]))
    check("デザイン: 次に開いてもダークモードのまま", r["dark_restored"])
    check("デザイン: 文字の大きさ", r["font_large"] == 11, str(r["font_large"]))
    check("デザイン: 初めての案内が出る・「わかった」で次から出ない・メニューから再表示", r["guide_first"]
          and r["guide_hidden"] and r["guide_second"] and r["guide_menu"])


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    (fx / "timeline").mkdir(exist_ok=True)
    try:
        t_waveform(check, fx)
    except Exception as e:  # noqa: BLE001
        check("タイムライン: 波形のテスト", False, repr(e))
    if not (out / "session.json").exists():
        check("タイムライン: GUIのテスト（荒編集の結果が必要）", False, "先に荒編集が成功している必要があります")
        return
    t_gui(check, out, fx)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
