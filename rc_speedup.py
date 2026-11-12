"""工作の作業シーンの自動早送り（タイムラプス）。

- 荒編集（工作モード）で残した「無言の作業区間」を、長さに応じて 2〜8倍の早送りにする。
  とても長い作業はタイムラプス（さらに速く）にする
- 演出プラン（plan_used.json）の "speedups" に、元素材の時刻で保存する。AI のプランでも調整・追加できる
- 書き出しでは、映像を速くし、音は消して（BGMだけにして）、画面の隅に「×4」などの印を出す
- 字幕・テロップ・章カード・BGM・チャプター・サムネイル候補は、早送り後の時刻に直してから使う
- Premiere 用に、速度を入れた XML（<名前>_早送り入り.xml）も作る

plan の "speedups" の1件:
  {"start": 元素材の開始秒, "end": 終了秒, "speed": 倍率, "style": "fast" | "timelapse",
   "work": 作業の番号（1から。AIが新しく足したものは 0）}
"""
from __future__ import annotations

import bisect
import copy
import json
from dataclasses import dataclass
from pathlib import Path

FAST_STEPS = [2, 3, 4, 6, 8]          # 早送りの倍率の候補（きりのいい数にする）
LAPSE_STEPS = [10, 12, 15, 20, 30, 45, 60]

DEFAULTS = {
    "enabled": True,          # 工作モードの無言の作業を、自動で早送りにする
    "min_seconds": 6.0,       # これより短い作業は早送りにしない
    "target_seconds": 10.0,   # 早送りしたあとの長さの目安（長い作業ほど速くなる）
    "min_speed": 2,
    "max_speed": 8,
    "timelapse_over": 240.0,  # これより長い作業で、8倍でも長すぎるときはタイムラプスにする
    "timelapse_max": 30,
    "audio": "mute",          # mute=作業の音を消してBGMだけ / fast=速くした音を小さく流す
    "audio_volume_db": -18,
    "badge": True,            # 画面の隅に「×4」などを出す
    "badge_text": "▶▶ ×{speed}",
    "timelapse_text": "タイムラプス ×{speed}",
    "position": "bottom-right",
    "font": "BIZ UDPGothic",
    "size": 54,
    "bold": True,
    "color": "#FFFFFF",
    "box_color": "#1A1A1A",
    "margin": 48,
}
ALIGN = {"bottom-right": 3, "bottom-left": 1, "top-right": 9, "top-left": 7}


def config(tpl) -> dict:
    """テンプレートの [speedup]（なければ初期値）"""
    c = dict(DEFAULTS)
    if tpl is not None:
        c.update(tpl.get("speedup", {}) or {})
    return c


def fmt_speed(v: float) -> str:
    return f"{v:g}" if abs(v - round(v)) > 1e-6 else str(int(round(v)))


# ---------------------------------------------------------------- 作業区間と倍率

def work_ranges(sess: dict, chunks=None, total: float | None = None) -> list[tuple[float, float]]:
    """session.json の無言の作業区間（元素材の秒）。古い session.json なら、工作モードのときだけ計算し直す。"""
    if "silent_work" in sess:
        return [(float(s), float(e)) for s, e in sess["silent_work"]]
    if sess.get("mode") != "craft" or not chunks or not total:
        return []
    from rc_timeline import PRESETS, compute_keep
    from rc_transcribe import Chunk
    cs = [c if isinstance(c, Chunk) else Chunk(**c) for c in chunks]
    return [(float(s), float(e)) for s, e in compute_keep(cs, set(sess.get("cut_ids", [])), total, PRESETS["craft"])[1]]


def _nearest(steps, v):
    return min(steps, key=lambda s: (abs(s - v), s))


