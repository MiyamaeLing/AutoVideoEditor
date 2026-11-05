"""Premiere Pro で読み込める FCP7 XML（xmeml）と SRT 字幕の書き出し。"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from rc_media import MediaInfo, SourceSet
from rc_timeline import Timeline


def _rate(fps: Fraction) -> str:
    ntsc = fps.denominator == 1001
    tb = round(float(fps))
    return f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"


def _pathurl(p: Path) -> str:
    s = str(p).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", s):
        return "file://localhost/" + quote(s, safe="/:")
    return "file://localhost" + quote(s, safe="/:")


class _Ids:
    def __init__(self):
        self.n = 0

    def next(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"


def _file_xml(fid: str, f: MediaInfo, defined: set) -> str:
    if fid in defined:
        return f'<file id="{fid}"/>'
    defined.add(fid)
    return (
        f'<file id="{fid}"><name>{escape(f.path.name)}</name><pathurl>{_pathurl(f.path)}</pathurl>'
        f"{_rate(f.fps)}<duration>{f.frames}</duration>"
        f"<media><video><samplecharacteristics>{_rate(f.fps)}<width>{f.width}</width><height>{f.height}</height>"
        f"</samplecharacteristics></video>"
        f"<audio><samplecharacteristics><depth>16</depth><samplerate>{f.sample_rate}</samplerate></samplecharacteristics>"
        f"<channelcount>{f.audio_channels}</channelcount></audio></media></file>"
    )


def write_xmeml(path: Path, name: str, tl: Timeline, markers: list[tuple[float, str, str]],
                cam2: SourceSet | None = None, cam2_offset: float = 0.0) -> None:
    src = tl.sources
    seq_fps = tl.fps
    main = src.main
    ids = _Ids()
    defined: set[str] = set()
    file_ids = [f"file-main-{i+1}" for i in range(len(src.files))]
    nch = min(max(main.audio_channels, 1), 2)

    v_items, a_items = [], [[] for _ in range(nch)]
    for idx, p in enumerate(tl.pieces):
        f = src.files[p.file_index]
        vid = f"clipitem-v{idx+1}"
        aids = [f"clipitem-a{ch+1}-{idx+1}" for ch in range(nch)]
        links = f'<link><linkclipref>{vid}</linkclipref><mediatype>video</mediatype><trackindex>1</trackindex><clipindex>{idx+1}</clipindex></link>'
        for ch, aid in enumerate(aids):
            links += (f'<link><linkclipref>{aid}</linkclipref><mediatype>audio</mediatype>'
                      f'<trackindex>{ch+1}</trackindex><clipindex>{idx+1}</clipindex><groupindex>1</groupindex></link>')
        common = (f"<name>{escape(f.path.name)}</name><enabled>TRUE</enabled><duration>{f.frames}</duration>"
                  f"{_rate(f.fps)}<start>{p.seq_start}</start><end>{p.seq_end}</end><in>{p.src_in}</in><out>{p.src_out}</out>")
        v_items.append(f'<clipitem id="{vid}">{common}{_file_xml(file_ids[p.file_index], f, defined)}{links}</clipitem>')
        for ch, aid in enumerate(aids):
            a_items[ch].append(
                f'<clipitem id="{aid}">{common}{_file_xml(file_ids[p.file_index], f, defined)}'
                f"<sourcetrack><mediatype>audio</mediatype><trackindex>{ch+1}</trackindex></sourcetrack>{links}</clipitem>"
            )

    # 2台目カメラ：同じカットで V2 に並べる（初期状態は非表示）
    v2_items = []
    if cam2 is not None:
        c2_ids = [f"file-cam2-{i+1}" for i in range(len(cam2.files))]
        for idx, p in enumerate(tl.pieces):
            s2, e2 = p.g_in + cam2_offset, p.g_out + cam2_offset
            seq_base = p.seq_start
            for fi, ls, le in cam2.split(max(s2, 0), max(e2, 0)):
                f2 = cam2.files[fi]
                g_start = cam2.offsets[fi] + ls - cam2_offset  # main基準の全体時刻
                st = seq_base + round((g_start - p.g_in) * seq_fps)
                fin, fout = round(ls * f2.fps), min(round(le * f2.fps), f2.frames)
                en = st + round((fout - fin) * seq_fps / f2.fps)
                en = min(en, p.seq_end)
                if en <= st or fout <= fin:
                    continue
                cid = ids.next("clipitem-cam2")
                v2_items.append(
                    f'<clipitem id="{cid}"><name>{escape(f2.path.name)}</name><enabled>TRUE</enabled>'
                    f"<duration>{f2.frames}</duration>{_rate(f2.fps)}<start>{st}</start><end>{en}</end>"
                    f"<in>{fin}</in><out>{fin + round((en - st) * f2.fps / seq_fps)}</out>"
                    f"{_file_xml(c2_ids[fi], f2, defined)}</clipitem>"
                )

    mk = "".join(
        f"<marker><name>{escape(n)}</name><comment>{escape(c)}</comment>"
        f"<in>{round(t * seq_fps)}</in><out>-1</out></marker>"
        for t, n, c in markers
    )
    fmt = (f"<format><samplecharacteristics>{_rate(seq_fps)}<width>{main.width}</width><height>{main.height}</height>"
           f"<pixelaspectratio>square</pixelaspectratio><anamorphic>FALSE</anamorphic>"
           f"<fielddominance>none</fielddominance></samplecharacteristics></format>")
    v_tracks = f"<track>{''.join(v_items)}<enabled>TRUE</enabled><locked>FALSE</locked></track>"
    if v2_items:
        v_tracks += f"<track>{''.join(v2_items)}<enabled>FALSE</enabled><locked>FALSE</locked></track>"
    a_tracks = "".join(
        f"<track>{''.join(items)}<enabled>TRUE</enabled><locked>FALSE</locked>"
        f"<outputchannelindex>{ch+1}</outputchannelindex></track>"
        for ch, items in enumerate(a_items)
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
        f'<sequence id="sequence-1"><name>{escape(name)}</name><duration>{tl.total_frames}</duration>{_rate(seq_fps)}'
        f"<timecode>{_rate(seq_fps)}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>"
        f"<media><video>{fmt}{v_tracks}</video>"
        f"<audio><numOutputChannels>{nch}</numOutputChannels><format><samplecharacteristics><depth>16</depth>"
        f"<samplerate>{main.sample_rate}</samplerate></samplecharacteristics></format>{a_tracks}</audio></media>"
        f"{mk}</sequence></xmeml>\n"
    )
    path.write_text(xml, encoding="utf-8")


# ---------- 字幕 ----------

def _srt_ts(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_caption(text: str, max_chars: int) -> list[str]:
    text = text.strip().rstrip("。")
    if len(text) <= max_chars:
        return [text]
    parts, cur = [], ""
    for tok in re.split(r"(?<=[、。！？!?\s])", text):
        if cur and len(cur) + len(tok) > max_chars:
            parts.append(cur)
            cur = ""
        while len(tok) > max_chars:
            parts.append(tok[:max_chars])
            tok = tok[max_chars:]
        cur += tok
    if cur:
        parts.append(cur)
    return [p.strip().rstrip("。") for p in parts if p.strip()]


def build_captions(items: list[tuple[float, float, str]], max_chars: int) -> list[tuple[float, float, str]]:
    """(開始, 終了, 文) の一覧を、読みやすい長さに分割した字幕にする。"""
    out = []
    for s, e, text in items:
        parts = split_caption(text, max_chars)
        total = sum(len(p) for p in parts) or 1
        t = s
        for p in parts:
            d = (e - s) * len(p) / total
            out.append((t, t + d, p))
            t += d
    # 短すぎる表示時間を少し延ばす（次の字幕にかぶらない範囲で）
    fixed = []
    for i, (s, e, p) in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else e + 10
        e = min(max(e, s + 0.7), nxt)
        fixed.append((s, e, p))
    return fixed


def write_srt(path: Path, caps: list[tuple[float, float, str]]) -> None:
    lines = []
    for i, (s, e, t) in enumerate(caps, 1):
        lines += [str(i), f"{_srt_ts(s)} --> {_srt_ts(e)}", t, ""]
    path.write_text("\n".join(lines), encoding="utf-8-sig")
