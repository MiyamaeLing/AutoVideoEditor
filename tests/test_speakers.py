"""話者の区別（字幕の色分け）のテスト（GPU・ネット・APIキーなしで動く）。

    python tests/test_speakers.py         # 単独で実行
    python tests/smoke_test.py            # 通しテストの最後からも呼ばれます

声の高さと響きの違う「合成した声」で会話を作り、次を確認する:
  - 2人・3人の会話で、発言ごとの話者を正しく分けられる（人数を指定／自動）
  - 1人だけのときは、自動で1人と判定する
  - roughcut.py --speakers で speakers.json ができる・分割ファイルでも時刻が合う
  - Premiere 用の字幕（SRT）に話者名を入れる／外す（speakers.py --write-srt）
  - 完成動画の字幕が、テンプレートの色で人ごとに色分けされる（ASS と書き出した静止画）
  - 文字起こしの区切りが変わったら、古い推定は使わない
  - GUI（gui_ext_speakers.py）で名前の変更・発言ごとの話者の修正・保存ができる
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable
SR = 16000

# 母音の響き（フォルマント：F1, F2, F3）
VOWELS = np.array([(800, 1200, 2500), (300, 2300, 3000), (350, 1300, 2400), (500, 1900, 2600), (500, 900, 2400)],
                  dtype=np.float64)
# 合成する声：(声の高さHz, 響きの倍率, 明るさ)
VOICES = [(115, 1.00, 1.0), (235, 1.17, 0.7), (165, 1.08, 1.3), (300, 1.25, 0.9)]


def synth_voice(dur: float, voice: tuple, rng) -> np.ndarray:
    """喋り声のような音（声の高さが揺れ、母音が変わる）を合成する"""
    f0, fs, tilt = voice
    n = int(dur * SR)
    t = np.arange(n) / SR
    f0c = f0 * rng.uniform(0.93, 1.07) * (1 + 0.07 * np.sin(2 * np.pi * rng.uniform(0.5, 1.3) * t + rng.uniform(0, 6)))
    f0c *= 1 - 0.08 * t / max(dur, 0.1)
    phase = 2 * np.pi * np.cumsum(f0c) / SR
    syl = rng.uniform(0.16, 0.24)
    nsyl = int(np.ceil(dur / syl)) + 1
    vid = rng.integers(0, len(VOWELS), nsyl)
    F = VOWELS[vid][np.minimum((t / syl).astype(int), nsyl - 1)] * fs
    k = 400
    F = np.stack([np.convolve(np.pad(F[:, j], (k // 2, k - k // 2 - 1), mode="edge"), np.ones(k) / k, "valid")
                  for j in range(3)], axis=1)
    y = np.zeros(n)
    for h in range(1, int(5000 / f0) + 1):
        fk = h * f0c
        amp = (1 / (1 + ((fk - F[:, 0]) / 90) ** 2) + 0.6 / (1 + ((fk - F[:, 1]) / 110) ** 2)
               + 0.3 / (1 + ((fk - F[:, 2]) / 150) ** 2)) * (fk / 100) ** (-0.5 * tilt)
        y += amp * np.sin(h * phase)
    env = 0.35 + 0.65 * np.sin(np.pi * ((t % syl) / syl)) ** 2
    fade = np.minimum(1, np.minimum(t, dur - t) / 0.03)
    y = y * env * np.clip(fade, 0, 1)
    return (y / (np.abs(y).max() + 1e-9) * rng.uniform(0.35, 0.7)).astype(np.float32)


def make_conversation(n_voices: int, total: float, seed: int = 0, noise: float = 0.003):
    """n_voices 人の会話（無音の間あり）を作る。戻り値は (音声, [(開始, 終了, 話者番号)])"""
    rng = np.random.default_rng(seed)
    y = rng.normal(0, noise, int(total * SR)).astype(np.float32)
    utts = []
    t, spk = 0.6, 0
    while t < total - 3:
        d = rng.uniform(0.7, 3.2)
        a = int(t * SR)
        v = synth_voice(d, VOICES[spk], rng)
        y[a:a + len(v)] += v[: len(y) - a]
        utts.append((round(t, 3), round(t + d, 3), spk))
        t += d + rng.uniform(0.3, 1.2)
        if n_voices > 1 and rng.random() < 0.7:
            spk = int((spk + rng.integers(1, n_voices)) % n_voices)
    return y, utts


def accuracy(lab: dict[int, int], truth: dict[int, int]) -> float:
    """番号の付け方が違っても、いちばん合う対応で正解率を出す"""
    from itertools import permutations
    ks = sorted(set(lab.values()) | set(truth.values()))
    best = 0
    for perm in permutations(ks, len(ks)):
        m = dict(zip(ks, perm))
        best = max(best, sum(1 for i, v in truth.items() if m.get(lab.get(i, -1), -2) == v))
    return best / max(len(truth), 1)


def _wav16(path: Path, y: np.ndarray):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())


def _run(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def _tail(p: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-n:])


def _direct_features(y: np.ndarray, utts) -> dict:
    """ffmpeg を通さずに、発言ごとの特徴を作る（グループ分けだけを確かめる用）"""
    import rc_speakers as S
    feats = {}
    for i, (s, e, _spk) in enumerate(utts):
        x = y[int(s * SR):int(e * SR)]
        n = (len(x) - S.FRAME) // S.HOP + 1
        fr = np.lib.stride_tricks.as_strided(x, shape=(n, S.FRAME), strides=(x.strides[0] * S.HOP, x.strides[0]))
        feats[i + 1] = S._summarize(*S.frame_features(fr))
    return feats


# ---------------------------------------------------------------- グループ分け
def t_cluster(check):
    import rc_speakers as S
    cases = [(2, 2, 0.95, "2人（人数を指定）"), (2, None, 0.95, "2人（自動）"), (1, None, 1.0, "1人（自動）"),
             (3, 3, 0.9, "3人（人数を指定）"), (3, None, 0.9, "3人（自動）")]
    for voices, num, need, name in cases:
        y, utts = make_conversation(voices, 90, seed=voices * 7)
        lab, _score, k, _info = S.cluster(_direct_features(y, utts), num)
        truth = {i + 1: u[2] for i, u in enumerate(utts)}
        acc = accuracy(lab, truth)
        check(f"話者: {name}", k == voices and acc >= need, f"{k}人 / 正解率 {acc:.0%}")
    # 雑音が多くても、人数を指定すれば分けられる
    y, utts = make_conversation(2, 90, seed=5, noise=0.05)
    lab, *_ = S.cluster(_direct_features(y, utts), 2)
    acc = accuracy(lab, {i + 1: u[2] for i, u in enumerate(utts)})
    check("話者: 雑音があっても2人を分ける", acc >= 0.9, f"正解率 {acc:.0%}")
    # 推定し直したとき、前と同じ人には同じ番号が付く
    remap = S._match_old({1: 0, 2: 1, 3: 0}, {1: 1, 2: 0, 3: 1}, 2)
    check("話者: 推定し直しても名前が入れ替わらない", remap == {0: 1, 1: 0}, str(remap))
    # 短すぎる発言は、前の発言の人にする
    from rc_transcribe import Chunk
    lab, sc = {1: 0, 3: 1}, {1: 1.0, 3: 1.0}
    S.fill_short(lab, sc, [Chunk(1, 0, 1, "a"), Chunk(2, 1.1, 1.2, "b"), Chunk(3, 2, 3, "c"), Chunk(4, 3.1, 3.2, "d")])
    check("話者: 短い発言は前の発言の人", lab == {1: 0, 2: 0, 3: 1, 4: 1}, str(lab))
    # 色のタグ（色の指定がない字幕は今までと同じ）
    from rc_ass import _color_tag
    tag = _color_tag({"color": "#12A0fe", "outline_color": "#000000"})
    check("話者: 字幕の色のタグ", tag == r"{\1c&HFEA012&\3c&H000000&}" and _color_tag(None) == "", tag)


# ---------------------------------------------------------------- 素材を作って通しで確認
def make_media(fx: Path) -> tuple[Path, dict[int, int]]:
    """2人の会話の動画（2ファイルに分割）と、文字起こしのキャッシュを作る。戻り値は (フォルダ, 正解)"""
    d = fx / "speakers"
    stamp = f"v2 {d.resolve()}"
    truth_p = d / "truth.json"
    if (d / "done").exists() and (d / "done").read_text(encoding="utf-8") == stamp:
        return d, {int(k): v for k, v in json.loads(truth_p.read_text(encoding="utf-8")).items()}
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    y, utts = make_conversation(2, 60, seed=11)
    _wav16(d / "talk.wav", y)
    for name, ss in (("A.MP4", 0), ("B.MP4", 30)):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x505860:size=640x360:rate=30",
                        "-ss", str(ss), "-i", str(d / "talk.wav"), "-t", "30", "-c:v", "libx264", "-preset",
                        "ultrafast", "-c:a", "aac", "-ar", "48000", "-shortest", str(d / name)], check=True)
    from rc_media import SourceSet, probe
    from rc_transcribe import _cache_key

    def seg(s, e, text):
        n = len(text)
        return {"start": s, "end": e, "text": text,
                "words": [{"start": round(s + (e - s) * i / n, 3), "end": round(s + (e - s) * (i + 1) / n, 3),
                           "word": ch} for i, ch in enumerate(text)]}
    who = ["たろう", "はなこ"]
    segs, truth = [], {}
    for i, (s, e, spk) in enumerate(utts):
        segs.append(seg(s, e, f"{who[spk]}の{i + 1}番目の発言です。"))
        truth[i + 1] = spk
    srcset = SourceSet([probe(d / "A.MP4"), probe(d / "B.MP4")])
    (d / "transcript.json").write_text(json.dumps({"key": _cache_key(srcset, "large-v3-turbo", ""), "segments": segs},
                                                  ensure_ascii=False), encoding="utf-8")
    truth_p.write_text(json.dumps(truth), encoding="utf-8")
    (d / "done").write_text(stamp, encoding="utf-8")
    return d, truth


def _srt_blocks(p: Path) -> list[str]:
    return [b.split("\n", 2)[2] for b in p.read_text(encoding="utf-8-sig").strip().split("\n\n") if b.count("\n") >= 2]


def t_pipeline(check, fx: Path) -> Path | None:
    import rc_speakers as S
    d, truth = make_media(fx)
    out = d / "out 話者"          # 日本語とスペースを含むフォルダ
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    shutil.copy(d / "transcript.json", out / "transcript.json")
    rough = ["roughcut.py", str(d / "A.MP4"), str(d / "B.MP4"), "--mode", "outdoor", "--llm", "none", "--out", str(out)]

    p = _run(rough)
    ok = p.returncode == 0 and not (out / "speakers.json").exists()
    check("話者: 指定しなければ推定しない（既定は無効）", ok, _tail(p) if p.returncode else "")
    if p.returncode:
        return None
    srt0 = (out / "A_字幕.srt").read_text(encoding="utf-8-sig")

    p = _run(rough + ["--speakers"])
    if p.returncode:
        check("話者: roughcut.py --speakers", False, _tail(p))
        return None
    data = json.loads((out / "speakers.json").read_text(encoding="utf-8"))
    acc = accuracy(S.labels_of(data), truth)
    check("話者: roughcut.py --speakers（自動）で2人", data["detected"]["num"] == 2 and acc >= 0.95,
          f"{data['detected']['num']}人 / 正解率 {acc:.0%}")
    check("話者: 名前を入れる設定でなければ、SRTは今までと同じ",
          (out / "A_字幕.srt").read_text(encoding="utf-8-sig") == srt0)

    p = _run(["speakers.py", str(out), "--num", "2"])
    data = json.loads((out / "speakers.json").read_text(encoding="utf-8"))
    acc = accuracy(S.labels_of(data), truth)
    check("話者: speakers.py --num 2", p.returncode == 0 and acc >= 0.95,
          f"正解率 {acc:.0%}" + (_tail(p) if p.returncode else ""))

    # 正解に合わせた名前を付けて、SRTに名前を入れる
    lab = S.labels_of(data)
    names = ["", ""]
    for i in sorted(truth):
        names[lab[i]] = ["たろう", "はなこ"][truth[i]]
    p = _run(["speakers.py", str(out), "--no-detect", "--names", "、".join(names), "--srt-names", "on"])
    blocks = _srt_blocks(out / "A_字幕.srt")
    named = [b for b in blocks if "：" in b]
    good = sum(1 for b in named if b.split("：", 1)[1].startswith(b.split("：", 1)[0]))   # 「たろう：たろうの…」
    check("話者: Premiere用SRTに話者名", p.returncode == 0 and len(named) >= len(truth) * 0.9
          and good >= len(named) * 0.95, f"{good}/{len(named)}/{len(truth)}" + (_tail(p) if p.returncode else ""))

    # GUI の「カットの変更を反映」と同じように荒編集をやり直しても、名前の設定は残る
    p = _run(rough)
    blocks2 = _srt_blocks(out / "A_字幕.srt")
    check("話者: 荒編集をやり直しても名前が残る", p.returncode == 0 and blocks2 == blocks)

    p = _run(["speakers.py", str(out), "--no-detect", "--srt-names", "off"])
    check("話者: SRTの名前を外す", p.returncode == 0 and (out / "A_字幕.srt").read_text(encoding="utf-8-sig") == srt0)

    # 文字起こしの区切りが変わったら、古い推定は使わない
    from rc_transcribe import Chunk
    chunks = [Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))]
    chunks[0].start += 0.5
    check("話者: 区切りが変わったら古い推定を使わない", S.load(out, chunks, quiet=True) is None
          and S.load(out, quiet=True) is not None)
    return out


def t_finish(check, fx: Path, out: Path):
    """完成動画の字幕が、テンプレートの色で話者ごとに色分けされる"""
    import rc_speakers as S
    tpl = fx / "speakers" / "tpl"
    if tpl.exists():
        shutil.rmtree(tpl)
    shutil.copytree(fx / "tpl", tpl)
    t = (tpl / "template.toml").read_text(encoding="utf-8")
    if "[speakers]" in t:
        t = t[: t.index("[speakers]")]
    t += '\n[speakers]\ncolors = ["#FFFFFF", "#22FF22"]\noutline_colors = ["#1A1A1A", "#000080"]\n'
    (tpl / "template.toml").write_text(t, encoding="utf-8")
    shutil.rmtree(out / "work", ignore_errors=True)
    p = _run(["finish.py", str(out), "--template", str(tpl), "--llm", "none", "--preview", "30", "--no-thumbs",
              "--keep-work"])
    if p.returncode:
        check("話者: 完成動画の書き出し", False, _tail(p))
        return
    ass = (out / "work" / "subs.ass").read_text(encoding="utf-8")
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0,")]
    green = [ln for ln in lines if r"\1c&H22FF22&\3c&H800000&" in ln]
    white = [ln for ln in lines if r"\1c&HFFFFFF&" in ln]
    check("話者: 完成動画の字幕をテンプレートの色で色分け", len(green) > 3 and len(white) > 3 and
          len(green) + len(white) == len(lines), f"緑 {len(green)} / 白 {len(white)} / 全部 {len(lines)}")
    # 話者ごとに、字幕が出ている時刻の静止画を取り出して、字幕の所に緑の文字があるか調べる
    lab = S.labels_of(S.load(out))
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    from rc_transcribe import Chunk
    src = SourceSet([probe(Path(x)) for x in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    chunks = [Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))]
    shots = {}
    for c in chunks:
        r = tl.map_range(c.start, c.end)
        spk = lab.get(c.id)
        if r and r[0][0] < 28 and spk not in shots and c.end - c.start > 1.2:
            shots[spk] = (r[0][0] + r[0][1]) / 2
    mp4 = out / "A_プレビュー.mp4"
    res = {}
    from PIL import Image
    for spk, tt in shots.items():
        png = out / f"speaker_frame_{spk}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{tt:.2f}", "-i", str(mp4), "-frames:v", "1", str(png)],
                       check=True)
        im = np.asarray(Image.open(png).convert("RGB")).astype(int)
        bottom = im[int(im.shape[0] * 0.8):]
        res[spk] = int(((bottom[..., 1] > 200) & (bottom[..., 0] < 90) & (bottom[..., 2] < 90)).sum())
    check("話者: 書き出した動画で、話者2の字幕が緑", len(res) == 2 and res.get(1, 0) > 500 and res.get(0, 0) < 100,
          str(res))
    shutil.rmtree(out / "work", ignore_errors=True)


# ---------------------------------------------------------------- GUI
GUI_CODE = r'''
import os, sys, json
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer, QItemSelectionModel
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
res = {}
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
w = gui.MainWindow(); w.resize(1300, 860); w.show()
res["loaded"] = "gui_ext_speakers" in w.extensions
res["button"] = getattr(w, "speaker_button", None) is not None
# 拡張（gui_ext_*.py）が項目を足したメニューだけ出す（何もないメニューは出さない）
res["menus_hidden"] = all(m.menuAction().isVisible() == bool(m.actions()) for m in (w.tools_menu, w.help_menu))
out = Path(OUT)
w.review.load(out)
import gui_ext_speakers as G
dlg = G.open_dialog(w)
app.processEvents()
dlg.grab().save(str(out / "gui_speakers.png"))
w.grab().save(str(out / "gui_speakers_main.png"))
res["rows"] = dlg.t_utt.rowCount()
res["speakers"] = dlg.t_spk.rowCount()
res["status"] = dlg.status.text()
# 名前を変える
dlg.t_spk.item(0, 1).setText("ゆうき")
res["name_in_rows"] = [dlg.t_utt.item(r, 1).text() for r in range(dlg.t_utt.rowCount())]
# 1行目と2行目を選んで、話者2にする（数字キー・ボタンと同じ処理）
dlg.t_utt.clearSelection()
for r in (0, 1):
    dlg.t_utt.selectionModel().select(dlg.t_utt.model().index(r, 0),
                                      QItemSelectionModel.Select | QItemSelectionModel.Rows)
ids = [dlg.rows[r]["id"] for r in (0, 1)]
dlg.assign_selected(1)
res["dirty"] = dlg.b_save.text()
dlg.c_srt.setChecked(True)
dlg.save()
loop = QEventLoop()
def wait():
    if not w.runner.busy:
        loop.quit()
t = QTimer(); t.timeout.connect(wait); t.start(100)
QTimer.singleShot(60000, loop.quit)
loop.exec()
d = json.loads((out / "speakers.json").read_text(encoding="utf-8"))
res["names0"] = d["names"][0]
res["labels"] = [d["manual"].get(str(i), d["auto"].get(str(i))) for i in ids]
res["srt_names"] = d["srt_names"]
res["srt"] = (out / "A_字幕.srt").read_text(encoding="utf-8-sig")[:400]
res["stage"] = w.runner.stage.text()
# 開き直しても直した内容が出る
dlg.close()
dlg = G.open_dialog(w)
res["reopen"] = [dlg.labels.get(i) for i in ids]
res["reopen_name"] = dlg.t_spk.item(0, 1).text()
dlg.grab().save(str(out / "gui_speakers_after.png"))
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, fx: Path, out: Path):
    gd = fx / "speakers" / "gui"
    if gd.exists():
        shutil.rmtree(gd)
    shutil.copytree(out, gd, ignore=shutil.ignore_patterns("work", "*.mp4", "*.png"))
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(gd)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(fx / "appdata"), PYTHONIOENCODING="utf-8", XDG_CONFIG_HOME=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("話者GUI: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("話者GUI: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    check("話者GUI: 拡張の読み込みとボタン（メニューは増やさない）", r["loaded"] and r["button"] and r["menus_hidden"])
    check("話者GUI: 発言と話者の一覧", r["rows"] > 10 and r["speakers"] == 2,
          f"{r['rows']}行 / {r['speakers']}人 {r['status']}")
    check("話者GUI: 名前の変更が一覧に出る", any("ゆうき" in x for x in r["name_in_rows"]))
    check("話者GUI: 未保存の印", r["dirty"].endswith("●"), r["dirty"])
    check("話者GUI: 保存（名前・発言ごとの手直し）", r["names0"] == "ゆうき" and r["labels"] == [1, 1],
          f"{r['names0']} {r['labels']}")
    check("話者GUI: SRTに名前を入れて書き直す", r["srt_names"] and "：" in r["srt"],
          r["stage"] + " " + r["srt"][:80].replace("\n", " "))
    check("話者GUI: 開き直しても直した内容が残る", r["reopen"] == [1, 1] and r["reopen_name"] == "ゆうき",
          str(r["reopen"]))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    t_cluster(check)
    sp_out = t_pipeline(check, fx)
    if sp_out:
        t_finish(check, fx, sp_out)
        t_gui(check, fx, sp_out)


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
