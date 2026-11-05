"""MVモードのテスト用の素材を作る（曲・映像）。tests/_fixtures/mv/ 以下に作る（git には入れない）。

- 曲は numpy で合成する。テンポ・拍・小節の頭・サビの位置が分かっているので、解析の答え合わせができる
- 映像は ffmpeg の lavfi で作る（動きの多い絵・少ない絵・暗い絵・途中で場面が変わる絵・音声なしの縦長の絵）
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 44100
CHORDS = {"Am": [57, 60, 64], "F": [53, 57, 60], "C": [48, 52, 55], "G": [55, 59, 62], "Em": [52, 55, 59]}
STRUCT = [("intro", 8), ("verse", 8), ("chorus", 8), ("verse", 8), ("chorus", 8), ("outro", 4)]


def _hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def _wav(path: Path, y: np.ndarray, sr: int = SR):
    y = np.clip(y, -1, 1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((np.repeat(y[:, None], 2, 1) * 32767).astype(np.int16).tobytes())


def make_song(path: Path, bpm: float = 128.0, lead: float = 0.6, struct=STRUCT, seed: int = 0,
              drift: float = 0.0, jitter: float = 0.0, noise: float = 0.0) -> dict:
    """曲を合成して、答え（拍・小節の頭・サビの時刻）を返す。

    drift: 曲の終わりまでにテンポが何割変わるか（生演奏のゆれ） / jitter: 1音ごとのずれ（秒） / noise: 雑音の大きさ
    """
    rng = np.random.default_rng(seed)
    nbars = sum(n for _, n in struct)
    nb = nbars * 4
    periods = 60 / (bpm * (1 + drift * np.arange(nb + 1) / nb))
    grid = lead + np.concatenate([[0.0], np.cumsum(periods)])     # 拍の時刻（ゆれ込み）
    beat = 60 / bpm
    total = float(grid[-1]) + 2.0
    y = np.zeros(int(total * SR), dtype=np.float64)

    def add(t, sig):
        t = t + (rng.normal(0, jitter) if jitter else 0.0)
        s = int(max(t, 0) * SR)
        e = min(len(y), s + len(sig))
        if e > s:
            y[s:e] += sig[: e - s]

    def kick(amp):
        u = np.arange(int(0.25 * SR)) / SR
        f = 50 + 70 * np.exp(-u * 25)
        return amp * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-u * 12)

    def snare(amp):
        u = np.arange(int(0.18 * SR)) / SR
        return amp * (rng.normal(0, 1, len(u)) * 0.6 + np.sin(2 * np.pi * 190 * u)) * np.exp(-u * 22)

    def hat(amp):
        u = np.arange(int(0.05 * SR)) / SR
        n = rng.normal(0, 1, len(u))
        n = np.diff(n, prepend=0)
        return amp * n * np.exp(-u * 70)

    def tone(notes, dur, amp, saw=False):
        u = np.arange(int(dur * SR)) / SR
        s = np.zeros_like(u)
        for m in notes:
            f = _hz(m)
            if saw:
                s += sum(np.sin(2 * np.pi * f * k * u) / k for k in range(1, 6))
            else:
                s += np.sin(2 * np.pi * f * u)
        env = np.minimum(1, u / 0.02) * np.minimum(1, (dur - u) / 0.05)
        return amp * s / len(notes) * env

    beats, downs, choruses = [], [], []
    b0 = 0
    for kind, n in struct:
        prog = ["F", "G", "Em", "Am"] if kind == "chorus" else ["Am", "F", "C", "G"]
        if kind == "chorus":
            choruses.append((float(grid[b0 * 4]), float(grid[(b0 + n) * 4])))
        for k in range(n):
            t_bar = float(grid[(b0 + k) * 4])
            beat = float(grid[(b0 + k) * 4 + 1] - t_bar)
            bar = beat * 4
            ch = CHORDS[prog[k % 4]]
            downs.append(t_bar)
            if kind == "intro":
                add(t_bar, kick(0.25))
                add(t_bar, tone(ch, bar, 0.10))
                for e in range(8):
                    add(t_bar + e * beat / 2, hat(0.03))
            elif kind == "verse":
                add(t_bar, tone(ch, bar, 0.12))
                for q in range(4):
                    add(t_bar + q * beat, tone([ch[0] - 12], beat * 0.9, 0.22))
                for q in (0, 2):
                    add(t_bar + q * beat, kick(0.55))
                for q in (1, 3):
                    add(t_bar + q * beat, snare(0.25))
                for e in range(8):
                    add(t_bar + e * beat / 2, hat(0.06))
            elif kind == "chorus":
                add(t_bar, tone(ch, bar, 0.28, saw=True))
                add(t_bar, tone([ch[2] + 12], bar, 0.12))
                for q in range(4):
                    add(t_bar + q * beat, kick(0.9))
                    add(t_bar + q * beat, tone([ch[0] - 12], beat * 0.9, 0.38, saw=True))
                for q in (1, 3):
                    add(t_bar + q * beat, snare(0.6))
                for e in range(16):
                    add(t_bar + e * beat / 4, hat(0.10))
            else:  # outro
                add(t_bar, kick(0.2))
                add(t_bar, tone(ch, bar, 0.09))
            for q in range(4):
                beats.append(float(grid[(b0 + k) * 4 + q]))
        b0 += n
    if noise:
        y += rng.normal(0, noise, len(y)) * np.abs(y).max()
    y = y / np.abs(y).max() * 0.8
    _wav(path, y.astype(np.float32))
    return {"bpm": bpm, "beats": beats, "downbeats": downs, "choruses": choruses, "duration": total}


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args], check=True)


def make_videos(folder: Path, size: str = "640x360") -> dict[str, Path]:
    """テスト用の映像素材を作る"""
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15"]
    # 動きが多い（testsrc2 は絵が動く）＋音声あり
    p = folder / "動き 多め.mp4"
    _ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30", "-f", "lavfi", "-i", "sine=f=330:d=24",
        "-t", "24", *enc, "-c:a", "aac", "-shortest", str(p))
    out["action"] = p
    # 動きが少ない（ゆっくり色が変わるだけ）
    p = folder / "静か.mp4"
    _ff("-f", "lavfi", "-i", f"gradients=size={size}:rate=30:speed=0.002:seed=3", "-t", "24", *enc, str(p))
    out["calm"] = p
    # 暗い（使わない方がよい）
    p = folder / "暗い.mp4"
    _ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30", "-t", "20", "-vf", "lutrgb=r=val*0.05:g=val*0.05:b=val*0.05", *enc, str(p))
    out["dark"] = p
    # 途中で場面が変わる（前半 mandelbrot → 後半 カラーバー）
    p = folder / "場面転換.mp4"
    _ff("-f", "lavfi", "-i", f"mandelbrot=size={size}:rate=30", "-f", "lavfi", "-i", f"smptebars=size={size}:rate=30",
        "-filter_complex", "[0:v]trim=duration=10,setpts=PTS-STARTPTS[a];[1:v]trim=duration=10,setpts=PTS-STARTPTS[b];"
        "[a][b]concat=n=2:v=1:a=0[v]", "-map", "[v]", *enc, str(p))
    out["scene"] = p
    # スマホの縦長・29.97fps・音声なし
    p = folder / "スマホ 縦.mp4"
    _ff("-f", "lavfi", "-i", "life=size=180x320:rate=30000/1001:mold=10:ratio=0.5:death_color=#203040:life_color=#F0C020",
        "-t", "20", "-vf", "scale=360:640:flags=neighbor", *enc, str(p))
    out["phone"] = p
    return out


def make_long_video(path: Path, minutes: int = 30) -> Path:
    """長い素材（小さい画面・低いfps）。キーフレームだけ読む経路と、メモリの確認に使う"""
    _ff("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5", "-t", str(minutes * 60), "-c:v", "libx264",
        "-preset", "ultrafast", "-g", "10", "-pix_fmt", "yuv420p", str(path))
    return path
