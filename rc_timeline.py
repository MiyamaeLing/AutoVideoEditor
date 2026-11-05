"""残す区間の計算と、元素材の時刻 → 編集後タイムラインの時刻 への変換。"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from rc_media import SourceSet
from rc_transcribe import Chunk

# 撮影ジャンルごとのカットの強さ
PRESETS = {
    # レビュー・解説：喋っていない所はどんどん詰める
    "talk": dict(pad_before=0.15, pad_after=0.25, merge_gap=0.35, keep_silence_over=None,
                 cut_silence=True, apply_ai_cuts=True, min_keep=0.3),
    # 工作：喋りの間は詰めるが、長い無言区間（作業シーン）は残して「早送り候補」マーカーを付ける
    "craft": dict(pad_before=0.2, pad_after=0.35, merge_gap=0.6, keep_silence_over=4.0,
                  cut_silence=True, apply_ai_cuts=True, min_keep=0.3),
    # 屋外企画：景色や環境音も大事なので自動ではカットしない。NG候補などはマーカーで示すだけ
    "outdoor": dict(pad_before=0.3, pad_after=0.5, merge_gap=1.0, keep_silence_over=None,
                    cut_silence=False, apply_ai_cuts=False, min_keep=0.3),
}


def _merge(regions, gap):
    regions = sorted(r for r in regions if r[1] > r[0])
    out = []
    for s, e in regions:
        if out and s - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def compute_keep(chunks: list[Chunk], cut_ids: set[int], total: float, preset: dict):
    """残す区間 [(start, end)] と、工作モードの無言作業区間のリストを返す。"""
    silent_work = []
    if not preset["cut_silence"]:
        regions = [[0.0, total]]
        regions = _subtract(regions, [(c.start, c.end) for c in chunks if c.id in cut_ids])
        return [(s, e) for s, e in regions if e - s >= preset["min_keep"]], silent_work

    pb, pa = preset["pad_before"], preset["pad_after"]
    regions = [[max(0.0, c.start - pb), min(total, c.end + pa)] for c in chunks if c.id not in cut_ids]

    thr = preset["keep_silence_over"]
    if thr:
        # 全チャンク（カットするものも含む）の間にある長い無言区間＝作業シーンとして残す
        bounds = [(0.0, 0.0)] + [(c.start, c.end) for c in chunks] + [(total, total)]
        for (_, prev_end), (next_start, _) in zip(bounds, bounds[1:]):
            if next_start - prev_end >= thr:
                s, e = prev_end + pa, next_start - pb
                if e - s >= thr * 0.5:
                    regions.append([s, e])
                    silent_work.append((s, e))

    merged = _merge(regions, preset["merge_gap"])
    # カット対象チャンクに余白(pad)がかぶらないように削る
    cut_ranges = [(c.start, c.end) for c in chunks if c.id in cut_ids]
    merged = _subtract(merged, cut_ranges)
    return [(s, e) for s, e in merged if e - s >= preset["min_keep"]], silent_work


def _subtract(regions, cuts):
    out = []
    for s, e in regions:
        pieces = [[s, e]]
        for cs, ce in cuts:
            nxt = []
            for ps, pe in pieces:
                if ce <= ps or cs >= pe:
                    nxt.append([ps, pe])
                    continue
                if cs > ps:
                    nxt.append([ps, cs])
                if ce < pe:
                    nxt.append([ce, pe])
            pieces = nxt
        out.extend(pieces)
    return out


@dataclass
class Piece:
    """タイムライン上の1クリップ。"""
    file_index: int
    src_in: int      # 素材ファイル内のフレーム（素材のfps）
    src_out: int
    seq_start: int   # シーケンス上のフレーム（シーケンスのfps）
    seq_end: int
    g_in: float      # 全体時刻（秒）
    g_out: float


class Timeline:
    def __init__(self, sources: SourceSet, keep: list[tuple[float, float]], seq_fps: Fraction):
        self.sources = sources
        self.fps = seq_fps
        self.pieces: list[Piece] = []
        cur = 0
        for s, e in keep:
            for fi, ls, le in sources.split(s, e):
                f = sources.files[fi]
                fin, fout = round(ls * f.fps), round(le * f.fps)
                fout = min(fout, f.frames)
                if fout <= fin:
                    continue
                seq_len = round((fout - fin) * seq_fps / f.fps)
                off = sources.offsets[fi]
                self.pieces.append(Piece(
                    fi, fin, fout, cur, cur + seq_len,
                    off + fin / f.fps, off + fout / f.fps,
                ))
                cur += seq_len
        self.total_frames = cur

    @property
    def duration(self) -> float:
        return self.total_frames / float(self.fps)

    def map_time(self, t: float) -> float | None:
        """元素材の時刻 → 編集後の時刻（カットされた位置なら None）"""
        for p in self.pieces:
            if p.g_in <= t < p.g_out:
                return p.seq_start / float(self.fps) + (t - p.g_in)
        return None

    def map_time_nearest(self, t: float) -> float:
        """カットされた位置なら、その直後に残っている位置に寄せる。"""
        m = self.map_time(t)
        if m is not None:
            return m
        for p in self.pieces:
            if p.g_in >= t:
                return p.seq_start / float(self.fps)
        return self.duration

    def map_range(self, s: float, e: float) -> list[tuple[float, float]]:
        out = []
        for p in self.pieces:
            a, b = max(s, p.g_in), min(e, p.g_out)
            if b - a > 0.05:
                base = p.seq_start / float(self.fps)
                out.append((base + a - p.g_in, base + b - p.g_in))
        return out
