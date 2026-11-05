"""テスト用の素材を作る（tests/_fixtures/ 以下。git には入れない）。

- C0001.MP4 / C0002.MP4 : カメラが分割した想定の本編（1920x1080 29.97fps、喋りの代わりに音のバースト）
- phone.mp4             : 3.5秒遅れて録画を始めた2台目カメラ（縦型）
- 文字起こしのキャッシュ（transcript.json）を用意するので、Whisper のモデルは不要
- claude_cuts.json / claude_meta.json / plan.json : AIの返答の代わり
- tpl/ : default テンプレートのフォントを Linux の日本語フォントに差し替えたもの
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FX = ROOT / "tests" / "_fixtures"
SR = 48000


def _wav(path, y):
    y = np.clip(y, -1, 1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.repeat(y[:, None], 2, 1) * 32767).astype(np.int16).tobytes())


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def find_jp_font() -> Path:
    import os
    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for p in ("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", "/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              *(str(win_fonts / n) for n in ("BIZ-UDGothicR.ttc", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc"))):
        if Path(p).exists():
            return Path(p)
    try:
        out = subprocess.run(["fc-list", ":lang=ja", "file"], capture_output=True, text=True).stdout.split("\n")
    except FileNotFoundError:   # Windows には fc-list がない
        out = []
    for line in out:
        if line.strip():
            return Path(line.split(":")[0])
    raise SystemExit("日本語フォントが見つかりません（fonts-ipafont-gothic を入れてください）")


def make(force: bool = False) -> Path:
    # 文字起こしのキャッシュはフォルダの場所を含むので、別の場所からコピーした古い素材は作り直す
    stamp = f"ok {FX.resolve()}"
    if (FX / "done").exists() and not force and (FX / "done").read_text(encoding="utf-8") == stamp:
        return FX
    if FX.exists():
        shutil.rmtree(FX)
    FX.mkdir(parents=True)
    rng = np.random.default_rng(1)
    x = np.zeros(SR * 70, dtype=np.float32)
    t = 0.5
    while t < 68:
        d = rng.uniform(0.3, 2.5)
        n, s = int(d * SR), int(t * SR)
        x[s:s + n] += 0.3 * np.sin(2 * np.pi * rng.uniform(200, 800) * np.arange(n) / SR) * rng.uniform(0.3, 1)
        t += d + rng.uniform(0.2, 3)
    _wav(FX / "main.wav", x[:SR * 60])
    d = int(3.5 * SR)
    _wav(FX / "cam2.wav", x[d:d + SR * 50] * 0.6 + rng.normal(0, 0.02, SR * 50).astype(np.float32))
    src = "testsrc=size=1920x1080:rate=30000/1001"
    _ff("-f", "lavfi", "-i", src, "-i", str(FX / "main.wav"), "-t", "30", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(FX / "C0001.MP4"))
    _ff("-f", "lavfi", "-i", src, "-ss", "30", "-i", str(FX / "main.wav"), "-t", "30", "-c:v", "libx264",
        "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(FX / "C0002.MP4"))
    _ff("-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30", "-i", str(FX / "cam2.wav"), "-t", "50", "-c:v", "libx264",
        "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(FX / "phone.mp4"))

    from rc_media import SourceSet, probe
    from rc_transcribe import _cache_key

    def seg(s, e, text):
        n = len(text)
        return {"start": s, "end": e, "text": text,
                "words": [{"start": round(s + (e - s) * i / n, 3), "end": round(s + (e - s) * (i + 1) / n, 3),
                           "word": ch} for i, ch in enumerate(text)]}
    segs = [seg(1, 3, "どうもこんにちは。"), seg(3.4, 6, "今日はこのドリルをレビューします。"),
            seg(8, 10, "えーと"), seg(11, 14, "まずはスペックですが、重さが1.5キロで"),
            seg(15, 17, "あ、違う、もう一回。"), seg(18, 22, "まずはスペックですが、重さが1.2キロです。"),
            seg(23, 25, "撮れてるかな？"), seg(26, 29.5, "実際に穴を開けてみます。"),
            seg(40, 45, "はい、きれいに開きました。すごい。"), seg(46, 52, "というわけでマキタのドリルでした。"),
            seg(53, 57, "チャンネル登録お願いします。")]
    srcset = SourceSet([probe(FX / "C0001.MP4"), probe(FX / "C0002.MP4")])
    (FX / "transcript.json").write_text(json.dumps(
        {"key": _cache_key(srcset, "large-v3-turbo", ""), "segments": segs}, ensure_ascii=False), encoding="utf-8")
    # チャンクID: 1こんにちは 2レビュー 3えーと 4重さ1.5 5あ違う 6重さ1.2 7撮れてる 8穴を開け 9きれいに 10すごい 11マキタ 12登録
    (FX / "claude_cuts.json").write_text(json.dumps({
        "cuts": [{"ids": [4, 5], "type": "retake", "reason": "重さを言い直している"},
                 {"ids": [7], "type": "offscript", "reason": "カメラ確認"}],
        "review": [{"id": 12, "note": "締めの定型文"}],
        "highlights": [{"id": 9, "label": "穴あけ成功"}],
        "corrections": [{"id": 11, "text": "というわけでマキタのドリルでした"}]}, ensure_ascii=False), encoding="utf-8")
    (FX / "claude_meta.json").write_text(json.dumps({
        "titles": ["マキタのドリルを本音レビュー", "軽すぎるドリル"], "description": "マキタのドリルをレビューしました。",
        "chapters": [{"line": 1, "title": "オープニング"}, {"line": 2, "title": "スペック"}, {"line": 3, "title": "実際に使ってみた"}],
        "thumbnail_texts": ["軽すぎ！", "穴あけ一瞬"], "tags": ["マキタ", "ドリル"]}, ensure_ascii=False), encoding="utf-8")
    (FX / "plan.json").write_text(json.dumps({
        "telops": [{"start_id": 2, "end_id": 2, "text": "ドリルをレビュー！", "style": "emphasis", "se": "pop"},
                   {"start_id": 6, "end_id": 6, "text": "重さ 1.2kg", "style": "info", "se": "none"},
                   {"start_id": 9, "end_id": 10, "text": "きれいに開いた！", "style": "funny", "se": "ding"}],
        "zooms": [{"id": 10, "strength": "medium"}],
        "bgm": [{"start_id": 1, "mood": "upbeat"}, {"start_id": 9, "mood": "calm"}]}, ensure_ascii=False),
        encoding="utf-8")

    # テスト用テンプレート（日本語フォントを差し替え、テスト用BGMを入れる）
    tpl = FX / "tpl"
    shutil.copytree(ROOT / "templates" / "default", tpl,
                    ignore=shutil.ignore_patterns("_preview_work", "template_preview.mp4", "preview_*.png"))
    font = find_jp_font()
    shutil.copy(font, tpl / "fonts" / font.name)
    from PIL import ImageFont
    family = ImageFont.truetype(str(font), 20).getname()[0]
    s = (tpl / "template.toml").read_text(encoding="utf-8")
    s = s.replace("BIZ UDPGothic", family)
    s = re.sub(r'font_file = ".*?"', f'font_file = "fonts/{font.name}"', s)
    s = re.sub(r"font_index = \d+", "font_index = 0", s)
    (tpl / "template.toml").write_text(s, encoding="utf-8")
    for mood, bpm, notes in (("upbeat", 140, [523, 659, 784, 659]), ("calm", 70, [262, 330, 392, 330])):
        tt = np.arange(SR * 20) / SR
        y = np.zeros_like(tt)
        beat = 60 / bpm
        for i in range(int(20 / beat)):
            s0, n = int(i * beat * SR), int(beat * SR)
            u = np.arange(n) / SR
            y[s0:s0 + n] += (np.sin(2 * np.pi * notes[i % 4] * u) * np.exp(-u * 3))[:len(y) - s0]
        _wav(tpl / "bgm" / mood / f"test_{mood}.wav", y / np.abs(y).max() * 0.7)
    (FX / "done").write_text(stamp, encoding="utf-8")
    return FX


if __name__ == "__main__":
    print(make(force="--force" in sys.argv))