def auto_speed(dur: float, cfg: dict) -> tuple[float, str] | None:
    """作業の長さから倍率を決める。短すぎて早送りにしないときは None"""
    if dur < float(cfg["min_seconds"]):
        return None
    lo, hi = float(cfg["min_speed"]), float(cfg["max_speed"])
    raw = dur / max(float(cfg["target_seconds"]), 1.0)
    if dur >= float(cfg["timelapse_over"]) and raw > hi:
        lapse = [s for s in LAPSE_STEPS if hi < s <= float(cfg["timelapse_max"])] or [float(cfg["timelapse_max"])]
        return float(_nearest(lapse, raw)), "timelapse"
    steps = [s for s in FAST_STEPS if lo <= s <= hi] or [lo]
    return float(_nearest(steps, min(max(raw, lo), hi))), "fast"


def auto_speedups(works: list[tuple[float, float]], cfg: dict) -> list[dict]:
    out = []
    if not cfg.get("enabled", True):
        return out
    for n, (s, e) in enumerate(works, 1):
        a = auto_speed(e - s, cfg)
        if a:
            out.append({"start": round(s, 3), "end": round(e, 3), "speed": a[0], "style": a[1], "work": n})
    return out


def _clamp(item: dict, cfg: dict) -> dict | None:
    style = item.get("style", "fast")
    if style == "none":
        return None
    try:
        sp = float(item.get("speed", 0))
    except (TypeError, ValueError):
        sp = 0.0
    s, e = float(item["start"]), float(item["end"])
    if sp <= 1.0 or e - s < 1.0:
        return None
    if style == "timelapse":
        sp = min(max(sp, float(cfg["max_speed"])), max(float(cfg["timelapse_max"]), float(cfg["max_speed"])))
    else:
        style = "fast"
        sp = min(max(sp, float(cfg["min_speed"])), float(cfg["max_speed"]))
    return {"start": round(s, 3), "end": round(e, 3), "speed": sp, "style": style, "work": int(item.get("work", 0))}


def _no_overlap(items: list[dict]) -> list[dict]:
    out = []
    for it in sorted(items, key=lambda x: x["start"]):
        if out and it["start"] < out[-1]["end"]:
            continue
        out.append(it)
    return out


def sanitize(plan: dict, works: list[tuple[float, float]], by_id: dict, cfg: dict) -> list[dict]:
    """演出プランの "speedups" を、元素材の時刻の一覧にそろえる。

    - "speedups" がない（古いプラン・AIなし）: 作業区間から自動で作る
    - "speedups_auto": false が付いている（書き出しで保存したプラン・画面で手直ししたプラン）: その通りにする
    - それ以外（AIの返答）: 書いてある作業だけ変えて、書いていない作業は自動の倍率にする。
      work=0 は AI が新しく足した早送り（start_id〜end_id の発言の範囲）
    """
    raw = plan.get("speedups")
    if raw is None:
        return _no_overlap(auto_speedups(works, cfg))
    fixed = plan.get("speedups_auto") is False
    chosen: dict[int, dict | None] = {}
    extra = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        n = int(it.get("work") or 0)
        if "start" in it and "end" in it:
            item = {**it, "work": n}
        elif 1 <= n <= len(works):
            item = {**it, "start": works[n - 1][0], "end": works[n - 1][1]}
        elif it.get("start_id") in by_id and it.get("end_id") in by_id:
            a, b = by_id[it["start_id"]], by_id[it["end_id"]]
            item = {**it, "start": min(_st(a), _st(b)), "end": max(_en(a), _en(b)), "work": 0}
        else:
            continue
        c = _clamp(item, cfg)
        if 1 <= n <= len(works) and not fixed:
            chosen[n] = c       # None = この作業は早送りにしない
        elif c:
            extra.append(c)
    if not fixed:
        for a in auto_speedups(works, cfg):
            if a["work"] not in chosen:
                chosen[a["work"]] = a
    return _no_overlap([c for c in chosen.values() if c] + extra)


def _st(c):
    return c["start"] if isinstance(c, dict) else c.start


def _en(c):
    return c["end"] if isinstance(c, dict) else c.end


# ---------------------------------------------------------------- 早送り後のタイムライン

@dataclass
class _Part:
    g0: float        # 元素材の全体時刻（秒）
    g1: float
    o0: int          # 完成（本編）でのフレーム
    of: int          # 完成でのフレーム数
    speed: float
    sid: int | None  # どの早送りか（speedups の番号）
    file_index: int


