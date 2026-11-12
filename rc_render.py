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
import dataclasses
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
    fx: object = None   # 映像の補正（rc_enhance.VideoFx）。None なら何もしない
    speed: float = 1.0   # 早送りの倍率（frames は早送りしたあとのフレーム数）
    gain_db: float = 0.0
    video: Path | None = None   # 映像だけ別のファイル（2台目カメラ）から使うとき。音声は path のまま
    video_start: float = 0.0    # video のファイル内の開始秒
    fit: str = "pad"            # 縦横比が違うとき: pad=黒で埋める / blur=ぼかした背景で埋める
    broll: object = None   # 重ねる差し込み素材（rc_broll.Overlay）。無ければ None
    focus: tuple | None = None   # ズームの中心（横・縦 0〜1。rc_direct が映像を見て決める）。None なら画面の真ん中


def sub_seg(s: Seg, k0: int, k1: int, fps: Fraction) -> Seg:
    """区切り s の、出力のフレーム k0〜k1 の部分。早送りの倍率と2台目カメラの開始位置も合わせてずらす"""
    dt = float(Fraction(k0) / fps) * float(s.speed)
    return dataclasses.replace(s, start=s.start + dt, frames=k1 - k0,
                               video_start=s.video_start + dt if s.video is not None else s.video_start)


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
        try:
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
        finally:
            # Ctrl+C などで途中で止まったときに、FFmpeg だけが裏で動き続けないようにする
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            tail = "\n".join(err.read().strip().splitlines()[-15:])
            sys.exit(f"[エラー] FFmpeg（{what}）が失敗しました:\n{tail}")


def has_audio(path: Path) -> bool:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return bool(p.stdout.strip())


def media_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
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

def _pre_chain(s: Seg, w: int, h: int) -> str:
    zoom = ""
    if s.zoom > 1.001:
        zoom = f"crop=iw/{s.zoom:.3f}:ih/{s.zoom:.3f}"
        if s.focus:   # 中心を寄せたい物に合わせる（画面の外にはみ出さない所まで）
            fx, fy = (min(max(float(v), 0.0), 1.0) for v in s.focus)
            zoom += (f":x='max(0,min(iw-ow,iw*{fx:.4f}-ow/2))':y='max(0,min(ih-oh,ih*{fy:.4f}-oh/2))'")
        zoom += ","
    # 手ブレ補正（vidstab）は縦横が偶数の画素でないと動かない（縦長の2台目などで奇数になることがある）
    even = ":force_divisible_by=2" if s.fx and getattr(s.fx, "stab", "") else ""
    return f"{zoom}scale={w}:{h}:force_original_aspect_ratio=decrease{even}"


def _tempo(speed: float, gain_db: float = 0.0) -> str:
    """早送りの音（atempo は1回で2倍までにして重ねる）。倍率1なら何もしない"""
    out, k = "", float(speed)
    while k > 2.0 + 1e-9:
        out += "atempo=2,"
        k /= 2.0
    if abs(k - 1.0) > 1e-9:
        out += f"atempo={k:.6g},"
    if gain_db:
        out += f"volume={gain_db}dB,"
    return out


