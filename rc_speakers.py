"""話者の区別（実験的な機能）：発言ごとに「だれが話しているか」を推定し、字幕を人ごとに色分けする。

重いライブラリ（torch など）は使わず、numpy だけで動く軽い方法です。
  1. 音声を少しずつ読みながら（3時間の素材でもメモリを使いすぎない）、発言ごとに
     声の音色（MFCC：声の響きの特徴）と声の高さ（ピッチ）を計算する
  2. 発言を、似た声どうしでグループ分けする（k-means 法）。人数は指定するか、自動で推定する
  3. 結果を出力フォルダの speakers.json に保存する。GUI で名前の変更や、発言ごとの手直しができる

speakers.json の中身:
  enabled    … 完成動画（finish.py）の字幕を、人ごとに色分けするか
  srt_names  … Premiere 用の字幕（○○_字幕.srt）の頭に話者名を入れるか
  names      … 話者の名前（0番目が「話者1」）
  auto       … 推定した結果 {発言ID: 話者番号}
  manual     … 人が直した結果 {発言ID: 話者番号}（auto より優先）
  score      … 推定の自信（0〜1。低いものは確認がおすすめ）
  fingerprint… 発言の区切り（chunks.json）の目印。文字起こしをやり直して区切りが変わったら使わない
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

SR = 16000
FRAME = 400          # 25ms
HOP = 160            # 10ms
NFFT = 512
NMEL = 24
NCEP = 12
F0_MIN, F0_MAX = 70.0, 420.0
BLOCK_SEC = 30       # 一度に読む長さ（メモリを使いすぎないように少しずつ）
MIN_FRAMES = 20      # 0.2秒より短い（声の部分が少ない）発言は、特徴が不安定なので前後の発言から決める
FILE = "speakers.json"

DEFAULT_COLORS = ["#FFFFFF", "#FFE14D", "#7FDBFF", "#FF9AA2", "#9BE59B", "#D6A8FF"]
DEFAULT_OUTLINES = ["#1A1A1A", "#1A1A1A", "#1A1A1A", "#1A1A1A", "#1A1A1A", "#1A1A1A"]
DEFAULT_SRT_FORMAT = "{name}："


# ============================================================
#  音声の特徴（numpy だけで計算）
# ============================================================
def _mel_filterbank() -> np.ndarray:
    def hz2mel(f):
        return 2595 * np.log10(1 + f / 700)

    def mel2hz(m):
        return 700 * (10 ** (m / 2595) - 1)
    mels = np.linspace(hz2mel(100), hz2mel(7000), NMEL + 2)
    bins = np.floor((NFFT + 1) * mel2hz(mels) / SR).astype(int)
    fb = np.zeros((NMEL, NFFT // 2 + 1), dtype=np.float32)
    for i in range(NMEL):
        a, b, c = bins[i], bins[i + 1], bins[i + 2]
        if b > a:
            fb[i, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            fb[i, b:c] = (c - np.arange(b, c)) / (c - b)
    return fb


_FB = _mel_filterbank()
_WIN = np.hamming(FRAME).astype(np.float32)
_DCT = np.cos(np.pi / NMEL * (np.arange(NMEL)[None, :] + 0.5) * np.arange(1, NCEP + 1)[:, None]).astype(np.float32)
_LAG_MIN, _LAG_MAX = int(SR / F0_MAX), int(SR / F0_MIN)
# 窓をかけたことで、ずれ（lag）が大きいほど自己相関が小さくなる分を補正する
_WAC = np.fft.irfft(np.abs(np.fft.rfft(_WIN, 1024)) ** 2)[: _LAG_MAX + 2]
_WAC = (_WAC / _WAC[0]).astype(np.float32)


def frame_features(frames: np.ndarray):
    """frames: (n, FRAME) の音声 → (音量の対数, MFCC (n, NCEP), 声の高さHz (n,), 声らしさ (n,))"""
    frames = frames.astype(np.float32)
    frames = frames - frames.mean(axis=1, keepdims=True)
    energy = np.log((frames ** 2).sum(axis=1) + 1e-3)
    pre = np.concatenate([frames[:, :1], frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    spec = np.abs(np.fft.rfft(pre * _WIN, NFFT)) ** 2
    mel = np.log(spec.astype(np.float32) @ _FB.T + 1e-6)
    mfcc = mel @ _DCT.T
    # 声の高さ：自己相関のいちばん強い山（倍の周期の山を選ばないよう、強い山のうち最初のもの）
    ac = np.fft.irfft(np.abs(np.fft.rfft(frames * _WIN, 1024)) ** 2)[:, : _LAG_MAX + 2].astype(np.float32)
    r = ac / (ac[:, :1] + 1e-9) / np.maximum(_WAC[None, :], 0.05)
    seg = r[:, _LAG_MIN - 1: _LAG_MAX + 2]
    mid = seg[:, 1:-1]
    peak = (mid >= seg[:, :-2]) & (mid >= seg[:, 2:])
    best = np.where(peak, mid, -1).max(axis=1)
    cand = peak & (mid >= 0.85 * best[:, None])
    lag = cand.argmax(axis=1) + _LAG_MIN
    voiced = np.clip(best, 0, 1)
    f0 = SR / lag
    return energy, mfcc, f0.astype(np.float32), voiced.astype(np.float32)


def _summarize(energy, mfcc, f0, voiced):
    """1つの発言のフレーム → 発言の特徴（声の部分だけを使う）。声の部分のフレーム数も返す"""
    if len(energy) == 0:
        return None, 0
    thr = np.percentile(energy, 90) - 5.0      # いちばん大きい所より 約22dB 小さい所まで
    act = energy >= max(thr, energy.min())
    v = act & (voiced > 0.45)
    use = v if v.sum() >= 5 else act
    m = mfcc[use]
    mu, sd = m.mean(axis=0), m.std(axis=0)
    if v.sum() >= 5:
        lf = np.log(f0[v])
        pitch, prange = float(np.median(lf)), float(np.percentile(lf, 80) - np.percentile(lf, 20))
    else:
        pitch, prange = np.nan, np.nan
    vr = float(v.mean())
    return np.concatenate([mu, sd, [pitch, prange, vr]]).astype(np.float32), int(v.sum())


def _read_audio(path: Path):
    """素材の音声を 16kHz・モノラルで少しずつ読む（全体を一度にメモリに載せない）"""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    try:
        while True:
            buf = proc.stdout.read(SR * 2 * BLOCK_SEC)
            if not buf:
                break
            if len(buf) % 2:
                buf += proc.stdout.read(1)
            yield np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        proc.stdout.close()
        proc.wait()


def chunk_features(sources, chunks, log=print) -> dict[int, tuple[np.ndarray | None, int]]:
    """発言（チャンク）ごとの特徴。sources は rc_media.SourceSet、chunks は start/end/id を持つもの。"""
    chunks = sorted(chunks, key=lambda c: c.start)
    starts = np.array([c.start for c in chunks])
    ends = np.array([c.end for c in chunks])
    open_: dict[int, list] = {}
    out: dict[int, tuple] = {}
    total = max(sources.duration, 1.0)
    nxt = 0            # まだ終わっていない最初の発言
    last_report = 0.0

    def finish_until(t):
        nonlocal nxt
        while nxt < len(chunks) and ends[nxt] <= t:
            c = chunks[nxt]
            parts = open_.pop(c.id, [])
            if parts:
                cat = [np.concatenate([p[k] for p in parts]) for k in range(4)]
                out[c.id] = _summarize(*cat)
            else:
                out[c.id] = (None, 0)
            nxt += 1

    for f, off in zip(sources.files, sources.offsets):
        rest = np.zeros(0, dtype=np.float32)
        base = 0               # rest の先頭が、このファイルの何サンプル目か
        for x in _read_audio(f.path):
            buf = np.concatenate([rest, x])
            n = (len(buf) - FRAME) // HOP + 1
            if n > 0:
                t = off + (base + np.arange(n) * HOP + FRAME / 2) / SR
                # 発言の中にあるフレームだけ計算する（無言の長い区間は飛ばして速く）
                k = np.searchsorted(starts, t, side="right") - 1
                inside = (k >= 0) & (t < ends[np.clip(k, 0, None)])
                if inside.any():
                    idx = np.nonzero(inside)[0]
                    sh = np.lib.stride_tricks.as_strided(
                        buf, shape=(n, FRAME), strides=(buf.strides[0] * HOP, buf.strides[0]))
                    feats = frame_features(sh[idx])
                    kk = k[idx]
                    for ci in np.unique(kk):
                        sel = kk == ci
                        open_.setdefault(chunks[ci].id, []).append([a[sel] for a in feats])
                finish_until(t[-1])
                cut = n * HOP
                rest = buf[cut:].copy()
                base += cut
            else:
                rest = buf
            now = off + base / SR
            if now - last_report > 600:
                last_report = now
                print(f"    … {now / 60:5.1f}分 / {total / 60:.1f}分 ({100 * now / total:.0f}%)", flush=True)
    finish_until(float("inf"))
    return out


# ============================================================
#  グループ分け（k-means）
# ============================================================
def _kmeans(x: np.ndarray, k: int, seed: int = 0, n_init: int = 8, iters: int = 60):
    rng = np.random.default_rng(seed)
    best = (np.inf, None, None)
    for _ in range(n_init):
        # k-means++ の初期値
        c = [x[rng.integers(len(x))]]
        for _j in range(1, k):
            d = np.min(((x[:, None, :] - np.array(c)[None]) ** 2).sum(-1), axis=1)
            p = d / d.sum() if d.sum() > 0 else None
            c.append(x[rng.choice(len(x), p=p)])
        c = np.array(c)
        for _it in range(iters):
            d = ((x[:, None, :] - c[None]) ** 2).sum(-1)
            lab = d.argmin(axis=1)
            nc = np.array([x[lab == j].mean(axis=0) if (lab == j).any() else c[j] for j in range(k)])
            if np.allclose(nc, c):
                break
            c = nc
        d = ((x[:, None, :] - c[None]) ** 2).sum(-1)
        lab = d.argmin(axis=1)
        inertia = d[np.arange(len(x)), lab].sum()
        if inertia < best[0]:
            best = (inertia, lab, c)
    return best[1], best[2]


def _sep(x: np.ndarray, lab: np.ndarray, c: np.ndarray) -> float:
    """いちばん近いグループどうしの距離 ÷ グループ内のばらつき（大きいほど、はっきり分かれている）"""
    k = len(c)
    w = np.sqrt(np.mean(((x - c[lab]) ** 2).sum(1)) + 1e-9)
    return min(float(np.sqrt(((c[i] - c[j]) ** 2).sum())) for i in range(k) for j in range(i + 1, k)) / w


def estimate_k(x: np.ndarray, max_k: int = 6, seed: int = 0, refs: int = 6, max_n: int = 800):
    """人数の推定：ギャップ統計（でたらめに散らばった点と比べて、どれだけまとまっているか）で決め、
    はっきり分かれていないグループ（近すぎる）があれば人数を減らす。"""
    rng = np.random.default_rng(seed)
    if len(x) > max_n:
        x = x[rng.choice(len(x), max_n, replace=False)]
    max_k = max(1, min(max_k, len(x) - 1))
    xc = x - x.mean(0)
    _, _, vt = np.linalg.svd(xc, full_matrices=False)
    xp = xc @ vt.T
    lo, hi = xp.min(0), xp.max(0)

    def log_w(z, k):
        lab, c = _kmeans(z, k, seed, n_init=3)
        return np.log(((z - c[lab]) ** 2).sum() + 1e-9), lab, c
    gaps, sds, fits = [], [], {}
    for k in range(1, max_k + 1):
        ref = [log_w(rng.uniform(lo, hi, size=xp.shape), k)[0] for _ in range(refs)]
        lw, lab, c = log_w(xp, k)
        fits[k] = (lab, c)
        gaps.append(float(np.mean(ref) - lw))
        sds.append(float(np.std(ref) * np.sqrt(1 + 1 / refs)))
    k = max_k
    for j in range(1, max_k):
        if gaps[j - 1] >= gaps[j] - sds[j]:
            k = j
            break
    while k > 1 and _sep(xp, *fits[k]) < 1.3:
        k -= 1
    return k, {"gap": [round(g, 3) for g in gaps]}


def _prepare(feats: dict[int, tuple]) -> tuple[list[int], np.ndarray]:
    """発言の特徴をそろえて（平均0・ばらつき1）、声の高さを重めにする"""
    ids = [i for i, (v, n) in feats.items() if v is not None and n >= MIN_FRAMES]
    if not ids:
        return [], np.zeros((0, 1))
    x = np.array([feats[i][0] for i in ids], dtype=np.float64)
    for j in range(x.shape[1]):
        col = x[:, j]
        bad = ~np.isfinite(col)
        if bad.all():
            col[:] = 0
        elif bad.any():
            col[bad] = np.nanmedian(col)
    mu, sd = x.mean(0), x.std(0)
    x = (x - mu) / np.where(sd > 1e-6, sd, 1)
    w = np.ones(x.shape[1])
    w[:NCEP] = 1.0                    # 声の響き（平均）
    w[NCEP:2 * NCEP] = 0.5            # 声の響きのばらつき
    w[2 * NCEP] = 3.0                 # 声の高さ
    w[2 * NCEP + 1] = 0.7             # 抑揚の大きさ
    w[2 * NCEP + 2] = 0.5             # 声らしさ
    return ids, x * w


def cluster(feats: dict[int, tuple], num: int | None, max_speakers: int = 6, seed: int = 0):
    """発言ごとの話者番号と自信（0〜1）を返す。num=None なら人数も推定する。"""
    ids, x = _prepare(feats)
    labels: dict[int, int] = {}
    score: dict[int, float] = {}
    if len(ids) < 2:
        return {i: 0 for i in feats}, {i: 0.0 for i in feats}, 1, {}
    info = {}
    if num is None:
        k, info = estimate_k(x, max_speakers, seed)
    else:
        k = max(1, min(int(num), len(ids)))
    if k == 1:
        lab, cent = np.zeros(len(ids), dtype=int), x.mean(0, keepdims=True)
    else:
        lab, cent = _kmeans(x, k, seed)
    # 話者の番号は、たくさん話している順（いちばん話している人が「話者1」＝ふつうの字幕の色）
    dur = {i: 0.0 for i in range(k)}
    for i, l in zip(ids, lab):
        dur[int(l)] += feats[i][1]
    order = sorted(range(k), key=lambda j: -dur[j])
    remap = {old: new for new, old in enumerate(order)}
    d = np.sqrt(((x[:, None, :] - cent[None]) ** 2).sum(-1))
    for row, (i, l) in enumerate(zip(ids, lab)):
        labels[i] = remap[int(l)]
        if k > 1:
            ds = np.sort(d[row])
            score[i] = round(float((ds[1] - ds[0]) / (ds[1] + ds[0] + 1e-9)), 3)
        else:
            score[i] = 1.0
    return labels, score, k, info


def fill_short(labels: dict[int, int], score: dict[int, float], chunks) -> None:
    """推定できなかった（短すぎる）発言に、直前（なければ直後）の発言の話者を入れる"""
    order = sorted(chunks, key=lambda c: c.start)
    prev = None
    pending = []
    for c in order:
        if c.id in labels:
            for p in pending:
                labels[p] = labels[c.id] if prev is None else prev
                score[p] = 0.0
            pending = []
            prev = labels[c.id]
        else:
            pending.append(c.id)
    for p in pending:
        labels[p] = prev if prev is not None else 0
        score[p] = 0.0


# ============================================================
#  speakers.json の読み書き
# ============================================================
def fingerprint(chunks) -> str:
    h = hashlib.sha1()
    for c in sorted(chunks, key=lambda c: c.id):
        h.update(f"{c.id}|{c.start:.2f}|{c.end:.2f};".encode())
    return h.hexdigest()[:16]


def _chunks_of(out: Path):
    from rc_transcribe import Chunk
    p = Path(out) / "chunks.json"
    if not p.exists():
        return []
    return [Chunk(**c) for c in json.loads(p.read_text(encoding="utf-8"))]


def load(out: Path, chunks=None, quiet: bool = False) -> dict | None:
    """speakers.json を読む。発言の区切りが変わっていたら（文字起こしのやり直しなど）使わない"""
    p = Path(out) / FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        if not quiet:
            print(f"  [注意] {p.name} を読めませんでした。話者の色分けは使いません。")
        return None
    chunks = chunks if chunks is not None else _chunks_of(out)
    if chunks and data.get("fingerprint") and data["fingerprint"] != fingerprint(chunks):
        if not quiet:
            print("  [注意] 文字起こしの区切りが変わったので、前の話者の推定は使いません。"
                  "もう一度「話者を推定する」を実行してください。")
        return None
    data.setdefault("names", [])
    data.setdefault("auto", {})
    data.setdefault("manual", {})
    data.setdefault("score", {})
    data.setdefault("enabled", True)
    data.setdefault("srt_names", False)
    return data


def save(out: Path, data: dict) -> Path:
    p = Path(out) / FILE
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def labels_of(data: dict) -> dict[int, int]:
    """発言ID → 話者番号（人が直したものを優先）"""
    lab = {int(k): int(v) for k, v in data.get("auto", {}).items()}
    lab.update({int(k): int(v) for k, v in data.get("manual", {}).items()})
    return lab


def name_of(data: dict, spk: int) -> str:
    names = data.get("names") or []
    return names[spk] if 0 <= spk < len(names) and names[spk] else f"話者{spk + 1}"


def _match_old(new: dict[int, int], old: dict[int, int], k: int) -> dict[int, int]:
    """推定し直したとき、前と同じ人には同じ番号（名前）が付くように番号を合わせる"""
    common = [i for i in new if i in old]
    if not common:
        return {j: j for j in range(k)}
    m = max(max(old.values()) + 1, k)
    conf = np.zeros((k, m))
    for i in common:
        conf[new[i], old[i]] += 1
    remap, used = {}, set()
    for _ in range(k):
        a, b = np.unravel_index(np.argmax(conf), conf.shape)
        if conf[a, b] <= 0:
            break
        remap[int(a)] = int(b)
        used.add(int(b))
        conf[a, :] = -1
        conf[:, b] = -1
    free = (j for j in range(m + k) if j not in used)
    for j in range(k):
        if j not in remap:
            remap[j] = next(free)
    return remap


def detect(sources, chunks, out: Path, num: int | str | None = "auto", max_speakers: int = 6) -> dict:
    """話者を推定して speakers.json に保存する。前の名前と手直しは、区切りが同じなら引き継ぐ。"""
    n = None if num in (None, "auto", "自動", 0, "0") else int(num)
    print(f"■ 話者の推定（実験的な機能・人数: {'自動' if n is None else f'{n}人'}）")
    feats = chunk_features(sources, chunks)
    labels, score, k, info = cluster(feats, n, max_speakers)
    fill_short(labels, score, chunks)
    old = load(out, chunks, quiet=True)
    names = [f"話者{j + 1}" for j in range(k)]
    manual = {}
    keep = {"enabled": True, "srt_names": False}
    if old:
        remap = _match_old(labels, labels_of(old), k)
        labels = {i: remap[v] for i, v in labels.items()}
        names = list(old.get("names") or [])
        manual = {str(i): v for i, v in old.get("manual", {}).items()}
        keep = {"enabled": old.get("enabled", True), "srt_names": old.get("srt_names", False)}
        for j in range(max(labels.values(), default=0) + 1):
            if j >= len(names):
                names.append(f"話者{j + 1}")
    data = {
        "version": 1,
        **keep,
        "names": names,
        "fingerprint": fingerprint(chunks),
        "auto": {str(i): int(v) for i, v in sorted(labels.items())},
        "manual": manual,
        "score": {str(i): float(v) for i, v in sorted(score.items())},
        "detected": {"num": k, "requested": n or "auto", **info},
    }
    save(out, data)
    counts = np.bincount(list(labels.values()), minlength=k) if labels else []
    print(f"  {k}人と推定しました（" + "、".join(f"{name_of(data, j)} {int(c)}発言" for j, c in enumerate(counts)) + "）")
    low = sum(1 for v in score.values() if v < 0.1)
    if low:
        print(f"  自信の低い発言が {low}個あります。画面の「話者の色分け」で確認してください。")
    return data


# ============================================================
#  字幕への反映
# ============================================================
def caption_ids(items_text: list[str], item_ids: list[int], max_chars: int) -> list[int]:
    """build_captions の分け方と同じ順番で、字幕1枚ごとの発言IDを返す"""
    from rc_export import split_caption
    return [iid for text, iid in zip(items_text, item_ids) for _ in split_caption(text, max_chars)]


def _spk_cfg(tpl) -> dict:
    try:
        return dict(tpl.get("speakers", {}) or {}) if tpl is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def colors(tpl, spk: int) -> dict:
    cfg = _spk_cfg(tpl)
    cols = cfg.get("colors") or DEFAULT_COLORS
    outs = cfg.get("outline_colors") or DEFAULT_OUTLINES
    return {"color": cols[spk % len(cols)], "outline_color": outs[spk % len(outs)]}


def label_srt(out: Path, items: list[tuple[float, float, str]], item_ids: list[int], max_chars: int,
              caps: list[tuple[float, float, str]], tpl=None, chunks=None) -> list[tuple[float, float, str]]:
    """Premiere 用の字幕に話者名を入れる（speakers.json で srt_names を有効にしたときだけ）"""
    data = load(out, chunks)
    if not data or not data.get("srt_names"):
        return caps
    lab = labels_of(data)
    ids = caption_ids([t for _, _, t in items], item_ids, max_chars)
    if len(ids) != len(caps):
        return caps
    fmt = _spk_cfg(tpl).get("srt_name_format") or DEFAULT_SRT_FORMAT
    res, prev = [], None
    for (s, e, t), cid in zip(caps, ids):
        spk = lab.get(cid)
        # 同じ人が続けて話している間（同じ発言を分けた2枚目以降）は、名前をくり返さない
        if spk is not None and (cid, spk) != prev:
            t = fmt.replace("{name}", name_of(data, spk)) + t
        prev = (cid, spk)
        res.append((s, e, t))
    print("  字幕（SRT）に話者名を入れました")
    return res


def decorate_captions(out: Path, tpl, items, max_chars: int, off: float, inside) -> list | None:
    """完成動画の字幕を人ごとの色にする。items は finish.py の (id, 開始, 終了, 文)。
    色分けしないとき（speakers.json がない・無効）は None を返す。"""
    data = load(out)
    if not data or not data.get("enabled", True):
        return None
    from rc_export import build_captions, mend_items
    lab = labels_of(data)
    fixed = mend_items([(s, e, t) for _, s, e, t in items])   # 言葉の途中で区切られた所をつなぎ直す
    items = [(i, *m) for (i, *_x), m in zip(items, fixed)]
    caps = build_captions([(s, e, t) for _, s, e, t in items], max_chars)
    ids = caption_ids([t for *_x, t in items], [i for i, *_x in items], max_chars)
    if len(ids) != len(caps):
        return None
    show_name = bool(_spk_cfg(tpl).get("caption_name", False))
    fmt = _spk_cfg(tpl).get("srt_name_format") or DEFAULT_SRT_FORMAT
    res, prev = [], None
    for (s, e, t), cid in zip(caps, ids):
        if not inside(s):
            continue
        spk = lab.get(cid, 0)
        if show_name and (cid, spk) != prev:
            t = fmt.replace("{name}", name_of(data, spk)) + t
        prev = (cid, spk)
        res.append((s + off, e + off, t, colors(tpl, spk)))
    print(f"  字幕を話者ごとに色分けします（{len(set(lab.values()))}人）")
    return res


def rewrite_premiere_srt(out: Path, tpl=None) -> Path:
    """今の荒編集（session.json）のまま、Premiere 用の字幕（○○_字幕.srt）だけを書き直す"""
    from rc_export import build_captions, mend_items, write_srt
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    out = Path(out)
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    chunks = _chunks_of(out)
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    cut = set(sess.get("cut_ids", []))
    corr = {int(k): v for k, v in sess.get("corrections", {}).items()}
    items, item_ids = [], []
    for c in chunks:
        if c.id in cut:
            continue
        text = corr.get(c.id, c.text)
        for s, e in tl.map_range(c.start, c.end):
            items.append((s, e, text))
            item_ids.append(c.id)
    max_chars = int(sess.get("args", {}).get("caption_chars", 24))
    items = mend_items(items)
    caps = build_captions(items, max_chars)
    caps = label_srt(out, items, item_ids, max_chars, caps, tpl, chunks)
    p = out / f"{sess['stem']}_字幕.srt"
    write_srt(p, caps)
    return p