class SpeedTimeline:
    """Timeline に早送りを加えたもの。Timeline と同じ map_time / map_range / map_time_nearest で、
    元素材の時刻 → 完成動画（本編）の時刻 に直す。書き出しの素材リスト（segs）も同じ計算から作るので、
    字幕などの時刻と映像が必ずそろう。"""

    def __init__(self, tl, speedups: list[dict], zooms=(), audio: str = "mute", audio_db: float = -18.0):
        self.tl = tl
        self.sources = tl.sources
        self.fps = tl.fps
        self.speedups = list(speedups)
        fps = tl.fps
        sp_ranges = [(float(x["start"]), float(x["end"]), float(x["speed"])) for x in self.speedups]
        self.parts: list[_Part] = []
        self.segs = []
        import rc_render
        cur = 0
        acc: dict[int, list[int]] = {}   # 早送りごとの（元のフレーム, 完成のフレーム）。端数をためて合計をそろえる
        for p in tl.pieces:
            f = tl.sources.files[p.file_index]
            n = p.seq_end - p.seq_start
            local0 = p.g_in - tl.sources.offsets[p.file_index]
            cuts = {0, n}
            for s, e, *_ in list(zooms) + sp_ranges:
                for b in (s, e):
                    if p.g_in < b < p.g_out:
                        cuts.add(round((b - p.g_in) * fps))
            cuts = sorted(c for c in cuts if 0 <= c <= n)
            for k0, k1 in zip(cuts, cuts[1:]):
                if k1 <= k0:
                    continue
                mid = p.g_in + float((k0 + k1) / 2 / fps)
                hit = next((zz for zz in zooms if zz[0] <= mid < zz[1]), None)   # (開始, 終了, 倍率[, 中心])
                z, focus = (hit[2] if hit else 1.0), (hit[3] if hit and len(hit) > 3 else None)
                sid = next((i for i, (s, e, _) in enumerate(sp_ranges) if s <= mid < e), None)
                speed = sp_ranges[sid][2] if sid is not None else 1.0
                if sid is None:
                    of = k1 - k0
                else:
                    a = acc.setdefault(sid, [0, 0])
                    of = round((a[0] + k1 - k0) / speed) - a[1]
                    a[0] += k1 - k0
                    a[1] += of
                g0 = p.g_in + float(k0 / fps)
                g1 = p.g_in + float(k1 / fps)
                self.parts.append(_Part(g0, g1, cur, of, speed, sid, p.file_index))
                if of <= 0:
                    continue   # 早送りでフレームが残らないほど短い所（時刻の変換にだけ使う）
                if sid is None:
                    self.segs.append(rc_render.Seg(f.path, local0 + float(k0 / fps), of, z, focus=focus))
                else:
                    keep_audio = audio == "fast"
                    self.segs.append(rc_render.Seg(f.path, local0 + float(k0 / fps), of, z, has_audio=keep_audio,
                                                   speed=speed, gain_db=float(audio_db) if keep_audio else 0.0,
                                                   focus=focus))
                cur += of
        self.total_frames = cur
        self._g0 = [pt.g0 for pt in self.parts]

    # ---- Timeline と同じ使い方 ----
    @property
    def duration(self) -> float:
        return self.total_frames / float(self.fps)

    def _o(self, pt: _Part, t: float) -> float:
        fps = float(self.fps)
        return pt.o0 / fps + max(0.0, min((t - pt.g0) / pt.speed, pt.of / fps))

    def _find(self, t: float) -> _Part | None:
        i = bisect.bisect_right(self._g0, t) - 1
        if 0 <= i < len(self.parts) and self.parts[i].g0 <= t < self.parts[i].g1:
            return self.parts[i]
        return None

    def map_time(self, t: float) -> float | None:
        pt = self._find(t)
        return None if pt is None else self._o(pt, t)

    def map_time_nearest(self, t: float) -> float:
        m = self.map_time(t)
        if m is not None:
            return m
        i = bisect.bisect_left(self._g0, t)
        if i < len(self.parts):
            return self.parts[i].o0 / float(self.fps)
        return self.duration

    def _overlapping(self, s: float, e: float):
        i = max(0, bisect.bisect_right(self._g0, s) - 1)
        while i < len(self.parts) and self.parts[i].g0 < e:
            pt = self.parts[i]
            if pt.g1 > s:
                yield pt
            i += 1

    def map_range(self, s: float, e: float) -> list[tuple[float, float]]:
        out = []
        for pt in self._overlapping(s, e):
            a, b = max(s, pt.g0), min(e, pt.g1)
            if b - a > 0.05:
                r = (self._o(pt, a), self._o(pt, b))
                if out and abs(out[-1][1] - r[0]) < 1e-6:
                    out[-1] = (out[-1][0], r[1])   # ズームなどで分けただけの続きはつなげる
                else:
                    out.append(r)
        return out

    @property
    def pieces(self):
        """サムネイル候補など、Timeline.pieces を使う処理向け（seq_* は完成でのフレーム）"""
        from rc_timeline import Piece
        return [Piece(pt.file_index, 0, 0, pt.o0, pt.o0 + pt.of, pt.g0, pt.g1) for pt in self.parts]

    # ---- 早送りならでは ----
    def fast_share(self, s: float, e: float) -> float:
        """元素材の [s, e] のうち、早送りになる割合"""
        if e <= s:
            return 0.0
        fast = sum(min(e, pt.g1) - max(s, pt.g0) for pt in self._overlapping(s, e) if pt.sid is not None)
        return fast / (e - s)

    def badge_ranges(self) -> list[tuple[float, float, float, str]]:
        """完成（本編）での早送りの区間 [(開始, 終了, 倍率, 種類)]"""
        fps = float(self.fps)
        out = []
        last = None
        for pt in self.parts:
            if pt.sid is None:
                last = None
                continue
            s, e = pt.o0 / fps, (pt.o0 + pt.of) / fps
            if last == pt.sid and out:
                out[-1] = (out[-1][0], e, out[-1][2], out[-1][3])
            else:
                out.append((s, e, pt.speed, self.speedups[pt.sid].get("style", "fast")))
            last = pt.sid
        return out


