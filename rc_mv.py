"""MVモード：曲のビート（拍）に合わせて、映像素材をつないだMVのラフを作る。

1. 曲の解析   … テンポ・拍・小節の頭・サビ（盛り上がり）を numpy だけで求める
2. 素材の解析 … 動画を小さく・間引いて読み、動きの量・明るさ・くっきり度・色の鮮やかさを調べる
3. 並べ方     … おまかせ（動き・明るさで自動）／AI（Claudeが代表フレームを見て並べる）／撮った順
4. 書き出し   … MVラフの mp4 と、Premiere用XML（拍の位置にマーカー、素材は配置済み）

どの処理も、素材全体を一度にメモリへ載せない（3時間の素材でも動く）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from rc_media import MediaInfo, _parse_rate, snap_fps

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aif", ".aiff"}
VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".mkv", ".avi", ".mxf", ".webm", ".wmv", ".m4v", ".3gp"}

# 曲の解析
SR = 22050
N_FFT = 1024
HOP = 256
FR = SR / HOP                     # 解析のコマ数（1秒あたり 約86）
ENV_RATE = 10                     # 画面用の音量カーブ（1秒あたり10点）

# 素材の解析
AW, AH = 96, 54                   # 素材を調べるときの縮小サイズ
SAMPLE_FPS = 4                    # 短い素材は1秒に4枚調べる
KEYFRAME_ONLY_OVER = 15 * 60      # これより長い素材は、キーフレームだけ読んで速くする（秒）

SECTION_JA = {"intro": "イントロ", "verse": "Aメロ", "pre": "Bメロ（サビ前）", "chorus": "サビ",
              "inter": "間奏", "outro": "アウトロ"}
PACE_JA = {"fast": "細かめ", "normal": "ふつう", "slow": "ゆったり"}
ORDER_JA = {"auto": "おまかせ（動き・明るさ）", "ai": "AI（Claudeが場面を見て並べる）", "files": "撮った順"}

DEFAULTS = {
    "width": 1920, "height": 1080, "fps": "auto", "fit": "cover", "pace": "normal",
    "markers": "beats", "fade_out": 1.0, "min_cut": 0.3, "ai_frames": 36, "batch": 8,
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ============================================================
#  ffprobe
# ============================================================
def _ffprobe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        sys.exit(f"[エラー] ffprobe が {path} を読めませんでした:\n{p.stderr.strip()[-600:]}")
    return json.loads(p.stdout or "{}")


def probe_video(path: Path) -> tuple[MediaInfo, str]:
    """映像素材の情報（音声トラックがなくてもよい）と、撮影日時の文字列（分からなければ空）を返す"""
    info = _ffprobe(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    if v is None:
        sys.exit(f"[エラー] {path.name} に映像がありません。")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rate = _parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate")) or Fraction(30)
    w, h = int(v.get("width", 1920)), int(v.get("height", 1080))
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    tags = {**(info.get("format", {}).get("tags") or {}), **(v.get("tags") or {})}
    shot = str(tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate") or "")
    mi = MediaInfo(path=path.resolve(), duration=dur, fps=snap_fps(rate), width=w, height=h,
                   audio_channels=int(a.get("channels", 2)) if a else 0,
                   sample_rate=int(a.get("sample_rate", 48000)) if a else 48000)
    return mi, shot


def probe_song(path: Path) -> dict:
    info = _ffprobe(path)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if a is None:
        sys.exit(f"[エラー] {path.name} に音声がありません。曲のファイル（mp3・wav など）を指定してください。")
    return {"duration": float(info.get("format", {}).get("duration") or a.get("duration") or 0),
            "channels": max(1, min(int(a.get("channels", 2)), 2)), "sample_rate": int(a.get("sample_rate", 48000))}


def file_key(path: Path, extra: str = "") -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}|{extra}".encode("utf-8")).hexdigest()[:16]


def collect_materials(items: list[Path], exclude: Path | None = None) -> list[Path]:
    """ファイルとフォルダの一覧から、映像素材のファイルを集める（フォルダの中は名前の順）"""
    out: list[Path] = []
    ex = exclude.resolve() if exclude else None

    def natural(p: Path):
        return [int(s) if s.isdigit() else s.lower() for s in re.split(r"(\d+)", str(p))]
    for it in items:
        it = Path(it)
        if it.is_dir():
            found = []
            for p in it.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
                    continue
                rp = p.resolve()
                if ex and (rp == ex or ex in rp.parents):
                    continue
                # 作業用のフォルダかどうかは、指定したフォルダの中だけで判断する
                # （D:\work\MV素材 のように、上の階層の名前で全部はずれてしまわないように）
                rel = p.relative_to(it).parts[:-1]
                if any(part.endswith(("_mv", "_roughcut")) or part in ("work", "_mv_work") for part in rel):
                    continue
                found.append(p)
            out += sorted(found, key=natural)
        elif it.is_file():
            out.append(it)
        else:
            sys.exit(f"[エラー] ファイルが見つかりません: {it}")
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


# ============================================================
#  1. 曲の解析
# ============================================================
def _filters():
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    # 40本の帯域（対数の間隔）
    edges = np.geomspace(30, SR / 2 * 0.95, 41)
    bands = np.zeros((40, len(freqs)), dtype=np.float32)
    for i in range(40):
        m = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if not m.any():
            m[np.argmin(np.abs(freqs - (edges[i] + edges[i + 1]) / 2))] = True
        bands[i, m] = 1.0 / m.sum()
    # 音の高さ（ドレミ…の12音）
    chroma = np.zeros((12, len(freqs)), dtype=np.float32)
    ok = (freqs >= 80) & (freqs <= 5000)
    pc = np.mod(np.round(12 * np.log2(np.maximum(freqs[ok], 1) / 440.0)) + 9, 12).astype(int)
    chroma[pc, np.nonzero(ok)[0]] = 1.0
    centers = (edges[:-1] + edges[1:]) / 2
    return bands, chroma, np.nonzero(centers <= 160)[0], np.nonzero(centers <= 3000)[0]


def _decode_audio(path: Path):
    """曲をモノラル 22050Hz に変換しながら、少しずつ返す（全体をメモリに載せない）"""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SR),
           "-f", "f32le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err: list[bytes] = []
    th = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    th.start()
    try:
        while True:
            b = proc.stdout.read(HOP * 1024 * 4)
            if not b:
                break
            yield np.frombuffer(b[: len(b) // 4 * 4], dtype="<f4")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    th.join(5)
    if proc.returncode != 0:
        sys.exit(f"[エラー] 曲を読み込めませんでした: {path.name}\n{(b''.join(err)).decode('utf-8', 'replace')[-500:]}")


def audio_features(path: Path) -> dict:
    """1コマ（約11.6ms）ごとの特徴: 音の立ち上がり（全体・低音）、音量、12音、8帯域の音色"""
    bands, chroma_m, low_bands, mid_bands = _filters()
    win = np.hanning(N_FFT).astype(np.float32)
    buf = np.zeros(N_FFT // 2, dtype=np.float32)   # 先頭は無音で埋めて、0秒のコマも作る
    prev = None
    out = {"onset": [], "low": [], "mid": [], "rms": [], "chroma": [], "timbre": []}
    total = 0
    for block in _decode_audio(path):
        total += len(block)
        buf = np.concatenate([buf, block])
        n = (len(buf) - N_FFT) // HOP + 1
        if n <= 0:
            continue
        fr = np.lib.stride_tricks.sliding_window_view(buf, N_FFT)[::HOP][:n]
        spec = np.abs(np.fft.rfft(fr * win, axis=1)).astype(np.float32)
        be = spec ** 2 @ bands.T                                         # (n, 40)
        lb = np.log1p(1e3 * be)
        p = lb[:1] if prev is None else prev
        d = np.diff(np.vstack([p, lb]), axis=0)
        d = np.maximum(d, 0)
        out["onset"].append(d.sum(1))
        out["low"].append(d[:, low_bands].sum(1))
        out["mid"].append(d[:, mid_bands].sum(1))
        out["rms"].append(np.sqrt(np.mean(fr ** 2, axis=1)))
        c = spec ** 2 @ chroma_m.T
        out["chroma"].append(c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-9))
        out["timbre"].append(np.log1p(1e3 * be.reshape(n, 8, 5).mean(2)))
        prev = lb[-1:]
        buf = buf[n * HOP:]
    if total < SR:
        sys.exit("[エラー] 曲が短すぎます（1秒未満）。")
    res = {k: np.concatenate(v).astype(np.float32) for k, v in out.items()}
    res["duration"] = total / SR
    return res


def estimate_tempo(onset: np.ndarray, bpm_min=60.0, bpm_max=200.0, center=120.0) -> float:
    o = onset - np.convolve(onset, np.ones(int(FR)) / int(FR), mode="same")
    o = np.maximum(o, 0)
    n = len(o)
    f = np.fft.rfft(o, 2 * n)
    ac = np.fft.irfft(np.abs(f) ** 2)[:n]
    ac = ac / (ac[0] + 1e-9)
    lo, hi = int(60 * FR / bpm_max), min(int(60 * FR / bpm_min) + 1, n // 2)
    lags = np.arange(max(lo, 2), hi)
    if len(lags) < 3:
        return center
    # 倍・半分のテンポも少し足して、拍の間隔として一番自然なものを選ぶ
    score = ac[lags].copy()
    two = lags * 2
    score += 0.5 * np.where(two < n, ac[np.minimum(two, n - 1)], 0)
    bpms = 60 * FR / lags
    score *= np.exp(-0.5 * (np.log2(bpms / center) / 0.9) ** 2)
    k = int(np.argmax(score))
    lag = float(lags[k])
    if 0 < k < len(lags) - 1:   # なめらかに補間して、コマの間の値まで求める
        a, b, c = score[k - 1], score[k], score[k + 1]
        den = a - 2 * b + c
        if abs(den) > 1e-12:
            lag += 0.5 * (a - c) / den
    return 60 * FR / lag


def track_beats(onset: np.ndarray, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """テンポに沿って、音の立ち上がりが強い所を拍として選ぶ（動的計画法）。拍のコマ番号を返す"""
    period = 60 * FR / bpm
    o = onset / (onset.std() + 1e-9)
    g = np.exp(-0.5 * (np.arange(-int(period), int(period) + 1) * 32 / period) ** 2)
    loc = np.convolve(o, g, mode="same")
    n = len(loc)
    lo, hi = max(1, int(round(period / 2))), int(round(period * 2))
    d = np.arange(lo, hi + 1)
    tx = -tightness * np.log(d / period) ** 2
    cum = loc.copy()
    back = np.full(n, -1, dtype=np.int64)
    for t in range(lo, n):
        a = t - hi
        if a < 0:
            dd, txx = d[: t - lo + 1], tx[: t - lo + 1]
        else:
            dd, txx = d, tx
        s = cum[t - dd] + txx
        k = int(np.argmax(s))
        if s[k] > 0:
            cum[t] = loc[t] + s[k]
            back[t] = t - dd[k]
    # 最後の拍：終わりの2拍分の中で、点数が一番高いところ
    tail = max(0, n - int(period * 2))
    seg = cum[tail:]
    # 局所的に大きい所に限る
    peaks = [i for i in range(len(seg)) if seg[i] >= seg[max(0, i - 1)] and seg[i] >= seg[min(len(seg) - 1, i + 1)]]
    t = tail + (max(peaks, key=lambda i: seg[i]) if peaks else int(np.argmax(seg)))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    return np.array(beats[::-1], dtype=np.int64)


def _fit_tempo(beat_t: np.ndarray) -> float | None:
    """拍の時刻に直線を当てはめて、コマの細かさより正確なテンポを求める（外れた拍は除く）"""
    if len(beat_t) < 8:
        return None
    idx = np.arange(len(beat_t))
    d = np.diff(beat_t)
    med = np.median(d)
    if med <= 0:
        return None
    # 間隔が大きく外れていない、いちばん長い連続部分で求める
    ok = np.abs(d - med) < med * 0.25
    best, cur, start, bs = 0, 0, 0, 0
    for i, v in enumerate(ok):
        cur = cur + 1 if v else 0
        if cur == 1:
            start = i
        if cur > best:
            best, bs = cur, start
    if best < 6:
        return float(60 / med)
    sl = slice(bs, bs + best + 1)
    slope = np.polyfit(idx[sl], beat_t[sl], 1)[0]
    return float(60 / slope)


def _double_check(beats: np.ndarray, mid: np.ndarray) -> bool:
    """拍と拍のちょうど間の音が、拍と同じくらい強いか（強ければ、本当のテンポは倍）"""
    w = 2
    on = np.array([mid[max(0, b - w): b + w + 1].max() for b in beats])
    mids = ((beats[:-1] + beats[1:]) // 2).astype(int)
    off = np.array([mid[max(0, b - w): b + w + 1].max() for b in mids])
    return float(np.median(off)) > 0.6 * float(np.median(on))


def _trim_silent_beats(beats: np.ndarray, rms: np.ndarray) -> np.ndarray:
    """曲の前後の無音にある拍を取り除く"""
    if len(beats) == 0:
        return beats
    ref = np.percentile(rms, 95) + 1e-9
    w = int(FR * 0.2)
    loud = np.array([rms[max(0, b - w): b + w + 1].max() for b in beats]) / ref
    on = np.nonzero(loud > 0.03)[0]
    if len(on) == 0:
        return beats
    return beats[on[0]: on[-1] + 1]


def _bar_phase(beats: np.ndarray, low: np.ndarray, chroma: np.ndarray, bpb: int) -> int:
    """小節の頭（1拍目）がどの拍かを決める。低音の立ち上がりと、和音の変わり目が多い位置"""
    if len(beats) < bpb * 2:
        return 0
    w = 2
    lv = np.array([low[max(0, b - w): b + w + 1].max() for b in beats])
    chg = np.zeros(len(beats))
    for i in range(1, len(beats) - 1):
        a = chroma[beats[i - 1]: beats[i]].mean(0)
        b = chroma[beats[i]: beats[i + 1]].mean(0)
        chg[i] = 1 - float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    lv /= lv.mean() + 1e-9
    chg /= chg.mean() + 1e-9
    sc = [lv[k::bpb].mean() + chg[k::bpb].mean() for k in range(bpb)]
    return int(np.argmax(sc))


def _checker(L: int) -> np.ndarray:
    x = np.arange(-L, L) + 0.5
    g = np.exp(-0.5 * (x / (L * 0.6)) ** 2)
    k = np.outer(g, g) * np.outer(np.sign(x), np.sign(x))
    return k / np.abs(k).sum()


def find_sections(bar_feat: dict, first_bar: int = 0) -> tuple[list[tuple[int, int]], np.ndarray]:
    """小節ごとの特徴から、曲の区切り（イントロ・Aメロ・サビ…）を見つける。[(開始小節, 終了小節)] を返す"""
    E = bar_feat["energy"]
    nb = len(E)
    if nb < 8:
        return [(0, nb)], np.zeros((nb, nb))
    ch = bar_feat["chroma"]
    tb = bar_feat["timbre"]
    tz = (tb - tb.mean(0)) / (tb.std(0) + 1e-6)
    tz /= np.linalg.norm(tz, axis=1, keepdims=True) + 1e-9
    chn = ch / (np.linalg.norm(ch, axis=1, keepdims=True) + 1e-9)
    S = 0.5 * (chn @ chn.T) + 0.5 * (tz @ tz.T)
    L = 4
    K = _checker(L)
    pad = np.pad(S, L, mode="edge")
    nov = np.array([float((K * pad[i: i + 2 * L, i: i + 2 * L]).sum()) for i in range(nb)])
    ez = (E - E.mean()) / (E.std() + 1e-6)
    en = np.array([abs(ez[i: i + 4].mean() - ez[max(0, i - 4): i].mean()) if i >= 1 else 0 for i in range(nb)])

    def z(x):
        return (x - x.mean()) / (x.std() + 1e-9)
    score = z(nov) + z(en)
    # 4小節・8小節の切れ目は、区切りになりやすい
    rel = np.arange(nb) - first_bar
    score += 0.6 * (rel % 4 == 0) + 0.4 * (rel % 8 == 0)
    cands = [i for i in range(2, nb - 2)
             if score[i] >= score[i - 1] and score[i] >= score[i + 1] and score[i] > 0.8]
    cands.sort(key=lambda i: -score[i])
    chosen: list[int] = []
    for i in cands:
        # 曲の最初・最後の区間（イントロ・アウトロ）は2小節でもよい。それ以外は4小節以上
        if all(abs(i - c) >= 4 for c in chosen):
            chosen.append(i)
        if len(chosen) >= nb // 4:
            break
    bounds = [0] + sorted(chosen) + [nb]
    return [(a, b) for a, b in zip(bounds, bounds[1:])], S


def label_sections(secs: list[tuple[int, int]], energy_db: np.ndarray, S: np.ndarray) -> list[dict]:
    loud = np.array([energy_db[a:b].mean() for a, b in secs])
    lo, hi = float(loud.min()), float(loud.max())
    norm = (loud - lo) / (hi - lo) if hi - lo > 1.5 else np.full(len(secs), 0.5)
    rep = np.zeros(len(secs))
    if S.size:
        for i, (a, b) in enumerate(secs):
            best = 0.0
            for j, (c, d) in enumerate(secs):
                if i == j:
                    continue
                n = min(b - a, d - c)
                if n >= 2:
                    best = max(best, float(np.mean([S[a + k, c + k] for k in range(n)])))
            rep[i] = best
    rn = (rep - rep.min()) / (rep.max() - rep.min() + 1e-9) if len(secs) > 1 else rep
    out = []
    for i, (a, b) in enumerate(secs):
        sc = 0.75 * norm[i] + 0.25 * rn[i]
        is_ch = len(secs) > 1 and norm[i] >= 0.6 and sc >= 0.6 and loud[i] >= hi - 4.0
        out.append({"bar0": a, "bar1": b, "energy": round(float(norm[i]), 3), "loud_db": round(float(loud[i]), 1),
                    "repeat": round(float(rep[i]), 3), "kind": "chorus" if is_ch else ""})
    if len(out) > 1 and not any(s["kind"] for s in out):
        max(out, key=lambda s: s["energy"])["kind"] = "chorus"
    for i, s in enumerate(out):
        if s["kind"]:
            continue
        nxt = out[i + 1] if i + 1 < len(out) else None
        if i == 0 and s["energy"] < 0.5:
            s["kind"] = "intro"
        elif nxt is None and s["energy"] < 0.5:
            s["kind"] = "outro"
        elif s["energy"] < 0.25:
            s["kind"] = "inter"
        elif nxt is not None and nxt["kind"] == "chorus" and i > 0 and out[i - 1]["kind"] in ("verse", "pre"):
            s["kind"] = "pre"   # Aメロのあと、サビの直前の区間
        else:
            s["kind"] = "verse"
    for s in out:
        s["label"] = SECTION_JA[s["kind"]]
    return out


def analyze_song(path: Path, bpm: float | None = None, beats_per_bar: int = 4) -> dict:
    feat = audio_features(path)
    onset, rms = feat["onset"], feat["rms"]
    tempo = float(bpm) if bpm else estimate_tempo(onset)
    beats = track_beats(onset, tempo)
    if not bpm and len(beats) > 8:
        # 倍のテンポの方が正しいこともある（速い曲で、拍の間にも強い音がある＝裏拍ではなく本当の拍）
        if tempo * 2 <= 210 and _double_check(beats, feat["mid"]):
            tempo *= 2
            beats = track_beats(onset, tempo)
        # 拍の間隔から、テンポを細かく求め直して、もう一度
        tempo = _fit_tempo(beats * HOP / SR) or tempo
        beats = track_beats(onset, tempo)
    beats = _trim_silent_beats(beats, rms)
    if len(beats) < 2:
        sys.exit("[エラー] 拍を見つけられませんでした。--bpm でテンポを指定してください。")
    phase = _bar_phase(beats, feat["low"], feat["chroma"], beats_per_bar)
    beat_t = (beats * HOP) / SR                 # 窓の中心＝コマ番号×HOP（先頭を半窓ぶん無音で埋めたため）
    tempo = _fit_tempo(beat_t) or tempo
    down_idx = list(range(phase, len(beats), beats_per_bar))
    # 小節の特徴（1拍目〜次の1拍目）。最初の1拍目より前（弱起）は、1つの小節として扱う
    dframes = [int(beats[i]) for i in down_idx]
    pick = 1 if phase > 0 else 0
    starts = [0] + (dframes if pick else dframes[1:]) + [len(onset)]
    db = 20 * np.log10(rms + 1e-6)
    bars = {"energy": [], "chroma": [], "timbre": []}
    for a, b in zip(starts, starts[1:]):
        b = max(b, a + 1)
        bars["energy"].append(float(db[a:b].mean()))
        bars["chroma"].append(feat["chroma"][a:b].mean(0))
        bars["timbre"].append(feat["timbre"][a:b].mean(0))
    bf = {k: np.array(v, dtype=np.float32) for k, v in bars.items()}
    bar_times = [float(s * HOP / SR) for s in starts[:-1]]
    # 区切りは、弱起のぶんを除いた小節で求める
    secs, S = find_sections({k: v[pick:] for k, v in bf.items()}, 0)
    secs = [(a + pick, b + pick) for a, b in secs]
    if pick:
        secs[0] = (0, secs[0][1])
    sections = label_sections(secs, bf["energy"], S if pick == 0 else np.pad(S, ((1, 0), (1, 0)), mode="edge"))
    dur = float(feat["duration"])
    for s in sections:
        s["start"] = round(bar_times[s["bar0"]] if s["bar0"] > 0 else 0.0, 3)
        s["end"] = round(bar_times[s["bar1"]] if s["bar1"] < len(bar_times) else dur, 3)
    # 盛り上がり度（小節ごと、0〜1）
    e = bf["energy"]
    k = np.ones(3) / 3
    sm = np.convolve(np.pad(e, 1, mode="edge"), k, mode="valid")
    hype = (sm - sm.min()) / (sm.max() - sm.min() + 1e-9)
    # 画面用の音量カーブ
    step = int(FR / ENV_RATE)
    n_env = len(db) // step
    env = db[: n_env * step].reshape(n_env, step).max(1) if n_env else db
    ch = [s for s in sections if s["kind"] == "chorus"]
    best = max(ch, key=lambda s: (s["energy"], s["start"])) if ch else max(sections, key=lambda s: s["energy"])
    return {
        "song": str(path.resolve()), "key": file_key(path, f"{bpm}|{beats_per_bar}|v1"), "duration": round(dur, 3),
        "bpm": round(tempo, 2), "beats_per_bar": beats_per_bar,
        "beats": [round(float(t), 4) for t in beat_t],
        "downbeats": [round(float(beat_t[i]), 4) for i in down_idx],
        "bars": [round(t, 4) for t in bar_times],
        "sections": sections,
        "hype": [round(float(x), 3) for x in hype],
        "env_rate": ENV_RATE, "env": [round(float(x), 1) for x in env],
        "best_chorus": {"start": best["start"], "end": best["end"]},
    }


# ============================================================
#  2. 素材の解析
# ============================================================
@dataclass
class Clip:
    path: str
    duration: float
    fps: str
    width: int
    height: int
    has_audio: bool
    shot: str
    t: list[float] = field(default_factory=list)       # 調べた時刻
    motion: list[float] = field(default_factory=list)  # 動きの量（0〜1）
    bright: list[float] = field(default_factory=list)  # 明るさ（0〜1）
    sharp: list[float] = field(default_factory=list)   # くっきり度
    color: list[float] = field(default_factory=list)   # 色の鮮やかさ
    cuts: list[float] = field(default_factory=list)    # 素材の中の場面の切り替わり


def _frame_stats(fr: np.ndarray):
    """fr: (n, AH, AW, 3) uint8 → 明るさ・くっきり度・鮮やかさ・グレー画像"""
    f = fr.astype(np.float32) / 255.0
    g = f @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    bright = g.mean((1, 2))
    lap = 4 * g[:, 1:-1, 1:-1] - g[:, :-2, 1:-1] - g[:, 2:, 1:-1] - g[:, 1:-1, :-2] - g[:, 1:-1, 2:]
    sharp = lap.reshape(len(f), -1).var(1) * 1000
    rg = f[..., 0] - f[..., 1]
    yb = 0.5 * (f[..., 0] + f[..., 1]) - f[..., 2]
    col = np.sqrt(rg.reshape(len(f), -1).std(1) ** 2 + yb.reshape(len(f), -1).std(1) ** 2) \
        + 0.3 * np.sqrt(rg.reshape(len(f), -1).mean(1) ** 2 + yb.reshape(len(f), -1).mean(1) ** 2)
    return bright, sharp, col, g


def analyze_clip(path: Path) -> Clip:
    """素材を小さく・間引いて読み、時刻ごとの動き・明るさなどを調べる（メモリはほとんど使わない）"""
    mi, shot = probe_video(path)
    key_only = mi.duration > KEYFRAME_ONLY_OVER
    vf = ("" if key_only else f"fps={SAMPLE_FPS},") + f"scale={AW}:{AH}:flags=area,format=rgb24,showinfo"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-v", "info"]
    if key_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", str(path), "-map", "0:v:0", "-an", "-sn", "-vf", vf, "-fps_mode", "passthrough",
            "-f", "rawvideo", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    times: list[float] = []
    tail: list[str] = []
    pat = re.compile(rb"pts_time:\s*([-0-9.]+)")

    def read_err():
        for line in proc.stderr:
            m = pat.search(line)
            if m:
                try:
                    times.append(float(m.group(1)))
                except ValueError:
                    pass
            else:
                tail.append(line.decode("utf-8", "replace").rstrip())
                del tail[:-20]
    th = threading.Thread(target=read_err, daemon=True)
    th.start()
    fsz = AW * AH * 3
    B, M, S, C = [], [], [], []
    prev = None
    try:
        while True:
            b = proc.stdout.read(fsz * 64)
            if not b:
                break
            n = len(b) // fsz
            if n == 0:
                continue
            fr = np.frombuffer(b[: n * fsz], dtype=np.uint8).reshape(n, AH, AW, 3)
            br, sh, co, g = _frame_stats(fr)
            allg = g if prev is None else np.concatenate([prev[None], g])
            mo = np.abs(np.diff(allg, axis=0)).mean((1, 2))
            if prev is None:
                mo = np.concatenate([[np.nan], mo])
            prev = g[-1]
            B += br.tolist()
            S += sh.tolist()
            C += co.tolist()
            M += mo.tolist()
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    th.join(10)
    if proc.returncode != 0 or not B:
        sys.exit(f"[エラー] 素材を読み込めませんでした: {path.name}\n" + "\n".join(tail[-6:]))
    n = len(B)
    t = np.array(times[:n] if len(times) >= n else
                 times + [(times[-1] if times else 0) + (k + 1) / SAMPLE_FPS for k in range(n - len(times))])
    mo = np.array(M, dtype=np.float64)
    if n > 1:
        mo[0] = mo[1]
        dt = np.diff(t, prepend=t[0] - 1 / SAMPLE_FPS)
        # キーフレームだけ読んだときは間隔が広いので、1/4秒あたりの動きにそろえる
        mo = mo * np.sqrt(np.clip(0.25 / np.maximum(dt, 1e-3), 0.05, 1.0))
    else:
        mo[:] = 0
    # 素材の中の場面の切り替わり（急に絵が変わった所）
    cuts = []
    for i in range(1, n):
        local = np.median(mo[max(0, i - 8): i + 8])
        if mo[i] > 0.12 and mo[i] > 4 * local:
            cuts.append(float((t[i - 1] + t[i]) / 2))
            mo[i] = local     # 切り替わりそのものは「動き」に数えない
    r = lambda a, k=4: [round(float(x), k) for x in a]  # noqa: E731
    return Clip(path=str(mi.path), duration=round(mi.duration, 3), fps=f"{mi.fps.numerator}/{mi.fps.denominator}",
                width=mi.width, height=mi.height, has_audio=mi.audio_channels > 0, shot=shot,
                t=r(t, 3), motion=r(mo), bright=r(B), sharp=r(S, 2), color=r(C), cuts=r(cuts, 3))


def analyze_clips(paths: list[Path], cache_file: Path) -> list[Clip]:
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    out = []
    for i, p in enumerate(paths, 1):
        k = file_key(p, f"{AW}x{AH}|{SAMPLE_FPS}|{KEYFRAME_ONLY_OVER}|v1")
        pct = int(i / len(paths) * 100)
        if k in cache:
            log(f"  {p.name}: 前回の解析結果を使います ({pct}%)")
            out.append(Clip(**cache[k]))
            continue
        log(f"  {p.name} を調べています… {i}/{len(paths)} ({pct}%)")
        c = analyze_clip(p)
        cache[k] = asdict(c)
        out.append(c)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return out


@dataclass
class Window:
    """素材の中の「使える区間」。カットはこの中から切り出す"""
    no: int
    clip: int
    s: float
    e: float
    motion: float
    bright: float
    sharp: float
    color: float
    mrank: float = 0.0
    brank: float = 0.0
    q: float = 0.0
    uses: int = 0
    used: list = field(default_factory=list)   # 使った範囲 [(開始, 終了)]
    bad: bool = False    # 暗すぎる・白飛びしている（ほかに素材があれば使わない）

    @property
    def length(self) -> float:
        return self.e - self.s


def build_windows(clips: list[Clip], win_len: float) -> list[Window]:
    wins: list[Window] = []
    for ci, c in enumerate(clips):
        t = np.array(c.t)
        if len(t) == 0:
            continue
        head, tail = min(0.4, c.duration * 0.1), min(0.25, c.duration * 0.05)
        bounds = [head] + [x for x in c.cuts if head < x < c.duration - tail] + [c.duration - tail]
        for a, b in zip(bounds, bounds[1:]):
            if b - a < 0.25:
                continue
            n = max(1, int((b - a) // win_len))
            starts = [a + k * win_len for k in range(n)]
            rest = b - (a + n * win_len)
            pieces = [(s, s + win_len) for s in starts] if b - a >= win_len else []
            if rest >= 1.0 or not pieces:
                pieces.append((b - rest if pieces else a, b))
            for s, e in pieces:
                m = (t >= s - 0.13) & (t <= e + 0.13)
                if not m.any():
                    m = np.abs(t - (s + e) / 2) == np.abs(t - (s + e) / 2).min()
                wins.append(Window(len(wins) + 1, ci, round(s, 3), round(e, 3),
                                   float(np.mean(np.array(c.motion)[m])), float(np.mean(np.array(c.bright)[m])),
                                   float(np.median(np.array(c.sharp)[m])), float(np.mean(np.array(c.color)[m]))))
    if not wins:
        sys.exit("[エラー] 使える素材の区間がありません（素材が短すぎます）。")

    def rank(vals):
        v = np.array(vals)
        return (np.argsort(np.argsort(v)) / max(len(v) - 1, 1)).tolist()
    mr, br = rank([w.motion for w in wins]), rank([w.bright for w in wins])
    ms = float(np.median([w.sharp for w in wins])) + 1e-6
    mc = float(np.median([w.color for w in wins])) + 1e-6
    for w, a, b in zip(wins, mr, br):
        w.mrank, w.brank = a, b
        bq = float(np.interp(w.bright, [0.04, 0.16, 0.85, 0.98], [0.0, 1.0, 1.0, 0.3]))
        sq = min(w.sharp / ms, 1.5) / 1.5
        cq = min(w.color / mc, 1.5) / 1.5
        w.q = round((0.5 * bq + 0.3 * sq + 0.2 * cq) * (0.25 if bq < 0.2 else 1.0), 3)
        w.bad = bq < 0.2
    return wins


# ============================================================
#  3. カットの計画
# ============================================================
@dataclass
class Cut:
    t0: float            # 曲の中の時刻（使う範囲の頭からの秒）
    t1: float
    section: int
    beats: int
    energy: float
    kind: str
    f0: int = 0          # シーケンス上のフレーム
    f1: int = 0
    clip: int = -1
    src_in: float = 0.0  # 素材の中の開始秒
    window: int = 0
    why: str = ""


def song_range(an: dict, part: str, start: float | None, end: float | None) -> tuple[float, float]:
    dur = an["duration"]
    if part == "sabi":
        bc = an["best_chorus"]
        a, b = bc["start"], bc["end"]
        # 1小節前から入ると、サビの頭が気持ちよく決まる
        prev = [t for t in an["bars"] if t < a - 0.05]
        a = prev[-1] if prev else a
    else:
        a, b = 0.0, dur
    if start is not None:
        a = max(0.0, float(start))
    if end is not None and end > 0:
        b = min(dur, float(end))
    if b - a < 1.0:
        sys.exit("[エラー] 使う範囲が短すぎます（1秒以上にしてください）。")
    return round(a, 3), round(b, 3)


def plan_cuts(an: dict, a: float, b: float, pace: str, fps: Fraction, min_cut: float = 0.3) -> list[Cut]:
    """拍に合わせて、カットの長さと位置を決める。盛り上がる所ほど短く切り替える"""
    bpb = an["beats_per_bar"]
    secs = an["sections"]
    down = {round(t, 4) for t in an["downbeats"]}
    beats = [t for t in an["beats"] if a + 0.05 < t < b - 0.05]
    beat_len = 60 / an["bpm"]

    def sec_of(t):
        for i, s in enumerate(secs):
            if s["start"] - 1e-3 <= t < s["end"] - 1e-3:
                return i
        return len(secs) - 1

    def length(si):
        s = secs[si]
        if s["kind"] == "chorus":
            n = 2            # サビは2拍ごとに切り替える
        elif s["kind"] in ("intro", "outro", "inter") and s["energy"] < 0.6:
            n = 8            # 静かな所は2小節
        else:
            n = 4            # Aメロ・Bメロは1小節
        if pace == "fast":
            n = max(1, n // 2)
        elif pace == "slow":
            n = min(16, n * 2)
        return n

    bounds: list[float] = [a]
    if beats:
        is_down = [round(t, 4) in down for t in beats]
        sec_idx = [sec_of(t) for t in beats]
        # 最初のカットは、最初の小節の頭（近くになければ最初の拍）まで
        j = next((i for i, d in enumerate(is_down) if d), 0)
        if beats[j] - a > 8 * beat_len:
            j = 0
        if beats[j] - a >= min_cut:
            bounds.append(beats[j])
        while True:
            si = sec_idx[j]
            n = length(si)
            nxt = next((k for k in range(j + 1, len(beats)) if sec_idx[k] != si), len(beats))
            k = min(j + n, nxt)
            if n >= bpb and k < nxt:   # 長めのカットは小節の頭でそろえる
                kk = k
                while kk > j + 1 and not is_down[kk]:
                    kk -= 1
                if is_down[kk]:
                    k = kk
            # サビの直前の1小節は、1拍ずつたたみかける
            if pace != "slow" and nxt < len(beats) and secs[sec_idx[nxt]]["kind"] == "chorus" \
                    and secs[si]["kind"] != "chorus":
                roll = nxt - bpb
                if j < roll:
                    k = min(k, roll)
                else:
                    k = j + 1
            if k >= len(beats):
                break
            bounds.append(beats[k])
            j = k
    bounds.append(b)
    # 短すぎるカットは前とまとめる
    clean = [bounds[0]]
    for t in bounds[1:-1]:
        if t - clean[-1] >= min_cut:
            clean.append(t)
    if bounds[-1] - clean[-1] < min_cut and len(clean) > 1:
        clean[-1] = bounds[-1]
    else:
        clean.append(bounds[-1])
    beat_arr = np.array(an["beats"] or [0.0])
    cuts = []
    for t0, t1 in zip(clean, clean[1:]):
        si = sec_of(t0 + 1e-3)
        nb = int(((beat_arr >= t0 - 0.01) & (beat_arr < t1 - 0.01)).sum())
        c = Cut(round(t0 - a, 4), round(t1 - a, 4), si, nb, secs[si]["energy"], secs[si]["kind"])
        c.f0, c.f1 = int(round(c.t0 * fps)), int(round(c.t1 * fps))
        if c.f1 > c.f0:
            cuts.append(c)
    return cuts


def _offset(w: Window, d: float) -> float:
    """区間の中で、まだ使っていない所を選ぶ（はじめは真ん中）"""
    room = w.length - d
    if room <= 0:
        return w.s
    mid = (w.s + w.e) / 2
    cands = np.linspace(w.s, w.e - d, max(2, int(room / max(d * 0.5, 0.1)) + 1))

    def overlap(x):
        return sum(max(0.0, min(x + d, e) - max(x, s)) for s, e in w.used)
    return round(float(min(cands, key=lambda x: (round(overlap(x), 3), abs(x + d / 2 - mid)))), 3)


def _take(c: Cut, w: Window, why: str, src: float | None = None) -> None:
    d = c.t1 - c.t0
    c.clip, c.window = w.clip, w.no
    c.src_in = _offset(w, d) if src is None else round(src, 3)
    c.why = why
    w.uses += 1
    w.used.append((c.src_in, c.src_in + d))


def _used_frac(w: Window) -> float:
    return min(1.0, sum(e - s for s, e in w.used) / max(w.length, 1e-3))


def assign_auto(cuts: list[Cut], wins: list[Window], seed: int, avoid: set[int] = frozenset(),
                only: list[int] | None = None) -> None:
    rng = np.random.default_rng(seed)
    hist: list[Window] = []
    by_no = {w.no: w for w in wins}
    pool = [w for w in wins if w.no not in avoid and not w.bad] or [w for w in wins if not w.bad] or wins
    first_in_sec = set()
    seen_sec = set()
    for i, c in enumerate(cuts):
        if c.section not in seen_sec:
            seen_sec.add(c.section)
            first_in_sec.add(i)
    for i, c in enumerate(cuts):
        if only is not None and i not in only:
            if c.window:
                hist.append(by_no[c.window])
            continue
        d = c.t1 - c.t0
        cand = [w for w in pool if w.length >= d - 1e-3] or pool
        target = 0.1 + 0.8 * c.energy
        prev = hist[-1] if hist else None
        recent = {w.no for w in hist[-4:]}
        best, best_cost = None, 1e9
        jit = rng.random(len(cand)) * 0.15
        for w, jj in zip(cand, jit):
            fr = _used_frac(w)
            cost = 1.2 * abs(w.mrank - target) + 1.0 * (1 - w.q) + 1.2 * fr + (1.0 if fr > 0.9 else 0) + jj
            cost += 0.25 * abs(w.brank - (0.3 + 0.5 * c.energy))
            if prev is not None and w.clip == prev.clip:
                cost += 0.6 + (0.5 if abs(w.s - prev.e) < 3 * prev.length else 0)
            if len(hist) > 1 and w.clip == hist[-2].clip:
                cost += 0.3
            if w.no in recent:
                cost += 2.0
            if i in first_in_sec and c.kind == "chorus":
                cost -= 0.5 * w.q
            if cost < best_cost:
                best, best_cost = w, cost
        lvl = "大" if best.mrank > 0.66 else ("中" if best.mrank > 0.33 else "小")
        _take(c, best, f"自動（動き{lvl}・{SECTION_JA.get(c.kind, '')}）")
        hist.append(best)


def assign_files(cuts: list[Cut], wins: list[Window], clips: list[Clip]) -> None:
    """撮った順（撮影日時、なければ名前の順）に、素材の頭から終わりまでを曲の長さに割り振る"""
    import bisect
    order = sorted(range(len(clips)), key=lambda i: (clips[i].shot or "~", i))
    pos = {ci: k for k, ci in enumerate(order)}
    seq = sorted([w for w in wins if not w.bad] or wins, key=lambda w: (pos[w.clip], w.s))
    cum = np.concatenate([[0.0], np.cumsum([w.length for w in seq])])
    L, total = float(cum[-1]), max(cuts[-1].t1, 1e-3)
    idx = [min(len(seq) - 1, bisect.bisect_right(cum, c.t0 / total * L) - 1) for c in cuts]
    prev_w, prev_src = None, -1.0
    for i, c in enumerate(cuts):
        d = c.t1 - c.t0
        lo = idx[i]
        hi = max(idx[i + 1] if i + 1 < len(cuts) else len(seq), lo + 1)
        if prev_w is not None and seq.index(prev_w) >= lo:
            lo = seq.index(prev_w)   # 前のカットより前には戻らない
            hi = max(hi, lo + 1)
        group = [w for w in seq[lo:hi] if w.length >= d - 1e-3] or seq[lo:hi]
        w = max(group, key=lambda w: w.q)
        local = w.s + (c.t0 / total * L - cum[seq.index(w)])
        src = min(max(local, w.s), max(w.s, w.e - d))
        if w is prev_w:
            src = min(max(src, prev_src), max(w.s, w.e - d))
        _take(c, w, "撮った順", src)
        prev_w, prev_src = w, src


# ---------------- AI（Claude）で並べる ----------------
MV_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["concept", "sections", "avoid"],
    "properties": {
        "concept": {"type": "string", "description": "MV全体の流れの考え方（2〜3文）"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["section", "materials", "reason"],
                "properties": {
                    "section": {"type": "integer", "description": "曲の区間の番号"},
                    "materials": {"type": "array", "items": {"type": "integer"},
                                  "description": "その区間で使う素材の番号（使う順）。同じ番号をくり返してもよい"},
                    "reason": {"type": "string"},
                },
            },
        },
        "avoid": {"type": "array", "items": {"type": "integer"},
                  "description": "使わない方がよい素材の番号（ブレ・暗すぎ・関係ない絵など）"},
    },
}


def pick_representatives(wins: list[Window], clips: list[Clip], limit: int) -> list[Window]:
    """AIに見せる代表の区間を選ぶ（素材の長さに応じて割り振り、良い絵を離れた場面から）"""
    wins = [w for w in wins if not w.bad] or wins
    if len(wins) <= limit:
        return list(wins)
    total = sum(c.duration for c in clips) or 1
    by_clip: dict[int, list[Window]] = {}
    for w in wins:
        by_clip.setdefault(w.clip, []).append(w)
    quota = {ci: max(1, round(limit * clips[ci].duration / total)) for ci in by_clip}
    while sum(quota.values()) > limit:
        k = max(quota, key=lambda ci: quota[ci])
        if quota[k] <= 1:
            break
        quota[k] -= 1
    out = []
    for ci, ws in by_clip.items():
        q = quota[ci]
        span = (clips[ci].duration / max(q, 1)) * 0.6
        chosen: list[Window] = []
        for w in sorted(ws, key=lambda w: -w.q):
            if all(abs(w.s - x.s) >= span for x in chosen):
                chosen.append(w)
            if len(chosen) >= q:
                break
        out += chosen
    return sorted(out[:limit], key=lambda w: (w.clip, w.s))


def grab_frame(path: str, t: float, out: Path, width: int = 384) -> bool:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-ss", f"{max(t, 0):.3f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)], capture_output=True)
    return p.returncode == 0 and out.exists()


def _fmt(t: float) -> str:
    m, s = divmod(int(max(t, 0)), 60)
    return f"{m}:{s:02d}"


def ai_order(an: dict, cuts: list[Cut], wins: list[Window], clips: list[Clip], note: str, effort: str,
             frames_dir: Path, limit: int) -> tuple[dict, dict, list[Window]]:
    import rc_llm
    reps = pick_representatives(wins, clips, limit)
    frames_dir.mkdir(parents=True, exist_ok=True)
    for f in frames_dir.glob("m*.jpg"):
        f.unlink()
    content: list[dict] = []
    shown: list[Window] = []
    for w in reps:
        k = len(shown) + 1
        img = frames_dir / f"m{k:03d}.jpg"
        if not grab_frame(clips[w.clip].path, (w.s + w.e) / 2, img):
            continue
        shown.append(w)
        mv = "大" if w.mrank > 0.66 else ("中" if w.mrank > 0.33 else "小")
        br = "暗い" if w.bright < 0.2 else ("明るい" if w.bright > 0.7 else "ふつう")
        content.append({"type": "text", "text": f"素材{k}: {Path(clips[w.clip].path).name} {_fmt(w.s)}〜"
                                                f"（{w.length:.1f}秒・動き{mv}・明るさ{br}）"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(img.read_bytes()).decode()}})
    if not shown:
        raise RuntimeError("代表フレームを取り出せませんでした")
    used = sorted({c.section for c in cuts})
    lines = []
    for si in used:
        s = an["sections"][si]
        n = sum(1 for c in cuts if c.section == si)
        lines.append(f"区間{si}: {_fmt(s['start'])}〜{_fmt(s['end'])} {s['label']} "
                     f"盛り上がり{int(s['energy'] * 100)}% カット数{n}")
    content.append({"type": "text", "text":
                    f"## 曲\nテンポ {an['bpm']:.0f} BPM・{an['beats_per_bar']}拍子・長さ {_fmt(an['duration'])}\n"
                    f"曲の雰囲気（作者のメモ）: {note or '（なし）'}\n\n## 曲の区間\n" + "\n".join(lines)})
    system = """あなたは音楽サークルのMV（ミュージックビデオ）を編集する映像ディレクターです。