def _blur_fill(src: str, w: int, h: int, i: int) -> str:
    """縦横比が違う映像（縦長のスマホなど）を、同じ映像をぼかして広げた背景の上に置く（最後に , が付く）"""
    bw, bh = max(2, w // 8 // 2 * 2), max(2, h // 8 // 2 * 2)   # 小さくしてからぼかすと速い
    return (f"{src}split=2[mf{i}][mb{i}];"
            f"[mb{i}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=6:2,"
            f"colorchannelmixer=rr=0.75:gg=0.75:bb=0.75,scale={w}:{h},setsar=1[mg{i}];"
            f"[mf{i}]scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1[mc{i}];"
            f"[mg{i}][mc{i}]overlay=(W-w)/2:(H-h)/2,")


def _batch_cmd(segs: list[Seg], w: int, h: int, fps: Fraction, out: Path, enc_args: list[str],
               tag: str | None = None) -> list[str]:
    """tag: 手ブレの解析ファイル（.trf）の名前の頭。省略すると出力ファイルの名前"""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    ins = []   # 各素材の (音声の入力番号, 映像の入力番号)
    for s in segs:
        dur = s.frames / fps * Fraction(s.speed).limit_denominator(1000) + Fraction(1, 10)
        cmd += ["-ss", f"{s.start:.4f}", "-t", f"{float(dur):.4f}", "-i", str(s.path)]
        ai = vi = (ins[-1][1] + 1) if ins else 0
        if s.video is not None:
            cmd += ["-ss", f"{s.video_start:.4f}", "-t", f"{float(dur):.4f}", "-i", str(s.video)]
            vi = ai + 1
        ins.append((ai, vi))
    parts, labels = [], ""
    extra = []   # Bロール（差し込み素材）の入力。本編の入力の後ろに足す
    for i, s in enumerate(segs):
        d = float(s.frames / fps)
        ai, vi = ins[i]
        vl = f"v{i}" if s.broll is None else f"m{i}"
        fx = s.fx.chain(f"{tag or out.stem}_{i:02d}.trf") if s.fx else ""   # 色・手ブレ補正（rc_enhance）
        fast = f"setpts=(PTS-STARTPTS)/{s.speed:g}," if s.speed != 1 else ""
        tail = (f"setsar=1,fps={fps},format=yuv420p,"
                f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={s.frames},setpts=PTS-STARTPTS[{vl}]")
        if s.fit == "blur":
            parts.append(_blur_fill(f"[{vi}:v]{fast}{_pre_chain(s, w, h)},{fx}", w, h, i) + tail)
        else:
            parts.append(f"[{vi}:v]{fast}{_pre_chain(s, w, h)},{fx}"
                         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,{tail}")
        if s.broll is not None:
            import rc_broll
            n_in = ins[-1][1] + 1   # 本編（と2台目）の入力の数。Bロールの入力はその後ろ
            inp, filt = rc_broll.seg_filter(s.broll, n_in + len(extra), f"[{vl}]", f"[v{i}]", fps,
                                            s.frames, f"br{i}")
            extra.append(inp)
            parts.append(filt)
        if s.has_audio:
            # -ss で読み始めた所が映像のキーフレームにちょうど重なると、FFmpeg 9 では最初の音声が少し後ろ
            # （AAC の1かたまり＝最大 21ms）から始まる。asetpts で頭を 0 に詰めると音が早まるので、空きを無音で埋める
            parts.append(f"[{ai}:a]aresample=async=1:first_pts=0,{_tempo(s.speed, s.gain_db)}aresample={SR},"
                         f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"apad=whole_dur={d:.4f},atrim=end={d:.4f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            parts.append(f"anullsrc=r={SR}:cl=stereo,atrim=end={d:.4f}[a{i}]")
        labels += f"[v{i}][a{i}]"
    parts.append(f"{labels}concat=n={len(segs)}:v=1:a=1[v][a]")
    for inp in extra:
        cmd += inp
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
                  *enc_args, "-c:a", "pcm_s16le", str(out)]


def _part_key(b: list[Seg], w: int, h: int, fps: Fraction, tag: str, enc_args: list[str], work: Path):
    """本編パーツの中身を決めるもの: FFmpeg の指示と、読むファイル（本編・2台目・Bロール）。
    色の補正（.cube）は毎回作り直すので、更新日時ではなく中身で比べる"""
    import hashlib
    cmd = _batch_cmd(b, w, h, fps, Path(tag + ".mkv"), enc_args, tag=tag)
    paths = []
    for s in b:
        paths.append(s.path)
        if s.video is not None:
            paths.append(s.video)
        if s.broll is not None and getattr(s.broll, "src", None):
            paths.append(s.broll.src)
        cube = getattr(s.fx, "cube", "") if s.fx else ""
        if cube and (Path(work) / cube).is_file():
            cmd.append(hashlib.sha1((Path(work) / cube).read_bytes()).hexdigest())
    return cmd, paths


def render_body(segs: list[Seg], w: int, h: int, fps: Fraction, work: Path, enc_args: list[str]) -> Path:
    batches = [segs[i:i + BATCH] for i in range(0, len(segs), BATCH)]
    names = []
    import os

    import rc_ops
    fx = None
    if any(s.fx for s in segs):
        import rc_enhance   # 手ブレの解析は、1つ前のまとまりの書き出しと同時に裏で進める
        fx = rc_enhance.StabPipeline(batches, w, h, fps, work, lambda s: _pre_chain(s, w, h))
    for k, b in enumerate(batches, 1):
        tag = f"part_{k:04d}"   # 手ブレの解析ファイル（.trf）の名前にも使う（StabPipeline と同じ名前）
        # 中身の印をファイル名に入れておき、途中で止まったあとの書き出しでは、できているパートを使い回す
        cmd, paths = _part_key(b, w, h, fps, tag, enc_args, work)
        name = f"{tag}_{rc_ops.part_signature(cmd, paths)}.mkv"
        if rc_ops.part_ok(work / name, float(sum(s.frames for s in b) / fps)):
            print(f"    本編の書き出し {k}/{len(batches)}（前回書き出した分を使います）", flush=True)
            if fx:
                fx.skip(k)
        else:
            print(f"    本編の書き出し {k}/{len(batches)}", flush=True)
            if fx:
                fx.wait(k)
            tmp = (work / f"tmp_{name}").resolve()
            # 補正のファイル（.cube / .trf）はフィルターに相対パスで書くので、作業フォルダで実行する
            run(_batch_cmd(b, w, h, fps, tmp, enc_args, tag=tag), cwd=work if fx else None, what=f"本編 {k}")
            os.replace(tmp, work / name)
            rc_ops.mark_part_done(work / name)
        names.append(name)
    rc_ops.remove_unused_parts(work, set(names))
    (work / "parts.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "parts.txt", "-c", "copy", "body.mkv"],
        cwd=work, what="本編の結合")
    return work / "body.mkv"


# ---------- 2. 音声 ----------

def mix_audio(body: Path, total: float, bgm_sections, se_events, tpl: Template, work: Path,
              voice_fx=None) -> dict:
    """bgm_sections: [(開始, 終了, ファイル)] / se_events: [(時刻, ファイル)]。 mix.wav を作り、音量の測定値を返す。

    voice_fx: 声のトラックの処理（rc_enhance.VoiceFx）。None なら何もしない
    """
    a = tpl.get("audio", {})
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(body)]
    parts = [f"[0:a]volume={a.get('voice_volume_db', 0)}dB,asplit=2[voice][key]"]
    if voice_fx:   # 声を聞き取りやすくする処理（BGMを下げる合図にも、処理した後の声を使う）
        parts = voice_fx.graph(f"[0:a]volume={a.get('voice_volume_db', 0)}dB", "[vclean]") + \
            ["[vclean]asplit=2[voice][key]"]
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
    # FFmpeg は作業フォルダ（cwd=work）で動かすので、入出力のパスは絶対パスにしておく
    body, mix, out = Path(body).resolve(), Path(mix).resolve(), Path(out).resolve()
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
