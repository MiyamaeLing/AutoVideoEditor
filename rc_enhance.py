"""声を聞き取りやすくする音声処理と、映像の補正（明るさ・色・LUT・手ブレ補正・2台目カメラの色合わせ）。

テンプレート（template.toml）の [audio_clean] / [color] / [stabilize] で調整・無効化できる。
GUI の「詳細設定」や finish.py の --audio-clean / --color / --stabilize で、テンプレートより優先して切り替えられる。

しくみ
- 声: 低音カット → ノイズ除去（afftdn）→ 歯擦音の抑制（deesser）→ コンプレッサー。
  FFmpeg の標準フィルターだけを使う（外部のモデルファイルは要らない）。
  ノイズ除去は処理の都合で音が少し遅れる（約25ms）ので、遅れを実測して取り除き、映像とずれないようにする。
  オープニング・エンディングの音（BGMなど）には処理をかけない。
- 色: 素材ファイルごとに静止画を数枚だけ調べて、明るさ・色かぶりの補正を「3D LUT（.cube）」1枚にまとめる。
  手で書いた明るさ・コントラスト・彩度と、テンプレートの LUT も同じ 1 枚に合成して、lut3d フィルター 1 回で当てる。
  フレームごとに計算しないので、長い素材でも重くならず、カットの前後で色がちらつかない。
- 手ブレ補正: vidstab（2回に分けて解析→補正）。FFmpeg に vidstab が入っていなければ deshake を使う。
  解析は、次のまとまりの書き出しと同時に裏で進めるので、待ち時間が増えにくい。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

import rc_win  # noqa: F401

SR = 48000
CUBE_SIZE = 33

# ---------------------------------------------------------------- モードごとの初期値
# lowcut: 低音カットの周波数(Hz) / nr: ノイズ除去の強さ(dB) / deess: 歯擦音の抑制(0〜1)
# ratio: コンプレッサーの圧縮比 / wind: 風切り音対策（低音を急な傾きでカットし、ノイズ除去を強める）
AUDIO_MODE = {
    "talk": dict(lowcut=80, nr=10.0, deess=0.4, ratio=3.0, wind=False),
    "craft": dict(lowcut=80, nr=8.0, deess=0.3, ratio=2.5, wind=False),     # 作業の音は消しすぎない
    "outdoor": dict(lowcut=120, nr=14.0, deess=0.3, ratio=3.0, wind=True),  # 風切り音対策を強め
}
STAB_MODE_DEFAULT = {"talk": False, "craft": False, "outdoor": True}

OVERRIDE_KEYS = ("audio_clean", "color", "stabilize")
ENV_NAME = "AVE_ENHANCE"    # GUI から {"audio_clean": true, ...} の形で渡される


def _bool(v, default=None):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "on", "yes", "1", "使う"):
        return True
    if s in ("false", "off", "no", "0", "使わない"):
        return False
    return default


def _num(d: dict, k: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(d.get(k, default))
    except (TypeError, ValueError):
        print(f"  [注意] テンプレートの {k} の値が数字ではないので、{default} として扱います")
        v = default
    return min(max(v, lo), hi)


def env_overrides() -> dict:
    """GUI から渡された切り替え（環境変数 AVE_ENHANCE）を読む。おかしな値は無視する"""
    raw = os.environ.get(ENV_NAME, "")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out = {}
    for k in OVERRIDE_KEYS:
        b = _bool(d.get(k)) if isinstance(d, dict) and k in d else None
        if b is not None:
            out[k] = b
    return out


# ---------------------------------------------------------------- 設定のまとめ
@dataclass
class Settings:
    audio: dict | None = None      # None = 声の処理をしない
    color: dict | None = None      # None = 色の補正をしない
    stab: dict | None = None       # None = 手ブレ補正をしない
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        on = []
        if self.audio:
            on.append("声の聞き取りやすさ" + ("（風切り音対策つき）" if self.audio["wind"] else ""))
        if self.color:
            c = []
            if self.color["auto"]:
                c.append("自動補正")
            if self.color["lut"]:
                c.append(f"LUT {Path(self.color['lut']).name}")
            if self.color["manual"]:
                c.append("手動調整")
            on.append("色（" + "・".join(c) + "）")
        if self.stab:
            on.append(f"手ブレ補正（{self.stab['method']}）")
        return "・".join(on) if on else "なし"


def resolve(tpl, mode: str, overrides: dict | None = None) -> Settings:
    """テンプレート・撮影モード・切り替え（GUI/コマンド）から、実際に使う設定を決める。

    overrides: {"audio_clean": True/False, "color": True/False, "stabilize": True/False}（無いものはテンプレート通り）
    """
    ov = dict(env_overrides())
    ov.update({k: v for k, v in (overrides or {}).items() if v is not None})
    st = Settings()
    tdir = Path(getattr(tpl, "dir", "."))

    # ---- 声 ----
    a = tpl.get("audio_clean", {}) or {}
    base = AUDIO_MODE.get(mode, AUDIO_MODE["talk"])
    on = ov.get("audio_clean", _bool(a.get("enabled", True), True))
    if on:
        s = _num(a, "strength", 1.0, 0.0, 2.0)
        wind = _bool(a.get("wind", "auto"))
        wind = base["wind"] if wind is None else wind
        lowcut = _num(a, "lowcut_hz", 0, 0, 300) or base["lowcut"]
        st.audio = dict(
            lowcut=lowcut, wind=wind,
            nr=base["nr"] * s * _num(a, "denoise", 1.0, 0.0, 3.0),
            deess=min(1.0, base["deess"] * s * _num(a, "deesser", 1.0, 0.0, 3.0)),
            ratio=1.0 + (base["ratio"] - 1.0) * s * _num(a, "compressor", 1.0, 0.0, 3.0),
        )
        if s <= 0:
            st.audio = None

    # ---- 色 ----
    c = tpl.get("color", {}) or {}
    lut_rel = str(c.get("lut", "") or "")
    lut = None
    if lut_rel:
        p = (tdir / lut_rel).resolve()
        if p.exists():
            lut = str(p)
        else:
            st.notes.append(f"LUT ファイルが見つかりません: {p}")
    manual = dict(brightness=_num(c, "brightness", 0.0, -0.5, 0.5), contrast=_num(c, "contrast", 1.0, 0.5, 2.0),
                  saturation=_num(c, "saturation", 1.0, 0.0, 2.0), temperature=_num(c, "temperature", 0.0, -1, 1))
    has_manual = (abs(manual["brightness"]) > 1e-3 or abs(manual["contrast"] - 1) > 1e-3
                  or abs(manual["saturation"] - 1) > 1e-3 or abs(manual["temperature"]) > 1e-3)
    auto = _bool(c.get("auto", False), False)
    use_color = ov.get("color")
    if use_color is True:
        auto = True
    if use_color is not False and (auto or lut or has_manual):
        st.color = dict(auto=auto, strength=_num(c, "strength", 0.5, 0.0, 1.0), lut=lut,
                        lut_strength=_num(c, "lut_strength", 1.0, 0.0, 1.0),
                        manual=manual if has_manual else None)

    # ---- 手ブレ補正 ----
    b = tpl.get("stabilize", {}) or {}
    en = _bool(b.get("enabled", "auto"))
    en = STAB_MODE_DEFAULT.get(mode, False) if en is None else en
    en = ov.get("stabilize", en)
    if en:
        method = str(b.get("method", "auto")).lower()
        have = available_filters()
        if method in ("auto", "vidstab") and {"vidstabdetect", "vidstabtransform"} <= have:
            method = "vidstab"
        elif "deshake" in have:
            if method == "vidstab":
                st.notes.append("この FFmpeg には vidstab が入っていないので、deshake で手ブレ補正します")
            method = "deshake"
        else:
            st.notes.append("この FFmpeg では手ブレ補正が使えません（vidstab も deshake もありません）")
            method = ""
        if method:
            st.stab = dict(method=method, strength=_num(b, "strength", 0.5, 0.05, 1.0))
    return st


_FILTERS: set[str] | None = None


def available_filters() -> set[str]:
    global _FILTERS
    if _FILTERS is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout
        except FileNotFoundError:
            out = ""
        _FILTERS = {m.group(1) for m in re.finditer(r"^\s*[TSC.]{3}\s+(\w+)\s", out, re.M)}
    return _FILTERS


# ================================================================ 声
def lowcut_chain(a: dict) -> str:
    lc = int(round(a["lowcut"]))
    f = [f"highpass=f={lc}:poles=2"]
    if a["wind"]:
        f.append(f"highpass=f={lc}:poles=2")   # 2段重ねて、風の「ボボボ」を急な傾きで切る
    return ",".join(f)


def voice_chain(a: dict, noise_db: float | None = None) -> str:
    """声のトラックにかけるフィルターの並び（FFmpeg の -af の書き方）

    noise_db: 素材の雑音の大きさ（dBFS）。ノイズ除去はこの大きさの音を「雑音」とみなして減らす
    """
    f = [lowcut_chain(a)]
    if a["nr"] > 0.5:
        nf = -50.0 if noise_db is None else min(max(noise_db + 3, -80), -20)
        f.append(f"afftdn=nr={min(a['nr'], 40):.1f}:nf={nf:.0f}")
    if a["deess"] > 0.01:
        f.append(f"deesser=i={a['deess']:.2f}:m=0.5:f=0.5:s=o")
    if a["ratio"] > 1.05:
        # しきい値 -24dBFS、ゆるやかなひざ。上げた分（makeup）は最後の音量合わせ（loudnorm）でならされる
        f.append(f"acompressor=threshold=0.063:ratio={a['ratio']:.2f}:attack=8:release=200:knee=4:makeup=1.6")
    return ",".join(f)


_DELAY: dict[str, int] = {}


def measure_delay(chain: str) -> int:
    """フィルターの並びで音が何サンプル遅れるかを実測する（FFmpeg の版によって違うため）"""
    if chain in _DELAY:
        return _DELAY[chain]
    rng = np.random.default_rng(7)
    n = SR * 2
    x = rng.normal(0, 0.002, n).astype(np.float32)
    for t in (0.6, 1.2):
        s = int(t * SR)
        x[s:s + 1200] += rng.normal(0, 0.3, 1200).astype(np.float32)   # 短い「ザッ」という音
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                        "-af", chain, "-f", "f32le", "-"], input=x.tobytes(), capture_output=True)
    if p.returncode != 0:
        sys.exit("[エラー] 声の処理（FFmpeg のフィルター）が使えませんでした:\n"
                 + p.stderr.decode("utf-8", "replace")[-800:]
                 + "\n  テンプレートの [audio_clean] の enabled = false にすると、声の処理なしで書き出せます。")
    y = np.frombuffer(p.stdout, dtype=np.float32)
    m = min(len(x), len(y))
    if m < SR:
        d = 0
    else:
        # 相互相関で、いちばんよく重なる位置（遅れ）を探す。0〜0.3秒の範囲
        k = 1 << int(np.ceil(np.log2(2 * m)))
        c = np.fft.irfft(np.fft.rfft(y[:m], k) * np.conj(np.fft.rfft(x[:m], k)), k)
        lim = int(0.3 * SR)
        d = int(np.argmax(c[:lim]))
    _DELAY[chain] = d
    return d


@dataclass
class VoiceFx:
    """rc_render.mix_audio に渡す、声のトラックの処理"""
    chain: str
    delay: int           # 実測した遅れ（サンプル）
    start: int = 0       # 処理する範囲（サンプル）。オープニング・エンディングは外す
    end: int | None = None

    def graph(self, src: str, out_label: str) -> list[str]:
        """src（"[0:a]volume=0dB" のような途中までの並び）から out_label までのフィルターグラフ"""
        d = self.delay
        core = (f"apad=pad_len={d}," if d else "") + self.chain + \
            (f",atrim=start_sample={d},asetpts=PTS-STARTPTS" if d else "")
        if self.start <= 0 and self.end is None:
            return [f"{src},{core}{out_label}"]
        parts, labels = [], []
        n = 1 + (self.start > 0) + (self.end is not None)
        parts.append(f"{src},asplit={n}" + "".join(f"[vfx{i}]" for i in range(n)))
        i = 0
        if self.start > 0:
            parts.append(f"[vfx{i}]atrim=end_sample={self.start},asetpts=PTS-STARTPTS[vpre]")
            labels.append("[vpre]")
            i += 1
        mid = f"atrim=start_sample={self.start}" + (f":end_sample={self.end}" if self.end is not None else "")
        parts.append(f"[vfx{i}]{mid},asetpts=PTS-STARTPTS,{core}[vmid]")
        labels.append("[vmid]")
        i += 1
        if self.end is not None:
            parts.append(f"[vfx{i}]atrim=start_sample={self.end},asetpts=PTS-STARTPTS[vpost]")
            labels.append("[vpost]")
        parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1{out_label}")
        return parts


def noise_floor(sources, pre_chain: str, windows: int = 24, win: float = 3.0) -> float | None:
    """素材のあちこちから少しずつ音を読み、雑音の大きさ（静かな所の音量, dBFS）を見積もる

    sources: [(ファイル, 開始秒, 長さ秒)]。全体は読まないので、長い素材でもすぐ終わる
    """
    sources = [(Path(p), float(s), float(d)) for p, s, d in sources if d > 0.2]
    total = sum(d for _, _, d in sources)
    if total <= 0:
        return None
    levels = []
    for k in range(windows):
        pos = total * (k + 0.5) / windows
        for p, s, d in sources:
            if pos < d:
                break
            pos -= d
        start = s + max(0.0, min(pos, d - win))
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{min(win, d):.3f}", "-i", str(p),
                            "-vn", "-af", pre_chain + ",pan=mono|c0=c0", "-ar", str(SR), "-f", "f32le", "-"],
                           capture_output=True)
        y = np.frombuffer(r.stdout, dtype=np.float32)
        n = len(y) // 1440    # 30ms ごとの音量（ノイズ除去と同じ 48kHz で測る）
        if n:
            levels.append(np.sqrt((y[:n * 1440].reshape(n, 1440) ** 2).mean(axis=1) + 1e-12))
    if not levels:
        return None
    lv = np.concatenate(levels)
    return float(20 * np.log10(np.percentile(lv, 5) + 1e-9))   # 静かな 5% の音量


def voice_fx(st: Settings, body_start: float = 0.0, body_end: float | None = None,
             total: float | None = None, sources=None) -> VoiceFx | None:
    """本編の範囲（秒）だけに声の処理をかける VoiceFx を作る。sources は noise_floor() と同じ"""
    if not st.audio:
        return None
    nd = noise_floor(sources, lowcut_chain(st.audio)) if sources and st.audio["nr"] > 0.5 else None
    if nd is not None:
        print(f"      雑音の大きさ: {nd:.0f}dB（これを目安にノイズを減らします）", flush=True)
    chain = voice_chain(st.audio, nd)
    s = int(round(body_start * SR))
    e = int(round(body_end * SR)) if body_end is not None and total is not None and total - body_end > 0.01 else None
    return VoiceFx(chain, measure_delay(chain), s, e)


# ================================================================ 色（3D LUT）
def identity_grid(n: int = CUBE_SIZE) -> np.ndarray:
    """(n^3, 3) の RGB 格子。並びは .cube と同じ（R が一番速く変わる）"""
    v = np.linspace(0.0, 1.0, n)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    return np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)


def read_cube(path: Path) -> tuple[str, np.ndarray, np.ndarray, np.ndarray]:
    """.cube を読む。(種類 "1D"/"3D", データ, 範囲の最小, 最大)"""
    size3 = size1 = 0
    lo, hi = np.zeros(3), np.ones(3)
    rows = []
    try:
        text = Path(path).read_bytes().decode("utf-8-sig", errors="replace")
    except OSError as e:
        sys.exit(f"[エラー] LUT ファイルを読めませんでした: {path}\n  {e}")
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        key = ln.split()[0].upper()
        if key == "LUT_3D_SIZE":
            size3 = int(ln.split()[1])
        elif key == "LUT_1D_SIZE":
            size1 = int(ln.split()[1])
        elif key == "DOMAIN_MIN":
            lo = np.array([float(x) for x in ln.split()[1:4]])
        elif key == "DOMAIN_MAX":
            hi = np.array([float(x) for x in ln.split()[1:4]])
        elif key[0].isdigit() or key[0] in "-.":
            try:
                rows.append([float(x) for x in ln.split()[:3]])
            except ValueError:
                pass
    data = np.array(rows, dtype=np.float64)
    if size3 and len(data) == size3 ** 3:
        return "3D", data.reshape(size3, size3, size3, 3), lo, hi   # [b][g][r]
    if size1 and len(data) == size1:
        return "1D", data, lo, hi
    sys.exit(f"[エラー] LUT ファイルの形式が読めませんでした: {path}\n"
             "  .cube 形式（LUT_3D_SIZE または LUT_1D_SIZE）のファイルを指定してください。")


def apply_cube(rgb: np.ndarray, cube) -> np.ndarray:
    """読み込んだ LUT を RGB(0〜1, (N,3)) に当てる（3Dは三線形補間）"""
    kind, data, lo, hi = cube
    x = np.clip((rgb - lo) / np.maximum(hi - lo, 1e-9), 0, 1)
    if kind == "1D":
        n = len(data)
        pos = np.linspace(0, 1, n)
        return np.stack([np.interp(x[:, c], pos, data[:, c]) for c in range(3)], axis=1)
    n = data.shape[0]
    f = x * (n - 1)
    i0 = np.clip(np.floor(f).astype(int), 0, n - 2)
    t = f - i0
    r0, g0, b0 = i0[:, 0], i0[:, 1], i0[:, 2]
    tr, tg, tb = t[:, :1], t[:, 1:2], t[:, 2:3]
    out = 0
    for db, wb in ((0, 1 - tb), (1, tb)):
        for dg, wg in ((0, 1 - tg), (1, tg)):
            for dr, wr in ((0, 1 - tr), (1, tr)):
                out = out + data[b0 + db, g0 + dg, r0 + dr] * (wb * wg * wr)
    return out


def write_cube(path: Path, values: np.ndarray, title: str, n: int = CUBE_SIZE) -> None:
    v = np.clip(values, 0, 1)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f'TITLE "{title}"\nLUT_3D_SIZE {n}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        f.write("\n".join(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in v))
        f.write("\n")


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722])


def grab_frames(path: Path, times: list[float], width: int = 192) -> np.ndarray:
    """指定した時刻の静止画を小さくして取り出す。(画素数, 3) の 0〜1 の配列"""
    out = []
    for t in times:
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
                            "-vf", f"scale={width}:-2,format=rgb24", "-f", "rawvideo", "-"], capture_output=True)
        if p.returncode == 0 and p.stdout:
            out.append(np.frombuffer(p.stdout, dtype=np.uint8).reshape(-1, 3))
    if not out:
        return np.zeros((0, 3))
    return np.concatenate(out).astype(np.float64) / 255.0


def auto_params(px: np.ndarray, strength: float) -> dict:
    """静止画の画素から、控えめな明るさ・色かぶりの補正量を決める"""
    if len(px) < 100 or strength <= 0:
        return dict(lo=0.0, hi=1.0, gains=[1.0, 1.0, 1.0], gamma=1.0)
    y = _luma(px)
    lo, hi = np.percentile(y, 0.5), np.percentile(y, 99.5)
    # 黒の浮き・白のくすみを少しだけ直す（伸ばしすぎない）
    lo2 = min(lo, 0.12) * strength
    hi2 = 1 - (1 - max(hi, 0.80)) * strength
    if hi2 - lo2 < 0.5:
        lo2, hi2 = 0.0, 1.0
    # 色かぶり: 灰色に近い画素の平均が灰色になるように（明るい・暗い所と、色の濃い所は使わない）
    sat = px.max(axis=1) - px.min(axis=1)
    m = (y > 0.15) & (y < 0.9) & (sat < 0.12)
    gains = np.ones(3)
    if m.mean() > 0.02:
        mean = px[m].mean(axis=0)
        gains = mean.mean() / np.maximum(mean, 1e-3)
        gains = np.clip(gains, 0.88, 1.12)
        gains = 1 + (gains - 1) * strength
    # 全体が暗すぎる・明るすぎるときだけ、中間の明るさを少し動かす
    yl = np.clip((y - lo2) / (hi2 - lo2), 1e-4, 1)
    med = float(np.median(yl))
    gamma = 1.0
    if med < 0.36 or med > 0.6:
        target = 0.36 if med < 0.36 else 0.6
        gamma = float(np.clip(np.log(target) / np.log(max(med, 1e-3)), 0.8, 1.25))
        gamma = 1 + (gamma - 1) * strength
    return dict(lo=float(lo2), hi=float(hi2), gains=[float(g) for g in gains], gamma=float(gamma))


def color_transform(rgb: np.ndarray, cfg: dict, auto: dict | None, user_cube=None) -> np.ndarray:
    """RGB(0〜1) に、自動補正 → 手動調整 → LUT の順で当てる"""
    x = rgb.copy()
    if auto:
        x = np.clip((x - auto["lo"]) / (auto["hi"] - auto["lo"]), 0, 1)
        x = np.clip(x * np.array(auto["gains"]), 0, 1)
        if abs(auto["gamma"] - 1) > 1e-4:
            x = x ** auto["gamma"]   # 1より小さいと明るく、大きいと暗く
    m = cfg.get("manual")
    if m:
        t = m["temperature"] * 0.08    # +で暖かく（赤み）、-で涼しく（青み）
        x = x * np.array([1 + t, 1.0, 1 - t])
        x = (x - 0.5) * m["contrast"] + 0.5 + m["brightness"] * 0.5
        yy = _luma(np.clip(x, 0, 1))[:, None]
        x = yy + (x - yy) * m["saturation"]
        x = np.clip(x, 0, 1)
    if user_cube is not None:
        k = cfg.get("lut_strength", 1.0)
        x = x + (apply_cube(x, user_cube) - x) * k
    return np.clip(x, 0, 1)


def build_color_cubes(cfg: dict, files: dict[Path, list[float]], work: Path) -> dict[Path, str]:
    """素材ファイルごとの補正 LUT を作業フォルダに書き、{素材: ファイル名（作業フォルダからの相対）} を返す"""
    user = read_cube(Path(cfg["lut"])) if cfg.get("lut") else None
    grid = identity_grid()
    out = {}
    shared = None
    for k, (path, times) in enumerate(files.items(), 1):
        auto = None
        if cfg["auto"]:
            ts = sorted(times)
            if len(ts) > 12:
                ts = [ts[round(i * (len(ts) - 1) / 11)] for i in range(12)]
            auto = auto_params(grab_frames(path, ts), cfg["strength"])
            name = f"color_{k:03d}.cube"
            write_cube(work / name, color_transform(grid, cfg, auto, user), f"auto {Path(path).name}")
            print(f"      色の自動補正 {Path(path).name}: 黒 {auto['lo']:.2f} 白 {auto['hi']:.2f} "
                  f"色 {'/'.join(f'{g:.2f}' for g in auto['gains'])} 明るさ {1 / auto['gamma']:.2f}", flush=True)
        else:
            name = "color.cube"   # 自動補正なしなら、全部の素材で同じ LUT
            if shared is None:
                write_cube(work / name, color_transform(grid, cfg, None, user), "color")
                shared = name
        out[path] = name
    return out


# ================================================================ 映像のフィルター
@dataclass
class VideoFx:
    """rc_render.Seg.fx に入れる、1区間ぶんの映像の補正"""
    cube: str = ""        # 作業フォルダ内の .cube（相対パス）
    stab: str = ""        # "vidstab" / "deshake" / ""
    strength: float = 0.5
    fps: float = 30.0

    def detect(self, trf: str) -> str:
        sh = int(round(3 + 5 * self.strength))
        return f"format=yuv420p,vidstabdetect=shakiness={sh}:accuracy=5:stepsize=12:result={trf}"

    def chain(self, trf: str) -> str:
        """scale の後・pad の前に入れるフィルター（末尾に , が付く。何もなければ空）"""
        f = []
        # vidstab / deshake は yuv420p で動かす（後ろの lut3d が RGB を求めても、間に変換が入るように）
        if self.stab == "vidstab":
            sm = max(4, int(round(self.fps * self.strength)))
            # optzoom=1: 黒い縁が出ないように少し拡大 / maxangle: 傾きの直しすぎを防ぐ（約6度まで）
            f.append(f"format=yuv420p,vidstabtransform=input={trf}:smoothing={sm}:optzoom=1:maxangle=0.1:"
                     "interpol=linear,format=yuv420p")
        elif self.stab == "deshake":
            r = int(round(16 + 32 * self.strength))
            f.append(f"format=yuv420p,deshake=rx={r}:ry={r}:edge=mirror:search=less,format=yuv420p")
        if self.cube:
            f.append(f"lut3d=file={self.cube}:interp=tetrahedral")
        return "".join(x + "," for x in f)


def trf_name(out_name: str, i: int) -> str:
    return f"{Path(out_name).stem}_{i:02d}.trf"


def detect_cmd(segs, w: int, h: int, fps: Fraction, out_name: str, pre_chain) -> list[str] | None:
    """vidstab の1回目（揺れの解析）のコマンド。対象がなければ None。pre_chain(seg) は scale までの並び"""
    targets = [(i, s) for i, s in enumerate(segs) if getattr(s, "fx", None) and s.fx.stab == "vidstab"]
    if not targets:
        return None
    cmd = ["ffmpeg", "-y", "-v", "error"]
    parts, maps = [], []
    for j, (i, s) in enumerate(targets):
        # 早送りの区間（rc_speedup）は、素材を倍率ぶん長く読む（書き出しと同じ読み方）
        speed = Fraction(getattr(s, "speed", 1.0) or 1.0).limit_denominator(1000)
        dur = s.frames / fps * speed + Fraction(1, 10)
        # 2台目カメラの区間（rc_multicam）は、映像のファイル（video）を解析する
        vpath, vstart = (s.video, s.video_start) if getattr(s, "video", None) is not None else (s.path, s.start)
        cmd += ["-ss", f"{vstart:.4f}", "-t", f"{float(dur):.4f}", "-i", str(vpath)]
        # 書き出しと同じフレームを解析するため、フレーム数も同じところで切る
        parts.append(f"[{j}:v]{pre_chain(s)},{s.fx.detect(trf_name(out_name, i))}[d{j}]")
        maps += ["-map", f"[d{j}]", "-f", "null", "-"]
    return cmd + ["-filter_complex", ";".join(parts)] + maps


class StabPipeline:
    """手ブレの解析（vidstab の1回目）を、まとまりごとに裏で進める。

    まとまり k を書き出している間に、k+1 の解析を進めておく（CPU の空いている分を使う）。
    """

    def __init__(self, batches, w: int, h: int, fps: Fraction, work: Path, pre_chain):
        self.work = Path(work)
        self.cmds = {k: detect_cmd(b, w, h, fps, f"part_{k:04d}.mkv", pre_chain) for k, b in enumerate(batches, 1)}
        self.procs: dict[int, tuple] = {}
        self.total = sum(1 for c in self.cmds.values() if c)
        import atexit
        atexit.register(self.stop)
        self._start(1)

    def _start(self, k: int) -> None:
        cmd = self.cmds.get(k)
        if cmd and k not in self.procs:
            import tempfile
            err = tempfile.TemporaryFile()
            self.procs[k] = (subprocess.Popen(cmd, cwd=self.work, stdout=subprocess.DEVNULL, stderr=err), err)

    def wait(self, k: int) -> None:
        self._start(k)
        item = self.procs.pop(k, None)
        if item:
            p, err = item
            if p.poll() is None:
                print("      手ブレの解析を待っています", flush=True)
            p.wait()
            if p.returncode != 0:
                err.seek(0)
                tail = "\n".join(err.read().decode("utf-8", "replace").strip().splitlines()[-10:])
                err.close()
                sys.exit(f"[エラー] FFmpeg（手ブレの解析 {k}）が失敗しました:\n{tail}\n"
                         "  テンプレートの [stabilize] の enabled = false にすると、手ブレ補正なしで書き出せます。")
            err.close()
        self._start(k + 1)

    def skip(self, k: int) -> None:
        """まとまり k は前回書き出したパーツを使い回す（rc_ops）ので、解析しない（動いていれば止める）"""
        self.cmds.pop(k, None)
        item = self.procs.pop(k, None)
        if item:
            p, err = item
            if p.poll() is None:
                p.kill()
                p.wait()
            err.close()

    def stop(self) -> None:
        for p, err in self.procs.values():
            if p.poll() is None:
                p.kill()
                p.wait()
            err.close()
        self.procs.clear()


def _video_of(s) -> tuple[Path, float]:
    """区間の映像のファイルと開始秒（2台目カメラの区間は video の方）"""
    if getattr(s, "video", None) is not None:
        return Path(s.video), float(s.video_start)
    return Path(s.path), float(s.start)


def apply_video(st: Settings, segs, work: Path, fps: Fraction) -> None:
    """本編の区間（rc_render.Seg）に映像の補正を割り当てる。オープニング・エンディングには付けない"""
    if not (st.color or st.stab):
        return
    cubes = {}
    if st.color:
        files: dict[Path, list[float]] = {}
        for s in segs:
            files.setdefault(_video_of(s)[0], []).append(_video_of(s)[1] + float(s.frames / fps) / 2)
        cubes = build_color_cubes(st.color, files, work)
    for s in segs:
        s.fx = VideoFx(cube=cubes.get(_video_of(s)[0], ""), stab=(st.stab or {}).get("method", ""),
                       strength=(st.stab or {}).get("strength", 0.5), fps=float(fps))


# ================================================================ 2台目カメラの色合わせ
def _quantile_curves(src: np.ndarray, ref: np.ndarray, strength: float) -> list[tuple[np.ndarray, np.ndarray]]:
    """色ごとに、src の明るさの分布を ref に寄せる曲線（入力, 出力）を作る"""
    q = np.linspace(0.01, 0.99, 25)
    curves = []
    for c in range(3):
        a = np.percentile(src[:, c], q * 100)
        b = np.percentile(ref[:, c], q * 100)
        xs = np.concatenate([[0.0], a, [1.0]])
        ys = np.concatenate([[0.0], b, [1.0]])
        # 単調にする（同じ入力が続くとき・逆転するときは平らにならす）
        xs = np.maximum.accumulate(xs + np.arange(len(xs)) * 1e-6)
        ys = np.maximum.accumulate(ys)
        # 極端な曲げ方は抑える（傾き 0.5〜2.0）
        ys2 = [ys[0]]
        for k in range(1, len(xs)):
            dx = xs[k] - xs[k - 1]
            ys2.append(min(max(ys[k], ys2[-1] + 0.5 * dx), ys2[-1] + 2.0 * dx))
        ys = np.clip(np.array(ys2), 0, 1)
        ys = xs + (ys - xs) * strength
        curves.append((xs, ys))
    return curves


def match_cube(src_px: np.ndarray, ref_px: np.ndarray, strength: float = 0.8) -> np.ndarray:
    curves = _quantile_curves(src_px, ref_px, strength)
    g = identity_grid()
    return np.stack([np.interp(g[:, c], *curves[c]) for c in range(3)], axis=1)


def match_cam2(out_dir: Path, strength: float = 0.8, samples: int = 16) -> Path:
    """荒編集の結果（session.json と sync.json）から、2台目カメラの色を本編に寄せる LUT を作る"""
    from rc_media import SourceSet, probe
    out_dir = Path(out_dir).resolve()
    sp = out_dir / "session.json"
    if not sp.exists():
        sys.exit(f"[エラー] {sp} がありません。先に荒編集（roughcut.py）を実行してください。")
    sess = json.loads(sp.read_text(encoding="utf-8"))
    cam2 = (sess.get("args") or {}).get("cam2") or []
    if not cam2:
        sys.exit("[エラー] この荒編集には2台目カメラの素材がありません。\n"
                 "  「作成」タブの「2台目を追加…」で素材を入れて、荒編集し直してください。")
    syncp = out_dir / "sync.json"
    offset = json.loads(syncp.read_text(encoding="utf-8")).get("offset_sec", 0.0) if syncp.exists() else 0.0
    main = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    sub = SourceSet([probe(Path(p)) for p in cam2])
    # 2台が同時に撮っている時間（本編の時刻）から、均等に選ぶ
    a, b = max(0.0, -offset), min(main.duration, sub.duration - offset)
    if b - a < 1:
        a, b = 0.0, main.duration
        print("  [注意] 2台が同時に撮っている時間が見つからないので、それぞれ全体から色を調べます")
        times = [(a + (b - a) * (i + 0.5) / samples, sub.duration * (i + 0.5) / samples) for i in range(samples)]
    else:
        times = [(t, t + offset) for t in (a + (b - a) * (i + 0.5) / samples for i in range(samples))]

    def grab(ss: SourceSet, t: float) -> np.ndarray:
        for fi, ls, _ in ss.split(t, t + 0.05):
            return grab_frames(ss.files[fi].path, [ls])
        return np.zeros((0, 3))
    ref = np.concatenate([grab(main, t) for t, _ in times])
    src = np.concatenate([grab(sub, t) for _, t in times])
    if len(ref) < 100 or len(src) < 100:
        sys.exit("[エラー] 素材から静止画を取り出せませんでした。素材のファイルが移動していないか確認してください。")
    dst = out_dir / "cam2_色合わせ.cube"
    write_cube(dst, match_cube(src, ref, strength), "cam2 to main")
    mr, ms = ref.mean(axis=0), src.mean(axis=0)
    print(f"  本編の色の平均 R{mr[0]:.2f} G{mr[1]:.2f} B{mr[2]:.2f} / "
          f"2台目 R{ms[0]:.2f} G{ms[1]:.2f} B{ms[2]:.2f}")
    return dst


# ================================================================ 補正前後の比較
def _duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        sys.exit(f"[エラー] 動画を読めませんでした: {path}")


def compare_image(video: Path, st: Settings, out: Path, t: float | None = None) -> Path:
    """1枚の静止画で、色の補正の前（左）と後（右）を並べた画像を作る"""
    import shutil
    import tempfile
    video = Path(video).resolve()
    if not video.exists():
        sys.exit(f"[エラー] 動画が見つかりません: {video}")
    dur = _duration(video)
    t = dur / 2 if t is None else t
    out = Path(out).resolve()
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        chain = "null"
        if st.color:
            cube = build_color_cubes(st.color, {video: [dur * (i + 0.5) / 12 for i in range(12)]}, work)[video]
            chain = f"lut3d=file={cube}:interp=tetrahedral"
        p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                            "-filter_complex", f"[0:v]scale=960:-2,split[a][b];[b]{chain}[c];[a][c]hstack",
                            "cmp" + out.suffix], cwd=work, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if p.returncode != 0:
            sys.exit(f"[エラー] 比較画像を作れませんでした:\n{p.stderr[-800:]}")
        shutil.copyfile(work / ("cmp" + out.suffix), out)
    return out