def retime_items(chunks, cut: set, corr: dict, stl: SpeedTimeline, old_span: dict):
    """発言を完成の時刻に直す。戻り値: (字幕にする発言 [(id, 開始, 終了, 文字)], 全発言の {id: (開始, 終了)})

    早送り（音を消す）の中にある発言は字幕を出さない。テロップ・BGM の位置には使うので span には残す。
    """
    items, span = [], {}
    by_id = {c.id: c for c in chunks}
    for c in chunks:
        if c.id in cut:
            continue
        r = stl.map_range(c.start, c.end)
        if r:
            span[c.id] = (r[0][0], r[-1][1])
            if stl.fast_share(c.start, c.end) < 0.5:
                items.append((c.id, r[0][0], r[-1][1], corr.get(c.id, c.text)))
    for cid in old_span:
        if cid not in span:   # 早送りでとても短くなった発言も、テロップの位置は求められるように
            t = stl.map_time_nearest(by_id[cid].start)
            span[cid] = (t, t + 0.1)
    return items, span


def wrap(tl, plan: dict | None):
    """プランに早送りがあれば、早送り後のタイムラインにする（サムネイル候補を完成の時刻で均等に選ぶため）"""
    sp = (plan or {}).get("speedups") or []
    try:
        return SpeedTimeline(tl, sp) if sp else tl
    except Exception:  # noqa: BLE001  プランが壊れていても、サムネイルは作れるように
        return tl


# ---------------------------------------------------------------- 画面の隅の「×4」

