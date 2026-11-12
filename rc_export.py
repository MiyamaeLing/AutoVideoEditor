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


def _v2_parts(p, shots):
    """1クリップの区間を、手元カメラを使う所（True）と使わない所（False）に分ける。shots が None なら分けない"""
    if shots is None:
        return [(p.g_in, p.g_out, True)]
    cuts = sorted({p.g_in, p.g_out} | {b for s, e in shots for b in (s, e) if p.g_in < b < p.g_out})
    return [(a, b, any(s <= (a + b) / 2 < e for s, e in shots)) for a, b in zip(cuts, cuts[1:]) if b > a]


def _fit_motion(f: MediaInfo, w: int, h: int) -> str:
    """縦横比が違う2台目の映像を、画面に収まる大きさにする（Premiere の「モーション → スケール」）"""
    if (f.width, f.height) == (w, h) or not f.width or not f.height:
        return ""
    scale = min(w / f.width, h / f.height) * 100
    return ("<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion</effectcategory>"
            "<effecttype>motion</effecttype><mediatype>video</mediatype>"
            '<parameter authoringApp="PremierePro"><parameterid>scale</parameterid><name>Scale</name>'
            f"<valuemin>0</valuemin><valuemax>1000</valuemax><value>{scale:.2f}</value></parameter></effect></filter>")


