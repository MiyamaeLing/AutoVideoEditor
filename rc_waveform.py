"""タイムライン表示用の「音の波形」を、軽く取り出して保存（キャッシュ）しておく処理。

- FFmpeg で音声だけを 8000Hz・モノラルにして少しずつ読み、1秒あたり RATE 個の「山の高さ」にまとめる
  （3時間の素材でも、メモリに載るのは数十KBずつ。結果も 3時間で 約0.5MB）
- 結果は 出力フォルダ/waveform_cache/ に保存し、次からは一瞬で読み込む
  （素材のファイルの大きさ・更新日時が変わったら作り直す）
- 画面の幅に合わせて間引く処理（peaks_for_view）と、区間の計算もここに置く（Qt を使わないのでテストしやすい）
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import numpy as np

import rc_win  # noqa: F401  (Windowsで黒い画面を出さない)

RATE = 50            # 1秒あたりの点の数（20ミリ秒ごと）
SR = 8000            # 取り出すときのサンプリング周波数（波形の形を見るだけなので低くてよい）
BLOCK = SR // RATE   # 1点あたりのサンプル数
VERSION = 1
CACHE_DIR = "waveform_cache"


def cache_path(out_dir: Path, src: Path) -> Path:
    """素材ごとのキャッシュの場所。ファイルの場所・大きさ・更新日時が変われば別の名前になる"""
    src = Path(src)
    try:
        st = src.stat()
        stamp = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        stamp = "missing"
    key = f"v{VERSION}|{RATE}|{src.resolve()}|{stamp}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in src.stem)[:40]
    return Path(out_dir) / CACHE_DIR / f"{safe}_{h}.npy"


def load_cached(out_dir: Path, src: Path) -> np.ndarray | None:
    p = cache_path(out_dir, src)
    try:
        a = np.load(p, allow_pickle=False)
        return a if a.dtype == np.uint8 and a.ndim == 1 else None
    except Exception:  # noqa: BLE001  壊れていたら作り直す
        return None


def _to_u8(peaks: np.ndarray) -> np.ndarray:
    """0〜1 の山の高さを 0〜255 にする。小さい音も見えるように平方根で持ち上げる"""
    return np.clip(np.sqrt(np.clip(peaks, 0, 1)) * 255 + 0.5, 0, 255).astype(np.uint8)


def extract(src: Path, duration: float | None = None, progress=None, should_stop=None,
            chunk_sec: float = 20.0) -> np.ndarray | None:
    """素材の音声から波形（uint8 の配列、1秒あたり RATE 個）を作る。

    progress(途中までの配列) を時々呼ぶ。should_stop() が True を返したら中止して None を返す。
    """
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    n_est = int((duration or 0) * RATE) + RATE
    out = np.zeros(max(n_est, RATE), dtype=np.uint8)
    n = 0
    rest = np.zeros(0, dtype=np.int16)
    read_bytes = int(chunk_sec * SR) * 2
    try:
        while True:
            if should_stop and should_stop():
                return None
            buf = proc.stdout.read(read_bytes)
            if not buf:
                break
            if len(buf) % 2:
                buf = buf[:-1]
            x = np.frombuffer(buf, dtype="<i2")
            if rest.size:
                x = np.concatenate([rest, x])
            k = x.size // BLOCK
            rest = x[k * BLOCK:].copy()
            if k:
                blk = np.abs(x[:k * BLOCK].reshape(k, BLOCK).astype(np.int32)).max(axis=1) / 32768.0
                if n + k > out.size:
                    out = np.concatenate([out, np.zeros(max(k, out.size // 2), dtype=np.uint8)])
                out[n:n + k] = _to_u8(blk)
                n += k
                if progress:
                    progress(out[:n])
        if rest.size:
            if n + 1 > out.size:
                out = np.concatenate([out, np.zeros(1, dtype=np.uint8)])
            out[n] = _to_u8(np.array([np.abs(rest.astype(np.int32)).max() / 32768.0]))[0]
            n += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    if proc.returncode not in (0, None) and n == 0:
        return None
    return out[:n].copy()


def get(out_dir: Path, src: Path, duration: float | None = None, progress=None, should_stop=None) -> np.ndarray | None:
    """キャッシュがあれば読み込み、なければ作って保存する"""
    a = load_cached(out_dir, src)
    if a is not None:
        return a
    a = extract(src, duration, progress, should_stop)
    if a is None:
        return None
    p = cache_path(out_dir, src)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.stem + ".tmp.npy")
        np.save(tmp, a, allow_pickle=False)
        os.replace(tmp, p)
    except OSError:   # 書き込めないフォルダでも、表示はできるようにする
        pass
    return a


# ------------------------------------------------------------ 表示用の計算
def peaks_for_view(wave: np.ndarray, t0: float, sec_per_px: float, width: int) -> np.ndarray:
    """画面の1列（1ピクセル）ごとの山の高さ（0〜255）。範囲外は 0"""
    width = max(int(width), 0)
    res = np.zeros(width, dtype=np.uint8)
    if width == 0 or wave is None or wave.size == 0:
        return res
    edges = (t0 + sec_per_px * np.arange(width + 1)) * RATE
    lo = np.floor(edges[:-1]).astype(np.int64)
    hi = np.maximum(np.ceil(edges[1:]).astype(np.int64), lo + 1)
    ok = (hi > 0) & (lo < wave.size)
    if not ok.any():
        return res
    lo_c = np.clip(lo, 0, wave.size - 1)
    if sec_per_px * RATE <= 2:   # 拡大していると1列が1〜2点なので、そのまま拾う
        res[ok] = np.maximum(wave[lo_c[ok]], wave[np.clip(hi[ok] - 1, 0, wave.size - 1)])
        return res
    idx = lo_c[ok]
    # reduceat は増えていく位置が必要。同じ位置が続くことはない（1列が2点より大きい）
    vals = np.maximum.reduceat(wave, idx)
    # 最後の列は配列の終わりまでを含んでしまうので、その列の範囲だけで計算し直す
    last = np.flatnonzero(ok)[-1]
    vals[-1] = wave[lo_c[last]:min(hi[last], wave.size)].max(initial=0)
    res[ok] = vals
    return res


def merge(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """重なった区間をまとめる"""
    out: list[list[float]] = []
    for s, e in sorted((float(a), float(b)) for a, b in iv if b > a):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract(iv: list[tuple[float, float]], cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """区間 iv から 区間 cut を取り除く"""
    res = merge(iv)
    for cs, ce in merge(cut):
        nxt = []
        for s, e in res:
            if ce <= s or cs >= e:
                nxt.append((s, e))
                continue
            if s < cs:
                nxt.append((s, cs))
            if ce < e:
                nxt.append((ce, e))
        res = nxt
    return res


def keep_intervals(base_keep, cut_now: list[tuple[float, float]], keep_now: list[tuple[float, float]]):
    """荒編集で残した区間に、画面でのチェックの変更（未反映）を重ねる"""
    return subtract(merge(list(map(tuple, base_keep or [])) + list(keep_now)), cut_now)


def tick_step(sec_per_px: float, min_px: float = 80) -> float:
    """目盛りの間隔（秒）。文字が重ならない、きりのよい間隔を選ぶ"""
    for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
        if s / max(sec_per_px, 1e-9) >= min_px:
            return float(s)
    return 14400.0