def add_badges(ass: str, tpl, w: int, h: int, ranges, offset: float = 0.0, until: float | None = None) -> str:
    """build_ass() の結果に、早送り中の「×4」などの表示を足す。ranges は本編の時刻"""
    cfg = config(tpl)
    if not cfg.get("badge", True) or not ranges:
        return ass
    from rc_ass import _esc, _style, _t
    style = _style("Speedup", cfg, ALIGN.get(cfg.get("position", "bottom-right"), 3), int(cfg.get("margin", 48)),
                   int(cfg.get("margin", 48)), box=bool(cfg.get("box_color")))
    ev = []
    for s, e, speed, kind in ranges:
        if until is not None:
            if s >= until - 0.05:
                continue
            e = min(e, until)
        if e - s < 0.3:
            continue
        text = cfg["timelapse_text"] if kind == "timelapse" else cfg["badge_text"]
        text = text.replace("{speed}", fmt_speed(speed))
        ev.append(f"Dialogue: 4,{_t(s + offset)},{_t(e + offset)},Speedup,,0,0,0,,"
                  r"{\fad(150,150)\fscx80\fscy80\t(0,150,\fscx100\fscy100)}" + _esc(text))
    if not ev:
        return ass
    k = ass.find("\n\n[Events]")
    if k >= 0:
        ass = ass[:k] + "\n" + style + ass[k:]
    return ass.rstrip("\n") + "\n" + "\n".join(ev) + "\n"


# ---------------------------------------------------------------- AI の演出プラン

SPEEDUP_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["work", "start_id", "end_id", "speed", "style"],
        "properties": {
            "work": {"type": "integer"},
            "start_id": {"type": "integer"},
            "end_id": {"type": "integer"},
            "speed": {"type": "integer"},
            "style": {"type": "string", "enum": ["fast", "timelapse", "none"]},
        },
    },
}


def plan_schema() -> dict:
    import rc_plan
    s = copy.deepcopy(rc_plan.PLAN_SCHEMA)
    s["properties"]["speedups"] = SPEEDUP_SCHEMA
    s["required"] = list(s["required"]) + ["speedups"]
    return s


def _work_lines(works, tl, cfg) -> list[tuple[float, str]]:
    from rc_transcribe import fmt_ts
    out = []
    for n, (s, e) in enumerate(works, 1):
        a = auto_speed(e - s, cfg)
        auto = (f"自動なら {'タイムラプス' if a[1] == 'timelapse' else '早送り'} ×{fmt_speed(a[0])}" if a
                else "短いので自動では早送りにしない")
        out.append((tl.map_time_nearest(s), f"【作業W{n} [{fmt_ts(tl.map_time_nearest(s), ms=True)}] "
                                            f"無言の作業 {e - s:.0f}秒（{auto}）】"))
    return out


def instructions(mode: str, moods: list[str], cfg: dict) -> str:
    import rc_plan
    return rc_plan._instructions(mode, moods).replace("\n出力はJSONのみです。", "") + f"""
- speedups: 作業シーンの早送りの調整。文字起こしの途中にある `【作業W番号 …】` の行は、喋っていない作業の場面です
  - 何も書かなかった作業は、長さに応じて自動で早送りになります（{cfg['min_speed']}〜{cfg['max_speed']}倍）。変えたい作業だけ書いてください
  - work: 作業の番号（W の後の数字）。そのときは start_id と end_id は 0
  - 作業以外の場面（独り言だけの長い作業など）を新しく早送りにするときは、work を 0 にして start_id〜end_id の発言を指定します。その間の声は消え、字幕も出ません。説明している所には使わない
  - speed: 倍率。fast は {cfg['min_speed']}〜{cfg['max_speed']}（大事な手元の作業は 2〜3、単純なくり返しは 6〜8）、timelapse は 10〜{cfg['timelapse_max']}（乾かす・待つなど、とても長い時間）
  - style: fast=早送り / timelapse=タイムラプス / none=早送りにしない（作業の音を聞かせたい所など）

出力はJSONのみです。"""


def plan_text(items, works, tl, cfg) -> str:
    from rc_transcribe import fmt_ts
    rows = [(s, f"#{i} [{fmt_ts(s, ms=True)}] {t}") for i, s, e, t in items] + _work_lines(works, tl, cfg)
    rows.sort(key=lambda r: r[0])
    return "\n".join(r[1] for r in rows)