def write_xmeml(path: Path, name: str, tl: Timeline, markers: list[tuple[float, str, str]],
                cam2: SourceSet | None = None, cam2_offset: float = 0.0,
                cam2_shots: list[tuple[float, float]] | None = None) -> None:
    """cam2_shots（元素材の時刻）を渡すと、その区間だけ V2（2台目カメラ）のクリップを有効にする"""
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

    # 2台目カメラ：同じカットで V2 に並べる（初期状態は非表示。cam2_shots があれば、その区間だけ有効）
    v2_items = []
    if cam2 is not None:
        c2_ids = [f"file-cam2-{i+1}" for i in range(len(cam2.files))]
        parts = [(p, g_a, g_b, on) for p in tl.pieces for g_a, g_b, on in _v2_parts(p, cam2_shots)]
        for p, g_a, g_b, on in parts:
            s2, e2 = g_a + cam2_offset, g_b + cam2_offset
            seq_base = p.seq_start
            part_end = p.seq_end if g_b >= p.g_out else seq_base + round((g_b - p.g_in) * seq_fps)
            for fi, ls, le in cam2.split(max(s2, 0), max(e2, 0)):
                f2 = cam2.files[fi]
                g_start = cam2.offsets[fi] + ls - cam2_offset  # main基準の全体時刻
                st = seq_base + round((g_start - p.g_in) * seq_fps)
                fin, fout = round(ls * f2.fps), min(round(le * f2.fps), f2.frames)
                en = st + round((fout - fin) * seq_fps / f2.fps)
                en = min(en, part_end)
                if en <= st or fout <= fin:
                    continue
                cid = ids.next("clipitem-cam2")
                motion = _fit_motion(f2, main.width, main.height) if cam2_shots is not None else ""
                v2_items.append(
                    f'<clipitem id="{cid}"><name>{escape(f2.path.name)}</name>'
                    f"<enabled>{'TRUE' if on else 'FALSE'}</enabled>"
                    f"<duration>{f2.frames}</duration>{_rate(f2.fps)}<start>{st}</start><end>{en}</end>"
                    f"<in>{fin}</in><out>{fin + round((en - st) * f2.fps / seq_fps)}</out>"
                    f"{_file_xml(c2_ids[fi], f2, defined)}{motion}</clipitem>"
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
        v2_on = "TRUE" if cam2_shots is not None else "FALSE"   # 区間を選んだときは、選んだクリップだけ映る
        v_tracks += f"<track>{''.join(v2_items)}<enabled>{v2_on}</enabled><locked>FALSE</locked></track>"
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


_PUNCT = set("、。，．！？!?…‥）)」』】〉》 　")
_PARSER = None


def _phrases(text: str) -> list[str]:
    """日本語を文節（単語のまとまり）に分ける。BudouX が無ければ、文字の種類の変わり目で分ける"""
    global _PARSER
    if _PARSER is None:
        try:
            import budoux
            _PARSER = budoux.load_default_japanese_parser()
        except Exception:  # noqa: BLE001  入っていなくても字幕は作れるようにする
            _PARSER = False
    if _PARSER:
        try:
            return [p for p in _PARSER.parse(text) if p]
        except Exception:  # noqa: BLE001
            pass
    return [m.group(0) for m in re.finditer(
        r"[一-龯々〆ヵヶ]+[ぁ-ゖー]*|[ァ-ヺー]+[ぁ-ゖ]*|[A-Za-z0-9０-９Ａ-Ｚａ-ｚ.,'&+\-]+[ぁ-ゖ]*|[ぁ-ゖー]+|.", text)]


def _char_kind(c: str) -> str:
    if "一" <= c <= "龯" or c in "々〆ヵヶ":
        return "k"
    if "ァ" <= c <= "ヺ":
        return "K"
    if "ぁ" <= c <= "ゖ":
        return "h"
    if c.isascii() and c.isalnum():
        return "a"
    return "o"


def split_caption(text: str, max_chars: int) -> list[str]:
    """字幕を max_chars 以内の枚数に分ける。単語の途中では切らず、句読点・文節の切れ目を選び、
    なるべく同じくらいの長さにそろえる（どうしても入らない長い単語だけは、文字の種類の変わり目か途中で切る）"""
    text = text.strip().rstrip("。")
    if len(text) <= max_chars:
        return [text] if text else []
    n = len(text)
    # 切れ目の良さ（小さいほど良い）。位置 i は text[:i] と text[i:] の間
    pen = [100.0] * (n + 1)
    pos = 0
    for ph in _phrases(text):
        pos += len(ph)
        if 0 < pos < n:
            pen[pos] = 1.0
    for i in range(1, n):
        a, b = text[i - 1], text[i]
        if a in _PUNCT:
            pen[i] = 0.0
        elif pen[i] > 4 and _char_kind(a) != _char_kind(b) and "o" not in (_char_kind(a) + _char_kind(b)):
            pen[i] = 4.0   # 文節の分け方では切れ目でないが、文字の種類が変わる所
        if b in _PUNCT and b not in " 　":
            pen[i] = max(pen[i], 50.0)   # 句読点を次の字幕の頭に回さない
        elif a.isdigit() and (_char_kind(b) in "Ka" or b in _COUNTERS):
            pen[i] = max(pen[i], 60.0)   # 数字と単位（10｜ミリ）の間では切らない
    pen[n] = 0.0
    parts_min = -(-n // max_chars)
    target = n / parts_min
    inf = float("inf")
    best = [inf] * (n + 1)
    back = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for i in range(max(0, j - max_chars), j):
            if best[i] == inf:
                continue
            seg = j - i
            c = best[i] + pen[j] + 3.0 + 0.08 * (seg - target) ** 2
            if seg < 4 and j < n:
                c += 6.0   # 短すぎる字幕は避ける
            if c < best[j]:
                best[j], back[j] = c, i
    cuts, j = [], n
    while j > 0:
        cuts.append(j)
        j = back[j]
    cuts.reverse()
    parts, prev = [], 0
    for c in cuts:
        parts.append(text[prev:c])
        prev = c
    return [p.strip().rstrip("。") for p in parts if p.strip()]


_SENTENCE_END = re.compile(r"(ます|です|ました|でした|ません|ますね|ですね|ましょう|でしょう|よね|ますが|ですが|けど|ので|たら|から)$")
_COUNTERS = "点円個本枚回年日分秒倍位人台歳度種"
MEND_GAP = 2.0   # これより間が空いていれば、別々の発言として扱う（言葉をつなぎ直さない）


def _straddle(a: str, b: str) -> tuple[int, int]:
    """a の終わりと b の頭が1つの言葉になっているとき、(a 側の字数, b 側の字数) を返す。切れ目として自然なら (0, 0)"""
    if not a or not b or a[-1] in _PUNCT or b[0] in _PUNCT or _SENTENCE_END.search(a):
        return 0, 0   # 句読点・文の終わり（〜ます／〜です など）のあとは、そこが切れ目
    joined, n = a + b, len(a)
    lo = hi = None
    pos = 0
    for ph in _phrases(joined):
        if pos < n < pos + len(ph):
            lo, hi = pos, pos + len(ph)
            if min(n - lo, hi - n) >= 3:   # 両側とも長い（「…ブロック」「総合点は…」）→ 別々の言葉。つながない
                return 0, 0
            break
        pos += len(ph)
    ka, kb = _char_kind(a[-1]), _char_kind(b[0])
    if lo is None and ka == kb == "K":   # カタカナ語の途中（デザ｜イン）は、文節の判定に関係なくつなぐ
        lo = hi = n
        while lo > 0 and _char_kind(joined[lo - 1]) == "K":
            lo -= 1
        while hi < len(joined) and (_char_kind(joined[hi]) == "K" or joined[hi] == "ー"):
            hi += 1
    if lo is None and ka == "k" and (n < 2 or _char_kind(a[-2]) != "k"):
        # 前が漢字1字で終わっている（「それと木」「材の…」/「…とか早」「く書きたい」）→ その1字は次の言葉の頭
        return 1, len(b)
    if lo is None and a[-1].isdigit() and (kb == "K" or b[0] in _COUNTERS):   # 「重さが30」「グラム」/「総合点は8」「点」
        k = n
        while k > 0 and (a[k - 1].isdigit() or a[k - 1] in ".,"):
            k -= 1
        return n - k, len(b)
    if lo is None:
        return 0, 0
    return n - lo, hi - n


def mend_items(items: list[tuple[float, float, str]], max_gap: float = MEND_GAP) -> list[tuple[float, float, str]]:
    """文字起こしが言葉の途中で区切った所（「…にある工」「房となっています」）を、言葉がつながるように直す。

    区切りをまたいでいる言葉の、短い方のかけらを隣へ移す。数は変えない（文字が全部移った発言は、文が空になる）。
    もう一度かけても変わらない。
    """
    out = [(s, e, str(t).strip()) for s, e, t in items]
    for i in range(len(out) - 1):
        (s1, e1, a), (s2, e2, b) = out[i], out[i + 1]
        if not a or not b or s2 - e1 > max_gap or a == b:
            continue
        left, right = _straddle(a, b)
        if not left and not right:
            continue
        if right < left and right < len(b):   # 次の頭のかけらを、前へ（「黒柿」「とかチューリップ」→「黒柿とか」）
            a, b = a + b[:right], b[right:]
        elif left >= len(a):                  # 前が丸ごと言葉のかけら（「シ」「タンと…」）→ 次へまとめて、前は空にする
            a, b, s2 = "", a + b, s1
        else:                                 # 前の終わりのかけらを、次へ（「…にある工」「房と…」→「工房と…」）
            a, b = a[:-left], a[-left:] + b
        out[i], out[i + 1] = (s1, e1, a.strip()), (s2, e2, b.strip())
    return out


def build_captions(items: list[tuple[float, float, str]], max_chars: int) -> list[tuple[float, float, str]]:
    """(開始, 終了, 文) の一覧を、読みやすい長さに分割した字幕にする。"""
    out = []
    for s, e, text in mend_items(items):
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
