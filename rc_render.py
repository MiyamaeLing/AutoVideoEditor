"""FFmpeg による完成動画の書き出し。

1. カット・ズームを反映した本編を、数十カットずつ高画質の中間ファイルに書き出してつなぐ
2. 声・BGM（喋っている間は自動で下げる）・効果音を混ぜ、YouTube基準の音量にそろえる
3. 字幕・テロップ・章カード・ロゴを焼き込んで、最終エンコード
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from rc_template import Template

BATCH = 20
SR = 48000


@dataclass
class Seg:
    path: Path
    start: float     # ファイル内の開始秒
    frames: int      # 出力フレーム数
    zoom: float = 1.0
    has_audio: bool = True


def run(cmd: list[str], cwd: Path | None = None, what: str = "") -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-15:])
        sys.exit(f"[エラー] FFmpeg（{what}）が失敗しました:\n{tail}")
    return p


def run_progress(cmd: list[str], total: float, cwd: Path | None = None, what: str = "") -> None:
    """FFmpegを実行しながら進捗(%)を表示する"""
    import tempfile
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:], cwd=cwd,
                                stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", errors="replace")
        last = -10
        for line in proc.stdout:
            if line.startswith("out_time_us=") and total > 0:
                try:
                    pct = int(int(line.split("=")[1]) / 1e6 / total * 100)
                except ValueError:
                    continue
                if pct >= last + 5:
                    last = pct
                    print(f"      {min(pct, 100)}%", flush=True)
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            tail = "\n".join(err.read().strip().splitlines()[-15:])
            sys.exit(f"[エラー] FFmpeg（{what}）が失敗しました:\n{tail}")


def has_audio(path: Path) -> bool:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(p.stdout.strip())


def media_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(p.stdout.strip() or 0)


# ---------- エンコーダの選択 ----------

def pick_encoder(pref: str, quality: int) -> tuple[list[str], list[str], str]:
    """(最終用の引数, 中間ファイル用の引数, 名前) を返す。NVENCが使えなければ libx264。"""
    cands = ["h264_nvenc", "libx264"] if pref == "auto" else [pref, "libx264"]
    for enc in cands:
        test = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.2",
                               "-c:v", enc, "-f", "null", "-"], capture_output=True)
        if test.returncode == 0:
            break
    if enc.endswith("_nvenc"):
        final = ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
        inter = ["-c:v", enc, "-preset", "p4", "-rc", "vbr", "-cq", str(max(quality - 5, 10)), "-b:v", "0"]
    else:
        final = ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)]
        inter = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(max(quality - 5, 10))]
    pix = ["-pix_fmt", "yuv420p"]
    return final + pix, inter + pix, enc


# ---------- 1. 本編 ----------

def _batch_cmd(segs: list[Seg], w: int, h: int, fps: Fraction, out: Path, enc_args: list[str]) -> list[str]:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for s in segs:
        dur = s.frames / fps + Fraction(1, 10)
        cmd += ["-ss", f"{s.start:.4f}", "-t", f"{float(dur):.4f}", "-i", str(s.path)]
    parts, labels = [], ""
    for i, s in enumerate(segs):
        d = float(s.frames / fps)
        zoom = f"crop=iw/{s.zoom:.3f}:ih/{s.zoom:.3f}," if s.zoom > 1.001 else ""
        parts.append(
            f"[{i}:v]{zoom}scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={s.frames},setpts=PTS-STARTPTS[v{i}]"
        )
        if s.has_audio:
            parts.append(f"[{i}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"apad=whole_dur={d:.4f},atrim=end={d:.4f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            parts.append(f"anullsrc=r={SR}:cl=stereo,atrim=end={d:.4f}[a{i}]")
        labels += f"[v{i}][a{i}]"
    parts.append(f"{labels}concat=n={len(segs)}:v=1:a=1[v][a]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
                  *enc_args, "-c:a", "pcm_s16le", str(out)]


def render_body(segs: list[Seg], w: int, h: int, fps: Fraction, work: Path, enc_args: list[str]) -> Path:
    batches = [segs[i:i + BATCH] for i in range(0, len(segs), BATCH)]
    names = []
    for k, b in enumerate(batches, 1):
        name = f"part_{k:04d}.mkv"
        print(f"    本編の書き出し {k}/{len(batches)}", flush=True)
        run(_batch_cmd(b, w, h, fps, work / name, enc_args), what=f"本編 {k}")
        names.append(name)
    (work / "parts.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "parts.txt", "-c", "copy", "body.mkv"],
        cwd=work, what="本編の結合")
    return work / "body.mkv"


# ---------- 2. 音声 ----------

def mix_audio(body: Path, total: float, bgm_sections, se_events, tpl: Template, work: Path) -> dict:
    """bgm_sections: [(開始, 終了, ファイル)] / se_events: [(時刻, ファイル)]。 mix.wav を作り、音量の測定値を返す。"""
    a = tpl.get("audio", {})
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(body)]
    parts = [f"[0:a]volume={a.get('voice_volume_db', 0)}dB,asplit=2[voice][key]"]
    idx = 1
    bgm_labels = []
    for s, e, f in bgm_sections:
        dur = e - s
        if dur < 1:
            continue
        cmd += ["-stream_loop", "-1", "-i", str(f)]
        fo = min(2.0, dur / 3)
        parts.append(
            f"[{idx}:a]aresample={SR},aformat=channel_layouts=stereo,atrim=end={dur:.3f},"
            f"afade=t=in:d={min(1.5, dur / 3):.2f},afade=t=out:st={dur - fo:.3f}:d={fo:.2f},"
            f"volume={a.get('bgm_volume_db', -18)}dB,adelay={int(s * 1000)}:all=1[b{idx}]")
        bgm_labels.append(f"[b{idx}]")
        idx += 1
    se_labels = []
    for t, f in se_events:
        cmd += ["-i", str(f)]
        parts.append(f"[{idx}:a]aresample={SR},aformat=channel_layouts=stereo,"
                     f"volume={a.get('se_volume_db', -6)}dB,adelay={int(t * 1000)}:all=1[s{idx}]")
        se_labels.append(f"[s{idx}]")
        idx += 1

    mix_inputs = ["[voice]"]
    if bgm_labels:
        ratio = max(1.0, float(a.get("bgm_duck", 8)))
        parts.append(f"{''.join(bgm_labels)}amix=inputs={len(bgm_labels)}:normalize=0:duration=longest[bgmraw]")
        parts.append(f"[bgmraw][key]sidechaincompress=threshold=0.015:ratio={ratio}:attack=30:release=500[bgm]")
        mix_inputs.append("[bgm]")
    else:
        parts.append("[key]anullsink")
    if se_labels:
        parts.append(f"{''.join(se_labels)}amix=inputs={len(se_labels)}:normalize=0:duration=longest[se]")
        mix_inputs.append("[se]")
    parts.append(f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:normalize=0:duration=first,"
                 f"atrim=end={total:.3f}[out]")
    run(cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", str(work / "mix.wav")],
        what="音声のミックス")

    target = a.get("loudness", -14.0)
    p = run(["ffmpeg", "-v", "info", "-hide_banner", "-i", str(work / "mix.wav"), "-af",
             f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], what="音量の測定")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    return json.loads(m.group(0)) if m else {}


# ---------- 3. 仕上げ ----------

def render_final(body: Path, mix: Path, loud: dict, ass_name: str, fonts: Path | None, logo: Path | None,
                 tpl: Template, w: int, h: int, enc_args: list[str], out: Path, work: Path) -> None:
    target = tpl.get("audio", {}).get("loudness", -14.0)
    vf = f"[0:v]ass={ass_name}" + (":fontsdir=fonts" if fonts else "")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(body), "-i", str(mix)]
    if fonts:
        dst = work / "fonts"
        if not dst.exists():
            shutil.copytree(fonts, dst)
    parts = []
    if logo:
        lc = tpl["logo"]
        cmd += ["-i", str(logo)]
        m = 40
        pos = {"top-left": f"{m}:{m}", "top-right": f"W-w-{m}:{m}",
               "bottom-left": f"{m}:H-h-{m}", "bottom-right": f"W-w-{m}:H-h-{m}"}[lc.get("position", "top-right")]
        parts.append(f"[2:v]scale={lc.get('width', 180)}:-1,format=rgba,"
                     f"colorchannelmixer=aa={lc.get('opacity', 0.85)}[lg]")
        parts.append(f"{vf}[v0]")
        parts.append(f"[v0][lg]overlay={pos}[v]")
    else:
        parts.append(f"{vf}[v]")
    if loud:
        parts.append(f"[1:a]loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={loud['input_i']}:"
                     f"measured_TP={loud['input_tp']}:measured_LRA={loud['input_lra']}:"
                     f"measured_thresh={loud['input_thresh']}:offset={loud['target_offset']}:linear=true,"
                     f"aresample={SR}[a]")
    else:
        parts.append("[1:a]anull[a]")
    run_progress(cmd + ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", *enc_args,
                        "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", str(out)],
                 media_duration(body), cwd=work, what="最終書き出し")