def make_plan_api(items, mode: str, moods: list[str], effort: str, works, tl, cfg) -> tuple[dict, dict]:
    import rc_llm
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    print("  Claudeで演出プランを作成中…（作業シーンの早送りも含む）", flush=True)
    plan = rc_llm._call(client, anthropic, instructions(mode, moods, cfg), plan_text(items, works, tl, cfg),
                        plan_schema(), effort, usage)
    return plan, usage


def write_manual_prompt(items, mode: str, moods: list[str], path: Path, works, tl, cfg) -> None:
    schema = json.dumps(plan_schema(), ensure_ascii=False, indent=1)
    path.write_text(instructions(mode, moods, cfg) + f"\n\n## 出力形式（JSON Schema）\n```json\n{schema}\n```\n\n"
                    "## 文字起こし\n" + plan_text(items, works, tl, cfg), encoding="utf-8")


# ---------------------------------------------------------------- Premiere 用 XML（速度入り）

def _split_pieces(tl, speedups):
    """Timeline のクリップを早送りの境目で分け、[(file_index, 素材in, 素材out, seq開始, seq終了, 倍率, g_in, g_out)]"""
    fps = tl.fps
    sp = [(float(x["start"]), float(x["end"]), float(x["speed"])) for x in speedups]
    out, cur = [], 0
    acc: dict[int, list] = {}
    for p in tl.pieces:
        f = tl.sources.files[p.file_index]
        cuts = {p.src_in, p.src_out}
        for s, e, _ in sp:
            for b in (s, e):
                if p.g_in < b < p.g_out:
                    cuts.add(p.src_in + round((b - p.g_in) * f.fps))
        cuts = sorted(c for c in cuts if p.src_in <= c <= p.src_out)
        for a, b in zip(cuts, cuts[1:]):
            if b <= a:
                continue
            g0 = tl.sources.offsets[p.file_index] + a / f.fps
            g1 = tl.sources.offsets[p.file_index] + b / f.fps
            mid = (g0 + g1) / 2
            sid = next((i for i, (s, e, _) in enumerate(sp) if s <= mid < e), None)
            k = sp[sid][2] if sid is not None else 1.0
            raw = (b - a) * fps / f.fps
            if sid is None:
                n = round(raw)
            else:
                acc_s = acc.setdefault(sid, [0.0, 0])
                n = round((acc_s[0] + raw) / k) - acc_s[1]
                acc_s[0] += raw
                acc_s[1] += n
            if n <= 0:
                continue
            out.append((p.file_index, a, b, cur, cur + n, k, g0, g1))
            cur += n
    return out, cur


def _timeremap(speed: float, media: str) -> str:
    return ("<filter><effect><name>Time Remap</name><effectid>timeremap</effectid>"
            "<effectcategory>motion</effectcategory><effecttype>motion</effecttype>"
            f"<mediatype>{media}</mediatype>"
            "<parameter authoringApp=\"PremierePro\"><parameterid>variablespeed</parameterid><name>variablespeed</name>"
            "<valuemin>0</valuemin><valuemax>1</valuemax><value>0</value></parameter>"
            "<parameter authoringApp=\"PremierePro\"><parameterid>speed</parameterid><name>speed</name>"
            f"<valuemin>-100000</valuemin><valuemax>100000</valuemax><value>{speed * 100:g}</value></parameter>"
            "<parameter authoringApp=\"PremierePro\"><parameterid>reverse</parameterid><name>reverse</name>"
            "<value>FALSE</value></parameter>"
            "<parameter authoringApp=\"PremierePro\"><parameterid>frameblending</parameterid><name>frameblending</name>"
            "<value>FALSE</value></parameter></effect></filter>")