撮影素材から取り出した代表フレーム（素材1, 素材2, …）を見て、曲の各区間でどの素材をどの順で使うかを決めてください。

考え方:
- 曲の雰囲気・歌詞のメモに合う場面を選ぶ。区間の盛り上がりに合わせて、静かな所は落ち着いた絵、サビは動きがあって印象的な絵
- 物語の流れ（はじまり → 展開 → 一番の見せ場 → 余韻）を意識する
- 一番いい絵は最後のサビ（盛り上がりが一番高い区間）のために取っておく
- 同じ素材が続かないように。カット数より素材が少なければ、同じ番号をくり返してよい
- 各区間の materials には、カット数くらいの数の番号を使う順に並べる
- ブレている・暗すぎる・関係なさそうな絵は avoid に入れる
出力はJSONのみです。"""
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    res = rc_llm._call(client, anthropic, system, content, MV_SCHEMA, effort, usage)
    return res, usage, shown


def apply_ai(cuts: list[Cut], wins: list[Window], shown: list[Window], res: dict, seed: int) -> list[int]:
    """AIの並び順を当てはめる。当てはめられなかったカットの番号を返す（そこは自動で選ぶ）"""
    by_no = {k + 1: w for k, w in enumerate(shown)}
    avoid = {by_no[n].no for n in res.get("avoid", []) if n in by_no}
    plan = {}
    for s in res.get("sections", []):
        ms = [n for n in s.get("materials", []) if n in by_no]
        if ms:
            plan[int(s.get("section", -1))] = (ms, s.get("reason", ""))
    left = []
    counters: dict[int, int] = {}
    for i, c in enumerate(cuts):
        if c.section not in plan:
            left.append(i)
            continue
        ms, reason = plan[c.section]
        k = counters.get(c.section, 0)
        counters[c.section] = k + 1
        no = ms[k % len(ms)]
        w = by_no[no]
        d = c.t1 - c.t0
        if w.uses > 0 or w.length < d - 1e-3:
            # 同じ素材がくり返されたら、同じ場面の近くのまだ使っていない区間を使う
            near = [x for x in wins if x.clip == w.clip and x.uses == 0 and x.length >= d - 1e-3
                    and abs(x.s - w.s) <= 60 and x.no not in avoid and not x.bad]
            if near:
                w = min(near, key=lambda x: abs(x.s - w.s))
        _take(c, w, f"AI: 素材{no}" + (f"（{reason}）" if reason and k == 0 else ""))
    return left


# ============================================================
#  4. 書き出し
# ============================================================
def _batch_cmd(cuts: list[Cut], clips: list[Clip], w: int, h: int, fps: Fraction, fit: str,
               out: Path, enc: list[str]) -> list[str]:
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
    parts, labels = [], ""
    for i, c in enumerate(cuts):
        n = c.f1 - c.f0
        dur = float(n / fps) + 0.15
        cmd += ["-ss", f"{c.src_in:.4f}", "-t", f"{dur:.4f}", "-i", clips[c.clip].path]
        if fit == "contain":
            sc = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        else:
            sc = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        parts.append(f"[{i}:v]{sc},setsar=1,fps={fps.numerator}/{fps.denominator},format=yuv420p,"
                     f"tpad=stop_mode=clone:stop_duration=3,trim=end_frame={n},setpts=PTS-STARTPTS[v{i}]")
        labels += f"[v{i}]"
    parts.append(f"{labels}concat=n={len(cuts)}:v=1:a=0[v]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[v]", *enc, "-an", str(out)]


def _run(cmd: list[str], what: str, cwd: Path | None = None) -> None:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-12:])
        sys.exit(f"[エラー] FFmpeg（{what}）が失敗しました:\n{tail}")


def _run_progress(cmd: list[str], total: float, what: str, cwd: Path | None = None) -> None:
    import tempfile
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:], cwd=cwd,
                                stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", errors="replace")
        last = -10
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us=") and total > 0:
                    try:
                        pct = int(int(line.split("=")[1]) / 1e6 / total * 100)
                    except ValueError:
                        continue
                    if pct >= last + 10:
                        last = pct
                        log(f"    仕上げ中… ({min(pct, 100)}%)")
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            sys.exit(f"[エラー] FFmpeg（{what}）が失敗しました:\n" + "\n".join(err.read().strip().splitlines()[-12:]))


def render(cuts: list[Cut], clips: list[Clip], song: Path, a: float, total_frames: int, w: int, h: int,
           fps: Fraction, fit: str, enc_final: list[str], enc_inter: list[str], fade_out: float,
           out: Path, work: Path, batch: int = 8) -> None:
    import shutil
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    groups = [cuts[i:i + batch] for i in range(0, len(cuts), batch)]
    names = []
    for k, g in enumerate(groups, 1):
        log(f"    MVの本編の書き出し {k}/{len(groups)}")
        name = f"part_{k:04d}.mkv"
        _run(_batch_cmd(g, clips, w, h, fps, fit, work / name, enc_inter), f"映像 {k}")
        names.append(name)
    (work / "parts.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    _run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", "parts.txt",
          "-c", "copy", "body.mkv"], "映像の結合", cwd=work)
    total = float(total_frames / fps)
    af = [f"atrim=end={total:.4f}", "asetpts=PTS-STARTPTS"]
    vf = []
    if fade_out > 0:
        fo = min(fade_out, total / 3)
        af.append(f"afade=t=out:st={total - fo:.3f}:d={fo:.3f}")
        vf.append(f"fade=t=out:st={total - fo:.3f}:d={fo:.3f}")
    if a > 0.01:
        af.insert(0, "afade=t=in:d=0.05")
    log("■ 曲と合わせて仕上げています")
    _run_progress(["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", "body.mkv", "-ss", f"{a:.4f}",
                   "-t", f"{total + 0.5:.4f}", "-i", str(song.resolve()),
                   "-map", "0:v", "-map", "1:a",
                   *(["-vf", ",".join(vf)] if vf else []), "-af", ",".join(af),
                   *enc_final, "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-t", f"{total:.4f}",
                   "-movflags", "+faststart", str(out.resolve())], total, "MVの仕上げ", cwd=work)


# ---------------- Premiere用XML ----------------
def write_xml(path: Path, name: str, cuts: list[Cut], clips: list[Clip], song: Path, song_info: dict,
              a: float, total_frames: int, fps: Fraction, w: int, h: int, markers: list[tuple[float, str, str]]) -> None:
    from xml.sax.saxutils import escape

    from rc_export import _pathurl, _rate
    defined: set[str] = set()

    def vfile(ci: int) -> str:
        fid = f"file-mv-{ci + 1}"
        if fid in defined:
            return f'<file id="{fid}"/>'
        defined.add(fid)
        c = clips[ci]
        f = Fraction(c.fps)
        aud = ("<audio><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate>"
               "</samplecharacteristics><channelcount>2</channelcount></audio>") if c.has_audio else ""
        return (f'<file id="{fid}"><name>{escape(Path(c.path).name)}</name><pathurl>{_pathurl(Path(c.path))}</pathurl>'
                f"{_rate(f)}<duration>{int(round(c.duration * f))}</duration>"
                f"<media><video><samplecharacteristics>{_rate(f)}<width>{c.width}</width><height>{c.height}</height>"
                f"</samplecharacteristics></video>{aud}</media></file>")

    v_items = []
    for i, c in enumerate(cuts):
        f = Fraction(clips[c.clip].fps)
        fin = int(round(c.src_in * f))
        fout = fin + int(round((c.f1 - c.f0) * f / fps))
        v_items.append(
            f'<clipitem id="clipitem-mv-{i + 1}"><name>{escape(Path(clips[c.clip].path).name)}</name>'
            f"<enabled>TRUE</enabled><duration>{int(round(clips[c.clip].duration * f))}</duration>{_rate(f)}"
            f"<start>{c.f0}</start><end>{c.f1}</end><in>{fin}</in><out>{fout}</out>{vfile(c.clip)}"
            f"<comments><mastercomment1>{escape(c.why)}</mastercomment1></comments></clipitem>")
    nch = song_info["channels"]
    sdur = int(round(song_info["duration"] * fps))
    sin = int(round(a * fps))
    song_file = (f'<file id="file-song"><name>{escape(song.name)}</name><pathurl>{_pathurl(song.resolve())}</pathurl>'
                 f"{_rate(fps)}<duration>{sdur}</duration><media><audio><samplecharacteristics><depth>16</depth>"
                 f"<samplerate>{song_info['sample_rate']}</samplerate></samplecharacteristics>"
                 f"<channelcount>{nch}</channelcount></audio></media></file>")
    song_ref = '<file id="file-song"/>'
    a_tracks = ""
    for ch in range(nch):
        links = "".join(f'<link><linkclipref>clipitem-song-{k + 1}</linkclipref><mediatype>audio</mediatype>'
                        f'<trackindex>{k + 1}</trackindex><clipindex>1</clipindex><groupindex>1</groupindex></link>'
                        for k in range(nch))
        a_tracks += (f'<track><clipitem id="clipitem-song-{ch + 1}"><name>{escape(song.name)}</name><enabled>TRUE'
                     f"</enabled><duration>{sdur}</duration>{_rate(fps)}<start>0</start><end>{total_frames}</end>"
                     f"<in>{sin}</in><out>{sin + total_frames}</out>"
                     f"{song_file if ch == 0 else song_ref}"
                     f"<sourcetrack><mediatype>audio</mediatype><trackindex>{ch + 1}</trackindex></sourcetrack>"
                     f"{links if nch > 1 else ''}</clipitem><enabled>TRUE</enabled><locked>FALSE</locked>"
                     f"<outputchannelindex>{ch + 1}</outputchannelindex></track>")
    mk = "".join(f"<marker><name>{escape(n)}</name><comment>{escape(cm)}</comment>"
                 f"<in>{int(round(t * fps))}</in><out>-1</out></marker>" for t, n, cm in markers)
    fmt = (f"<format><samplecharacteristics>{_rate(fps)}<width>{w}</width><height>{h}</height>"
           f"<pixelaspectratio>square</pixelaspectratio><anamorphic>FALSE</anamorphic>"
           f"<fielddominance>none</fielddominance></samplecharacteristics></format>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
           f'<sequence id="sequence-mv"><name>{escape(name)}</name><duration>{total_frames}</duration>{_rate(fps)}'
           f"<timecode>{_rate(fps)}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat>"
           f"</timecode><media><video>{fmt}<track>{''.join(v_items)}<enabled>TRUE</enabled><locked>FALSE</locked>"
           f"</track></video><audio><numOutputChannels>{nch}</numOutputChannels><format><samplecharacteristics>"
           f"<depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></format>{a_tracks}</audio>"
           f"</media>{mk}</sequence></xmeml>\n")
    path.write_text(xml, encoding="utf-8")


def beat_markers(an: dict, a: float, b: float, mode: str) -> list[tuple[float, str, str]]:
    """Premiereのマーカー: (使う範囲の頭からの秒, 名前, コメント)。mode = beats / bars / sections"""
    down = an["downbeats"]
    secs = {round(s["start"], 2): s for s in an["sections"]}
    out, bar, beat_no = [], 0, 0
    for t in an["beats"]:
        is_down = any(abs(t - d) < 1e-3 for d in down)
        if is_down:
            bar, beat_no = bar + 1, 1
        else:
            beat_no += 1
        if not (a - 1e-3 <= t < b - 1e-3):
            continue
        sec = next((s for st, s in secs.items() if abs(st - t) < 0.06), None)
        if sec:
            out.append((t - a, f"【{sec['label']}】{bar}小節", f"{sec['label']}のはじまり（盛り上がり {int(sec['energy'] * 100)}%）"))
        elif mode == "beats":
            out.append((t - a, f"{bar}小節" if is_down else f"{beat_no}拍", "小節の頭" if is_down else "拍"))
        elif mode == "bars" and is_down:
            out.append((t - a, f"{bar}小節", "小節の頭"))
    have = [t for t, _, _ in out]
    for s in an["sections"]:
        if a - 1e-3 <= s["start"] < b and all(abs(s["start"] - a - h) > 0.06 for h in have):
            out.append((max(0.0, s["start"] - a), f"【{s['label']}】", "区間のはじまり"))
    return sorted(out)
