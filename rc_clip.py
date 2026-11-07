"""完成動画の一部（区間）だけを書き出す。

「確認・仕上げ」タブの「区間を書き出す…」（gui_ext_clip.py）と clip.py から使う。
完成動画（○○_完成.mp4）の開始〜終了をコマ単位で正確に切り出して、結果フォルダの clips フォルダに 1本の動画にする。
字幕ファイル（○○_完成.srt）があれば、区間の分だけ時刻をずらした SRT も一緒に作る（YouTube や SNS に字幕を付けるとき用）。
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

CLIP_DIR = "clips"
MIN_SEC = 0.5   # これより短い区間は書き出さない（押し間違いで 0秒の動画ができないように）


# ---------------------------------------------------------------- 時刻
def parse_time(text) -> float:
    """「83」「1:23」「1:23.5」「1:02:03」「１：２３」（全角）「1分23秒」を秒にする。読めなければ ValueError"""
    s = unicodedata.normalize("NFKC", str(text)).strip().replace(" ", "")
    m = re.fullmatch(r"(?:(\d+)時間)?(?:(\d+)分)?(?:(\d+(?:\.\d+)?)秒)?", s)
    if s and m and any(m.groups()):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    parts = s.split(":")
    ok = (s and len(parts) <= 3 and all(re.fullmatch(r"\d+", p) for p in parts[:-1])
          and re.fullmatch(r"\d+(\.\d+)?", parts[-1]))
    if not ok:
        raise ValueError(f"時刻を読めません: {text}（例: 1:23 / 1:23.5 / 83）")
    v = 0.0
    for p in parts:
        v = v * 60 + float(p)
    return v


def fmt_time(t: float) -> str:
    """画面に出す時刻（0.1秒まで）。例: 1:23.4 / 1:02:03.4"""
    t = max(0.0, round(float(t), 1))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m)}:{s:04.1f}"


def _name_time(t: float) -> str:
    """ファイル名に入れる時刻。例: 0m05s / 1m23s / 1h02m03s"""
    t = int(math.floor(max(0.0, t) + 1e-6))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


# ---------------------------------------------------------------- 完成動画と章
def final_video(target: Path) -> tuple[Path, Path]:
    """(完成動画, 結果フォルダ)。target は結果フォルダ（○○_roughcut）か、完成動画そのもの"""
    target = Path(target)
    if target.is_file():
        return target.resolve(), target.resolve().parent
    try:
        stem = json.loads((target / "session.json").read_text(encoding="utf-8-sig"))["stem"]
    except (OSError, ValueError, KeyError):
        raise ValueError(f"荒編集の結果フォルダではありません（session.json がありません）: {target}") from None
    video = target / f"{stem}_完成.mp4"
    if not video.is_file():
        raise ValueError(f"完成動画がまだありません: {video.name}\n先に「完成動画を書き出す」を押してください。")
    return video.resolve(), target.resolve()


def chapters(out_dir: Path, duration: float | None = None) -> list[dict]:
    """完成動画の章（final_info.json）。[{title, start, end}]。end は次の章の頭（最後は動画の終わり）"""
    try:
        info = json.loads((Path(out_dir) / "final_info.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    total = float(duration or info.get("duration") or 0)
    marks = sorted((float(c["t"]), str(c.get("title", ""))) for c in info.get("chapters", []) if "t" in c)
    out = []
    for i, (t, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else total
        if end - t >= MIN_SEC:
            out.append({"title": title, "start": t, "end": end})
    return out


def clip_path(video: Path, start: float, end: float) -> Path:
    """書き出すファイル。例: ○○_roughcut/clips/C0001_完成_1m23s-2m10s.mp4"""
    video = Path(video)
    return video.parent / CLIP_DIR / f"{video.stem}_{_name_time(start)}-{_name_time(end)}.mp4"


# ---------------------------------------------------------------- 字幕
def clip_captions(cues: list[tuple[float, float, str]], start: float, end: float) -> list[tuple[float, float, str]]:
    """区間にかかる字幕だけを残し、区間の頭を 0秒にする（区間の境目にまたがる字幕は、境目で切る）"""
    out = []
    for s, e, t in cues:
        a, b = max(s, start) - start, min(e, end) - start
        if b - a >= 0.05:
            out.append((round(a, 3), round(b, 3), t))
    return out


# ---------------------------------------------------------------- 書き出し
def export(video: Path, start: float, end: float, out_path: Path | None = None, tpl=None,
           with_srt: bool = True) -> dict:
    """完成動画の start〜end 秒を書き出す。コマ単位で正確に切るため、映像は作り直す（GPU があれば NVENC で速い）。

    戻り値: {"video": 書き出した動画, "srt": 字幕（作らなかったら None）, "start", "end", "duration"}
    区間がおかしいとき（逆・短すぎる・動画の外）は ValueError
    """
    import rc_media
    import rc_render
    video = Path(video)
    info = rc_media.probe(video)
    start, end = max(0.0, float(start)), min(float(end), info.duration)
    if start >= info.duration:
        raise ValueError(f"開始（{fmt_time(start)}）が動画の長さ（{fmt_time(info.duration)}）を超えています。")
    if end - start < MIN_SEC:
        raise ValueError(f"区間が短すぎます（{end - start:.1f}秒）。開始より {MIN_SEC}秒以上あとを終了にしてください。")
    v = (tpl.get("video", {}) or {}) if tpl is not None else {}
    final_args, _, enc = rc_render.pick_encoder(str(v.get("encoder", "auto")), int(v.get("quality", 19)))
    out_path = Path(out_path) if out_path else clip_path(video, start, end)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".書き出し中.mp4")   # 途中で止まっても、できかけのファイルを残さない
    dur = end - start
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video), "-map", "0:v:0"]
    if info.audio_channels:
        # 読み始めが映像のキーフレームに重なると、FFmpeg 9 では最初の音声が少し後ろから始まるので、空きを無音で埋める
        cmd += ["-map", "0:a:0", "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", "320k"]
    cmd += [*final_args, "-movflags", "+faststart", str(tmp)]
    print(f"  エンコーダ: {enc}", flush=True)
    try:
        rc_render.run_progress(cmd, dur, what="区間の書き出し")
        tmp.replace(out_path)
    finally:
        tmp.unlink(missing_ok=True)
    srt_out = None
    srt = video.with_suffix(".srt")
    if with_srt and srt.is_file():
        import rc_export
        import rc_i18n
        cues = clip_captions(rc_i18n.read_srt(srt, join="\n"), start, end)
        if cues:
            srt_out = out_path.with_suffix(".srt")
            rc_export.write_srt(srt_out, cues)
    return {"video": out_path, "srt": srt_out, "start": start, "end": end, "duration": dur}