def write_xmeml(path: Path, name: str, tl, speedups: list[dict], markers, cam2=None, cam2_offset: float = 0.0) -> None:
    """速度を入れた FCP7 XML。早送りのクリップには Time Remap（速度）を付け、音は無効にしておく。
    念のため、早送りの区間には「早送り ×4」のマーカーも付ける（速度が読み込まれなかったときの目印）。"""
    from xml.sax.saxutils import escape

    from rc_export import _file_xml, _rate
    src, seq_fps, main = tl.sources, tl.fps, tl.sources.main
    defined: set[str] = set()
    file_ids = [f"file-main-{i+1}" for i in range(len(src.files))]
    nch = min(max(main.audio_channels, 1), 2)
    pieces, total = _split_pieces(tl, speedups)
    v_items, a_items = [], [[] for _ in range(nch)]
    for idx, (fi, a, b, st, en, k, _g0, _g1) in enumerate(pieces):
        f = src.files[fi]
        vid = f"clipitem-v{idx+1}"
        aids = [f"clipitem-a{ch+1}-{idx+1}" for ch in range(nch)]
        links = (f"<link><linkclipref>{vid}</linkclipref><mediatype>video</mediatype><trackindex>1</trackindex>"
                 f"<clipindex>{idx+1}</clipindex></link>")
        for ch, aid in enumerate(aids):
            links += (f"<link><linkclipref>{aid}</linkclipref><mediatype>audio</mediatype><trackindex>{ch+1}</trackindex>"
                      f"<clipindex>{idx+1}</clipindex><groupindex>1</groupindex></link>")
        if k == 1.0:
            ci, co, dur, fv, fa, en_a = a, b, f.frames, "", "", "TRUE"
        else:
            # 速度を変えたクリップの in/out/duration は、速度を変えたあとのフレームで書く（Premiere の書き出しと同じ）
            ci = round(a / k)
            co = ci + (en - st)
            dur = round(f.frames / k)
            fv, fa, en_a = _timeremap(k, "video"), _timeremap(k, "audio"), "FALSE"
        common = (f"<name>{escape(f.path.name)}</name><enabled>@EN@</enabled><duration>{dur}</duration>"
                  f"{_rate(f.fps)}<start>{st}</start><end>{en}</end><in>{ci}</in><out>{co}</out>")
        v_items.append(f'<clipitem id="{vid}">{common.replace("@EN@", "TRUE")}'
                       f"{_file_xml(file_ids[fi], f, defined)}{fv}{links}</clipitem>")
        for ch, aid in enumerate(aids):
            a_items[ch].append(
                f'<clipitem id="{aid}">{common.replace("@EN@", en_a)}{_file_xml(file_ids[fi], f, defined)}{fa}'
                f"<sourcetrack><mediatype>audio</mediatype><trackindex>{ch+1}</trackindex></sourcetrack>{links}</clipitem>")

    v2 = []
    if cam2 is not None:
        c2_ids = [f"file-cam2-{i+1}" for i in range(len(cam2.files))]
        n = 0
        for (_fi, _a, _b, st0, en0, k, g0, g1) in pieces:
            for fi2, ls, le in cam2.split(max(g0 + cam2_offset, 0), max(g1 + cam2_offset, 0)):
                f2 = cam2.files[fi2]
                gs = cam2.offsets[fi2] + ls - cam2_offset
                st = st0 + round((gs - g0) * seq_fps / k)
                fin, fout = round(ls * f2.fps), min(round(le * f2.fps), f2.frames)
                en = min(st + round((fout - fin) * seq_fps / f2.fps / k), en0)
                if en <= st or fout <= fin:
                    continue
                n += 1
                if k == 1.0:
                    ci, co, dur, fv = fin, fin + round((en - st) * f2.fps / seq_fps), f2.frames, ""
                else:
                    ci = round(fin / k)
                    co = ci + round((en - st) * f2.fps / seq_fps)
                    dur, fv = round(f2.frames / k), _timeremap(k, "video")
                v2.append(f'<clipitem id="clipitem-cam2-{n}"><name>{escape(f2.path.name)}</name><enabled>TRUE</enabled>'
                          f"<duration>{dur}</duration>{_rate(f2.fps)}<start>{st}</start><end>{en}</end>"
                          f"<in>{ci}</in><out>{co}</out>{_file_xml(c2_ids[fi2], f2, defined)}{fv}</clipitem>")

    stl = SpeedTimeline(tl, speedups)
    mk_list = [(stl.map_time_nearest(t_src), n_, c_) for t_src, n_, c_ in markers]
    for s, e, k, kind in stl.badge_ranges():
        label = "タイムラプス" if kind == "timelapse" else "早送り"
        mk_list.append((s, f"{label} ×{fmt_speed(k)}", f"完成での長さ {e - s:.1f}秒"))
    mk_list.sort()
    mk = "".join(f"<marker><name>{escape(n_)}</name><comment>{escape(c_)}</comment>"
                 f"<in>{round(t * seq_fps)}</in><out>-1</out></marker>" for t, n_, c_ in mk_list)
    fmt = (f"<format><samplecharacteristics>{_rate(seq_fps)}<width>{main.width}</width><height>{main.height}</height>"
           f"<pixelaspectratio>square</pixelaspectratio><anamorphic>FALSE</anamorphic>"
           f"<fielddominance>none</fielddominance></samplecharacteristics></format>")
    v_tracks = f"<track>{''.join(v_items)}<enabled>TRUE</enabled><locked>FALSE</locked></track>"
    if v2:
        v_tracks += f"<track>{''.join(v2)}<enabled>FALSE</enabled><locked>FALSE</locked></track>"
    a_tracks = "".join(f"<track>{''.join(items)}<enabled>TRUE</enabled><locked>FALSE</locked>"
                       f"<outputchannelindex>{ch+1}</outputchannelindex></track>" for ch, items in enumerate(a_items))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
           f'<sequence id="sequence-1"><name>{escape(name)}</name><duration>{total}</duration>{_rate(seq_fps)}'
           f"<timecode>{_rate(seq_fps)}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat>"
           f"</timecode><media><video>{fmt}{v_tracks}</video>"
           f"<audio><numOutputChannels>{nch}</numOutputChannels><format><samplecharacteristics><depth>16</depth>"
           f"<samplerate>{main.sample_rate}</samplerate></samplecharacteristics></format>{a_tracks}</audio></media>"
           f"{mk}</sequence></xmeml>\n")
    path.write_text(xml, encoding="utf-8")


