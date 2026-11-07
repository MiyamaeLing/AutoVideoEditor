"""素材ファイルの情報取得（ffprobe）と、分割ファイルを1本の連続素材として扱うための仕組み。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

# よく使われるフレームレート。スマホの可変フレームレート素材を最も近い値に寄せるために使う
STANDARD_FPS = [
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
    Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120000, 1001), Fraction(120),
]


def require_ffmpeg() -> None:
    # winget で入れた直後は PATH が反映されていないことがあるので、既定の場所も探す
    if shutil.which("ffmpeg") is None and os.name == "nt":
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
        if (links / "ffmpeg.exe").exists():
            os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(
                f"[エラー] {tool} が見つかりません。\n"
                "  PowerShellで `winget install Gyan.FFmpeg` を実行し、ターミナルを開き直してください。"
            )


def _parse_rate(s: str | None) -> Fraction | None:
    if not s or s in ("0/0", "0"):
        return None
    try:
        r = Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None
    return r if r > 0 else None


def snap_fps(r: Fraction) -> Fraction:
    best = min(STANDARD_FPS, key=lambda s: abs(float(s) - float(r)))
    if abs(float(best) - float(r)) / float(best) < 0.02:
        return best
    return r.limit_denominator(1001)


@dataclass
class MediaInfo:
    path: Path
    duration: float
    fps: Fraction
    width: int
    height: int
    audio_channels: int
    sample_rate: int
    vfr_suspect: bool = False

    @property
    def frames(self) -> int:
        return int(round(self.duration * float(self.fps)))


def probe(path: Path) -> MediaInfo:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        sys.exit(f"[エラー] ffprobe が {path} を読めませんでした:\n{proc.stderr}")
    info = json.loads(proc.stdout)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        sys.exit(f"[エラー] {path} に映像トラックがありません。")
    if a is None:
        sys.exit(f"[エラー] {path} に音声トラックがありません（音声がないと文字起こしできません）。")

    avg = _parse_rate(v.get("avg_frame_rate"))
    raw = _parse_rate(v.get("r_frame_rate"))
    base = avg or raw or Fraction(30)
    fps = snap_fps(base)
    vfr = bool(avg and raw and abs(float(avg) - float(raw)) / float(raw) > 0.01)

    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    # 縦向きスマホ動画は rotation が付くので幅と高さを入れ替える
    w, h = int(v.get("width", 1920)), int(v.get("height", 1080))
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    if abs(rot) % 180 == 90:
        w, h = h, w

    return MediaInfo(
        path=path.resolve(), duration=dur, fps=fps, width=w, height=h,
        audio_channels=int(a.get("channels", 2)), sample_rate=int(a.get("sample_rate", 48000)),
        vfr_suspect=vfr,
    )


@dataclass
class SourceSet:
    """カメラが自動分割した複数ファイル（例: C0001.MP4, C0002.MP4）を、つながった1本として扱う。"""
    files: list[MediaInfo]
    offsets: list[float] = field(default_factory=list)

    def __post_init__(self):
        t = 0.0
        self.offsets = []
        for f in self.files:
            self.offsets.append(t)
            t += f.duration

    @property
    def duration(self) -> float:
        return self.offsets[-1] + self.files[-1].duration

    @property
    def main(self) -> MediaInfo:
        return self.files[0]

    def split(self, start: float, end: float):
        """全体時刻の区間 [start, end) を、(ファイル番号, ファイル内in, ファイル内out) のリストに分ける。"""
        out = []
        for i, (f, off) in enumerate(zip(self.files, self.offsets)):
            s, e = max(start, off), min(end, off + f.duration)
            if e - s > 1e-3:
                out.append((i, s - off, e - off))
        return out


# ---------------------------------------------------------------- 結果フォルダ（○○_roughcut）の場所
SOURCES_FILE = "sources.json"   # どの素材の結果か（保存先を選んだときに、同じ名前の別の動画と区別するため）


def _same_path(p) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def folder_inputs(d: Path) -> list[str] | None:
    """結果フォルダが、どの素材の結果か（sources.json か session.json の inputs）。分からなければ None"""
    for name in (SOURCES_FILE, "session.json"):
        f = Path(d) / name
        if not f.is_file():
            continue
        try:
            v = json.loads(f.read_text(encoding="utf-8-sig")).get("inputs")
        except (OSError, ValueError, AttributeError):
            continue
        if v:
            return [_same_path(p) for p in v]
    return None


def result_dir(inputs, base=None, reserve: bool = True) -> Path:
    """荒編集の結果フォルダ。

    base が無ければ、今まで通り素材と同じ場所の「<名前>_roughcut」。
    base（画面で選んだ保存先）があればその中に作る。カメラのファイル名（C0001.MP4 など）は撮影ごとに同じなので、
    同じ名前のフォルダが別の素材の結果なら「<名前>_2_roughcut」「<名前>_3_roughcut」…にする（前の結果を上書きしない）。
    reserve: 決めたフォルダを作って sources.json を書き、あとから同じ名前の別の素材が来ても分かるようにする
    （まとめて処理で、まだ動かしていない動画どうしがぶつからないように）。False なら決めるだけ（画面の表示用）
    """
    first = Path(inputs[0])
    if not base:
        return first.parent / f"{first.stem}_roughcut"
    want = [_same_path(p) for p in inputs]
    n = 1
    while True:
        d = Path(base) / (f"{first.stem}_roughcut" if n == 1 else f"{first.stem}_{n}_roughcut")
        if not d.exists():
            break
        if d.is_dir() and (not any(d.iterdir()) or folder_inputs(d) == want):
            break   # 空のフォルダか、同じ素材の結果（作り直し・途中からの再開）
        n += 1
    if reserve:
        d.mkdir(parents=True, exist_ok=True)
        (d / SOURCES_FILE).write_text(json.dumps({"inputs": [str(Path(p).resolve()) for p in inputs]},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    return d.resolve() if reserve else d
