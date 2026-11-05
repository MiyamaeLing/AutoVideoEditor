"""2台目カメラ（スマホ等）を音声の波形で自動同期する。"""
from __future__ import annotations

import subprocess

import numpy as np

from rc_media import SourceSet

SR = 4000          # 同期用に読み込む音声のサンプリングレート
HOP = 40           # 10ms 単位のエンベロープ（音量の変化）で照合する


def _envelope(sources: SourceSet) -> np.ndarray:
    envs = []
    for f in sources.files:
        proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", str(f.path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            stdout=subprocess.PIPE,
        )
        parts, rest = [], np.zeros(0, dtype=np.float32)
        while True:
            buf = proc.stdout.read(SR * 2 * 60)  # 1分ずつ
            if not buf:
                break
            x = np.concatenate([rest, np.frombuffer(buf, dtype=np.int16).astype(np.float32)])
            n = len(x) // HOP
            blk = x[: n * HOP].reshape(n, HOP)
            parts.append(np.sqrt((blk ** 2).mean(axis=1) + 1.0))
            rest = x[n * HOP:]
        proc.wait()
        envs.append(np.concatenate(parts) if parts else np.zeros(0))
    e = np.log(np.concatenate(envs))
    # 1秒の移動平均を引いて、マイク感度の違いに左右されにくくする
    k = 100
    if len(e) > k:
        ma = np.convolve(e, np.ones(k) / k, mode="same")
        e = e - ma
    e = (e - e.mean()) / (e.std() + 1e-9)
    return e


def find_offset(main: SourceSet, cam2: SourceSet) -> tuple[float, float]:
    """cam2の時刻 = mainの時刻 + offset となる offset（秒）と、信頼度を返す。"""
    a = _envelope(main)
    b = _envelope(cam2)
    n = 1
    while n < len(a) + len(b):
        n *= 2
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    k = int(np.argmax(corr))
    # 正のずれ（2台目が後から撮り始めた）は 0〜len(a)-1、負のずれは n-len(b)+1〜n-1 の位置に出る。
    # n//2 で分けると、メインが長いときに正しいずれを負と取り違えるので、len(a) で分ける。
    if k >= len(a):
        k -= n
    # a[t + k] ≒ b[t]  →  main時刻 = cam2時刻 + k  →  cam2時刻 = main時刻 - k
    offset = -k * HOP / SR
    confidence = float((corr.max() - corr.mean()) / (corr.std() + 1e-9))
    return offset, confidence