def write_session_xml(out_dir: Path, speedups: list[dict]) -> Path | None:
    """荒編集の結果（session.json）と早送りの一覧から、<名前>_早送り入り.xml を作る"""
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    out_dir = Path(out_dir)
    sess = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    if not speedups:
        return None
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    cam2, off = None, 0.0
    cam2_paths = (sess.get("args") or {}).get("cam2") or []
    if cam2_paths and all(Path(p).exists() for p in cam2_paths):
        cam2 = SourceSet([probe(Path(p)) for p in cam2_paths])
        try:
            off = float(json.loads((out_dir / "sync.json").read_text(encoding="utf-8"))["offset_sec"])
        except Exception:  # noqa: BLE001
            off = 0.0
    # session の markers は荒編集のタイムラインの時刻なので、元素材の時刻に戻してから付け直す
    markers = []
    for m in sess.get("markers", []):
        t_src = _to_source(tl, m["t"])
        if t_src is not None:
            markers.append((t_src, m["name"], m.get("comment", "")))
    path = out_dir / f"{sess['stem']}_早送り入り.xml"
    write_xmeml(path, f"{sess['stem']}_早送り入り", tl, speedups, markers, cam2, off)
    return path


def _to_source(tl, t: float) -> float | None:
    fps = float(tl.fps)
    for p in tl.pieces:
        s, e = p.seq_start / fps, p.seq_end / fps
        if s <= t < e or (t >= e and p is tl.pieces[-1]):
            return min(p.g_in + (t - s), p.g_out - 1e-3)
    return None


def plan_for_save(speedups: list[dict]) -> dict:
    """plan_used.json に書く形（"speedups_auto": false で、次からはこの通りに使う）"""
    return {"speedups": speedups, "speedups_auto": False}
