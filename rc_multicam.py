"""2台カメラの自動切り替え（正面カメラ ⇔ 手元カメラ）。

荒編集で --cam2 を付けて同期した2台目カメラ（スマホなど）を、場面ごとに使い分けます。

- 喋っている時は正面（本編）カメラ
- 長い無言の作業や、「ここを」「この部品」などの手元の説明は、手元（2台目）カメラ
- オプションで、Claude に両カメラの静止画と発言を見せて決めてもらうこともできます

決めた区間は演出プランの "angles" に、元素材（本編）の時刻で保存します:
    "angles": [{"start": 29.6, "end": 40.1, "cam": "cam2", "why": "無言の作業"}]
元素材の時刻なので、あとでカットを変えても同じ場面を指します。
1つのカメラを映す最短の長さ（min_shot 秒）は、カット後のタイムラインで守ります。
書き出し（rc_render）では、この区間の映像だけを2台目に差し替えます（音声は本編のまま）。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from rc_media import SourceSet, probe
from rc_timeline import Timeline
from rc_transcribe import fmt_ts

DEFAULTS = {
    "auto": True,          # 演出プランを作る時に、手元カメラの場面を自動で決める
    "ai": False,           # Claude に静止画と発言を見せて決める（「Claude APIで自動」の時だけ）
    "min_shot": 3.0,       # 1つのカメラを続けて映す最短の秒数
    "silence": 4.0,        # この秒数以上だれも喋っていない所は手元カメラにする
    "words": ["ここを", "ここに", "ここが", "ここの", "この部品", "このパーツ", "この部分", "このネジ", "こちらを",
              "手元", "こうやって", "こうして", "こんな感じ", "このように", "この向き"],
    "background": "blur",  # 縦横比が違う時: blur=ぼかした背景で埋める / black=黒で埋める
    "min_confidence": 6.0,  # 同期の信頼度がこれより低い時は、自動では切り替えない
}
EPS = 1e-3
PLAN_FILES = ("plan_edited.json", "plan_used.json")   # 手直しを優先（finish・rc_thumb と同じ順）


def settings(tpl=None) -> dict:
    cfg = dict(DEFAULTS)
    if tpl is not None:
        cfg.update(tpl.get("multicam", {}) or {})
    cfg["min_shot"] = max(0.0, float(cfg["min_shot"]))
    cfg["silence"] = max(0.5, float(cfg["silence"]))
    return cfg


# ============================================================
#  2台目カメラの読み込み
# ============================================================
@dataclass
class Cam2:
    src: SourceSet
    offset: float          # 2台目の時刻 = 本編の時刻 + offset
    confidence: float = 99.0

    @property
    def cover(self) -> tuple[float, float]:
        """2台目に映像がある、本編の時刻の範囲"""
        return max(0.0, -self.offset), self.src.duration - self.offset

    def locate(self, t_main: float) -> tuple[Path, float] | None:
        """本編の時刻 → (2台目のファイル, ファイル内の秒)"""
        t = t_main + self.offset
        for f, off in zip(self.src.files, self.src.offsets):
            if off - EPS <= t < off + f.duration:
                return f.path, max(0.0, t - off)
        return None


def load_cam2(out_dir: Path, sess: dict) -> Cam2 | None:
    paths = (sess.get("args") or {}).get("cam2") or []
    sync = Path(out_dir) / "sync.json"
    if not paths or not sync.exists():
        return None
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print(f"  [注意] 2台目カメラのファイルが見つからないので、カメラの切り替えはしません: {missing[0]}")
        return None
    s = json.loads(sync.read_text(encoding="utf-8"))
    return Cam2(SourceSet([probe(Path(p)) for p in paths]), float(s.get("offset_sec", 0.0)),
                float(s.get("confidence", 99.0)))


def load_plan(out_dir: Path) -> tuple[dict | None, Path | None]:
    """今の演出プラン（手直しがあればそちら）"""
    for name in PLAN_FILES:
        p = Path(out_dir) / name
        if p.exists() and (name != "plan_edited.json" or (Path(out_dir) / "plan_used.json").exists()):
            try:
                return json.loads(p.read_text(encoding="utf-8-sig")), p
            except (OSError, ValueError):
                continue
    return None, None


# ============================================================
#  区間の計算（元素材の時刻 ⇔ カット後の時刻）
# ============================================================
def _merge(iv, gap: float = EPS):
    out = []
    for s, e in sorted((s, e) for s, e in iv if e - s > EPS):
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _intersect(a, b):
    out = []
    a, b = _merge(a), _merge(b)
    for s, e in a:
        for bs, be in b:
            lo, hi = max(s, bs), min(e, be)
            if hi - lo > EPS:
                out.append((lo, hi))
    return _merge(out)


def _pieces(tl: Timeline):
    fps = float(tl.fps)
    for p in tl.pieces:
        ps, pe = p.seq_start / fps, p.seq_end / fps
        yield p, ps, pe


def to_edit(tl: Timeline, ranges) -> list[tuple[float, float]]:
    """元素材の時刻の区間 → カット後のタイムラインの区間"""
    out = []
    ranges = _merge(ranges)
    for p, ps, pe in _pieces(tl):
        for s, e in ranges:
            if e <= p.g_in or s >= p.g_out:
                continue
            a = ps + max(s, p.g_in) - p.g_in
            b = min(pe, ps + min(e, p.g_out) - p.g_in)
            if b - a > EPS:
                out.append((a, b))
    return _merge(out)


def to_source(tl: Timeline, ivs) -> list[tuple[float, float]]:
    """カット後のタイムラインの区間 → 元素材の時刻の区間（カットの所で分かれる）"""
    out = []
    for p, ps, pe in _pieces(tl):
        for s, e in ivs:
            a, b = max(s, ps), min(e, pe)
            if b - a > EPS:
                out.append((p.g_in + a - ps, min(p.g_out, p.g_in + b - ps)))
    return sorted(out)


def join_over_cuts(tl: Timeline, ranges) -> list[tuple[float, float]]:
    """カットで分かれているだけの区間（カット後には続いている）を1つにまとめる"""
    ed = to_edit(tl, ranges)
    out = []
    for s, e in ed:
        src = to_source(tl, [(s, e)])
        if src:
            out.append((src[0][0], src[-1][1]))
    return out


def _norm(segs):
    """同じカメラが続く所をつなげ、長さ0のショットを除く"""
    out = []
    for s, e, c in segs:
        if e - s <= EPS:
            continue
        if out and out[-1][2] == c:
            out[-1][1] = e
        else:
            out.append([s, e, c])
    return out


def _extend_short(segs, min_shot: float, covered) -> bool:
    """短い手元ショットを、2台目の映像がある範囲で後ろ→前に延ばす。

    となりの正面ショットは、min_shot 秒以上残るか、まるごと使い切る場合だけ削る（短い正面を作らない）。
    """
    changed = False
    for i, sg in enumerate(segs):
        if not sg[2] or sg[1] - sg[0] >= min_shot - EPS:
            continue
        cov = next(((cs, ce) for cs, ce in covered if cs - EPS <= sg[0] and sg[1] <= ce + EPS), None)
        if not cov:
            continue
        need = min_shot - (sg[1] - sg[0])
        for j, fwd in ((i + 1, True), (i - 1, False)):
            if need <= EPS or not (0 <= j < len(segs)) or segs[j][2]:
                continue
            nb = segs[j]
            room = nb[1] - nb[0]
            cov_room = (cov[1] - sg[1]) if fwd else (sg[0] - cov[0])
            if room - need < min_shot - EPS and room <= cov_room + EPS:
                add = room   # 残りの正面が短くなるなら、まるごと手元にする
            else:
                add = max(0.0, min(need, cov_room, room - min_shot))
            if add <= EPS:
                continue
            if fwd:
                sg[1] += add
                nb[0] = sg[1]
            else:
                sg[0] -= add
                nb[1] = sg[0]
            need -= add
            changed = True
    return changed


def _fix_one(segs, min_shot: float, covered) -> bool:
    """一番短いショットを1つだけ直す（直す所がなければ False）"""
    if len(segs) <= 1:
        return False
    short = [(sg[1] - sg[0], i) for i, sg in enumerate(segs) if sg[1] - sg[0] < min_shot - EPS]
    if not short:
        return False
    _, i = min(short)
    sg = segs[i]
    if sg[2]:
        sg[2] = False          # 延ばせなかった短い手元ショット → 正面に戻す
    elif any(cs - EPS <= sg[0] and sg[1] <= ce + EPS for cs, ce in covered):
        sg[2] = True           # 手元にはさまれた短い正面 → 手元にまとめる
    else:
        # 2台目の映像がない所の短い正面（撮り始め・撮り終わりのあたり）→ となりの手元ショットを削る
        nb = [j for j in (i - 1, i + 1) if 0 <= j < len(segs) and segs[j][2]]
        j = max(nb, key=lambda k: segs[k][1] - segs[k][0])
        need = min_shot - (sg[1] - sg[0])
        if j > i:
            segs[j][0] = min(segs[j][1], segs[j][0] + need)
            sg[1] = segs[j][0]
        else:
            segs[j][1] = max(segs[j][0], segs[j][1] - need)
            sg[0] = segs[j][1]
    return True


def enforce_min_shot(labels: list[list], min_shot: float, covered) -> list[list]:
    """[(開始, 終了, 手元か)] の並びで、min_shot 秒より短いショットをなくす。

    1. 短い手元ショットは、2台目の映像がある範囲で前後に延ばす
    2. それでも短いショットは、一番短いものから順に直す
       （短い手元 → 正面に戻す / 手元にはさまれた短い正面 → 手元にまとめる）
    """
    segs = _norm(labels)
    for _ in range(10000):
        changed = _extend_short(segs, min_shot, covered)
        segs = _norm(segs)
        fixed = _fix_one(segs, min_shot, covered)
        segs = _norm(segs)
        if not changed and not fixed:
            break
    return segs




def compute_shots(tl: Timeline, angles: list[dict], cam2: Cam2, min_shot: float) -> list[tuple[float, float]]:
    """演出プランの angles から、実際に手元カメラを映す区間（元素材の時刻）を求める"""
    want = [(float(a["start"]), float(a["end"])) for a in angles
            if a.get("cam", "cam2") == "cam2" and float(a.get("end", 0)) > float(a.get("start", 0))]
    if not want or not tl.pieces:
        return []
    cover = [cam2.cover]
    ed_cover = to_edit(tl, cover)
    ed_want = _intersect(to_edit(tl, want), ed_cover)
    if not ed_want:
        return []
    total = tl.duration
    labels, t = [], 0.0
    for s, e in ed_want:
        labels.append([t, s, False])
        labels.append([s, e, True])
        t = e
    labels.append([t, total, False])
    segs = enforce_min_shot(labels, min_shot, ed_cover)
    ed = _intersect([(s, e) for s, e, c in segs if c], ed_cover)
    return to_source(tl, ed)


# ============================================================
#  自動で決める（AIなし）
# ============================================================
def _kept_chunks(chunks, sess: dict):
    cut = set(sess.get("cut_ids", []))
    corr = {int(k): v for k, v in (sess.get("corrections") or {}).items()}
    return [(c, corr.get(c.id, c.text)) for c in chunks if c.id not in cut]


def rule_angles(tl: Timeline, chunks, sess: dict, cam2: Cam2, cfg: dict) -> list[dict]:
    """無言の作業と、手元を指す言葉のある発言を、手元カメラにする"""
    kept = _kept_chunks(chunks, sess)
    raw = []   # (開始, 終了, 理由) 元素材の時刻
    # 長い無言（カット後のタイムラインで測る）
    speech = to_edit(tl, [(c.start, c.end) for c, _ in kept])
    t = 0.0
    for s, e in speech + [(tl.duration, tl.duration)]:
        if s - t >= cfg["silence"]:
            for a, b in to_source(tl, [(t, s)]):
                raw.append((a, b, "無言の作業"))
        t = max(t, e)
    # 手元を指す言葉
    words = [w for w in cfg.get("words", []) if w]
    for c, text in kept:
        hit = next((w for w in words if w in text), None)
        if hit:
            raw.append((c.start, c.end, f"「{hit}」と手元を説明"))
    return finalize(tl, raw, cam2, cfg)


def finalize(tl: Timeline, raw, cam2: Cam2, cfg: dict) -> list[dict]:
    """(開始, 終了, 理由) の候補に最短ショット長を当てはめて、プランの angles にする"""
    shots = compute_shots(tl, [{"start": s, "end": e} for s, e, _ in raw], cam2, cfg["min_shot"])
    out = []
    for s, e in join_over_cuts(tl, shots):
        whys = []
        for rs, re_, why in sorted(raw):
            if rs < e and re_ > s and why not in whys:
                whys.append(why)
        out.append({"start": round(s, 3), "end": round(e, 3), "cam": "cam2", "why": "・".join(whys)[:60] or "手元"})
    return out


def sanitize_angles(angles) -> list[dict]:
    out = []
    for a in angles or []:
        try:
            s, e = float(a["start"]), float(a["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e - s > 0.05:
            out.append({"start": round(s, 3), "end": round(e, 3), "cam": "cam2" if a.get("cam", "cam2") != "main"
                        else "main", "why": str(a.get("why", ""))[:80]})
    return sorted(out, key=lambda a: a["start"])


# ============================================================
#  Claude に静止画と発言を見せて決める
# ============================================================
ANGLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scenes"],
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["no", "cam", "why"],
                "properties": {"no": {"type": "integer"},
                               "cam": {"type": "string", "enum": ["main", "cam2"]},
                               "why": {"type": "string"}},
            },
        },
    },
}
PER_CALL = 45   # 1回に送る場面の数（画像は場面ごとに2枚。1回100枚までなので余裕を持たせる）


def _instructions(mode: str, min_shot: float) -> str:
    import rc_llm
    return f"""あなたはYouTube動画の編集者です。2台のカメラで撮った動画の、場面ごとにどちらのカメラの映像を使うかを決めてください。
動画のジャンル: {rc_llm.MODE_HINT.get(mode, '')}

## カメラ
- main: 正面のカメラ（話している人が映る）
- cam2: 手元のカメラ（作業している手元・商品・部品のアップ）

## 決め方
- 人が喋って説明している時は、基本は main（顔が見える方が伝わる）
- 長い無言の作業、手元の細かい作業、「ここを」「この部品」など手元を見てほしい説明の時は cam2
- 画像を見て、cam2 に手元が映っていない（ブレている・何も映っていない・暗い）場面は main
- 切り替えすぎない。1つのカメラを{min_shot:.0f}秒以上は続ける（短い場面は前後と同じカメラにする）

## 入力
場面ごとに「場面番号・時刻・発言」と、main の静止画・cam2 の静止画を1枚ずつ見せます。
すべての場面について、no（場面番号）・cam・why（短い理由）を返してください。出力はJSONのみです。"""


def _scenes(tl: Timeline, kept, cam2: Cam2, cfg: dict, target: float):
    """カット後のタイムラインを、場面（数秒〜十数秒のかたまり）に分ける。

    戻り値: [(カット後の開始, 終了, 発言の文)]（2台目の映像がある所だけ）
    """
    speech = []
    for c, text in kept:
        for s, e in to_edit(tl, [(c.start, c.end)]):
            speech.append((s, e, text))
    speech.sort()
    units, t = [], 0.0
    for s, e, text in speech + [(tl.duration, tl.duration, "")]:
        if s - t >= 1.5:
            units.append([t, s, "", s - t >= cfg["silence"]])
        elif units and s > t:
            units[-1][1] = s
        if e > s:
            units.append([max(s, t), e, text, False])
        t = max(t, e)
    scenes = []
    for u in units:
        long_silence = u[3]
        if scenes and not long_silence and not scenes[-1][3] and scenes[-1][1] - scenes[-1][0] < target:
            scenes[-1][1] = u[1]
            if u[2]:
                scenes[-1][2] = (scenes[-1][2] + " " + u[2]).strip()
        else:
            scenes.append(list(u))
    cover = to_edit(tl, [cam2.cover])
    out = []
    for s, e, text, _ in scenes:
        for a, b in _intersect([(s, e)], cover):
            if b - a >= 0.5:
                out.append((a, b, text))
    return out


def grab_jpeg(path: Path, t: float, size: int = 320) -> bytes | None:
    """動画の t 秒の静止画を、長い辺が size ピクセルの JPEG で取り出す（ファイルは作らない）"""
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                            "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 and p.stdout else None


def _main_locate(tl: Timeline, g: float) -> tuple[Path, float]:
    src = tl.sources
    for f, off in zip(src.files, src.offsets):
        if off - EPS <= g < off + f.duration:
            return f.path, max(0.0, g - off)
    return src.files[-1].path, max(0.0, g - src.offsets[-1])


def ai_angles(tl: Timeline, chunks, sess: dict, cam2: Cam2, cfg: dict, effort: str, usage: dict) -> list[dict]:
    import rc_llm
    anthropic, client = rc_llm._client()
    kept = _kept_chunks(chunks, sess)
    target = max(6.0, cfg["min_shot"] * 2, tl.duration / 300)   # 長い素材でも場面が多くなりすぎないように
    scenes = _scenes(tl, kept, cam2, cfg, target)
    if not scenes:
        return []
    system = _instructions(sess.get("mode", "craft"), cfg["min_shot"])
    raw = []
    batches = [scenes[i:i + PER_CALL] for i in range(0, len(scenes), PER_CALL)]
    no = 0
    for k, batch in enumerate(batches, 1):
        print(f"  Claudeでカメラを選んでいます… ({k}/{len(batches)})", flush=True)
        content, nos = [], {}
        for s, e, text in batch:
            no += 1
            g = to_source(tl, [((s + e) / 2, (s + e) / 2 + 0.05)])
            if not g:
                continue
            gm = g[0][0]
            mp, ml = _main_locate(tl, gm)
            loc2 = cam2.locate(gm)
            im1 = grab_jpeg(mp, ml)
            im2 = grab_jpeg(*loc2) if loc2 else None
            if not im1 or not im2:
                continue
            nos[no] = (s, e)
            content.append({"type": "text", "text": f"場面{no} [{fmt_ts(s)}〜{fmt_ts(e)}]（{e - s:.0f}秒）"
                            f" 発言: {('「' + text[:200] + '」') if text else '（無言）'}"})
            for label, data in (("main", im1), ("cam2", im2)):
                content.append({"type": "text", "text": f"場面{no}の{label}"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": base64.b64encode(data).decode()}})
        if not nos:
            continue
        res = rc_llm._call(client, anthropic, system, content, ANGLE_SCHEMA, effort, usage)
        for sc in res.get("scenes", []):
            if sc.get("no") in nos and sc.get("cam") == "cam2":
                s, e = nos[sc["no"]]
                for a, b in to_source(tl, [(s, e)]):
                    raw.append((a, b, ("AI: " + str(sc.get("why", ""))).strip()[:60]))
    return finalize(tl, raw, cam2, cfg)


# ============================================================
#  仕上げ（finish.py）から使う
# ============================================================
@dataclass
class Multicam:
    cam2: Cam2
    shots: list[tuple[float, float]]   # 手元カメラを映す区間（元素材の時刻）
    background: str = "blur"


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def decide(tl: Timeline, chunks, sess: dict, cam2: Cam2, cfg: dict, use_ai: bool, effort: str,
           usage: dict) -> list[dict]:
    if use_ai:
        if not has_api_key():
            print("  [お知らせ] APIキーが設定されていないので、カメラは自動（AIなし）で選びます。")
        else:
            try:
                return ai_angles(tl, chunks, sess, cam2, cfg, effort, usage)
            except (Exception, SystemExit) as e:  # noqa: BLE001  AIが失敗しても書き出しは止めない
                print(f"  [注意] AIでカメラを選べなかったので、自動（AIなし）で選びます: {e}")
    return rule_angles(tl, chunks, sess, cam2, cfg)


def prepare(out_dir: Path, sess: dict, tl: Timeline, chunks, plan: dict, tpl, llm: str, effort: str,
            usage: dict) -> Multicam | None:
    """演出プランの angles を用意して（無ければ自動で決めて）、書き出しで使う区間を返す。

    2台目カメラがなければ何もしない（None）。plan は書き換える。
    """
    cam2 = load_cam2(out_dir, sess)
    if cam2 is None:
        return None
    cfg = settings(tpl)
    if "angles" in plan:
        plan["angles"] = sanitize_angles(plan["angles"])
    elif not cfg["auto"]:
        plan["angles"] = []
    elif cam2.confidence < float(cfg["min_confidence"]):
        print(f"  [注意] 2台目カメラの同期の信頼度が低い（{cam2.confidence:.1f}）ので、自動ではカメラを切り替えません。"
              "画面の「カメラ」の表で手動で選べます。")
        plan["angles"] = []
    else:
        plan["angles"] = decide(tl, chunks, sess, cam2, cfg, bool(cfg["ai"]) and llm == "api", effort, usage)
    shots = compute_shots(tl, plan["angles"], cam2, cfg["min_shot"])
    try:
        write_xml(out_dir, sess, plan["angles"], cam2, cfg["min_shot"])
    except Exception as e:  # noqa: BLE001  XMLが作れなくても動画の書き出しは続ける
        print(f"  [注意] Premiere用XMLを更新できませんでした: {e}")
    sec = sum(to_edit_len(tl, shots))
    print(f"  手元カメラ: {len(join_over_cuts(tl, shots))}場面（合計 {sec:.0f}秒）")
    return Multicam(cam2, shots, str(cfg.get("background", "blur")))


def to_edit_len(tl: Timeline, ranges) -> list[float]:
    return [e - s for s, e in to_edit(tl, ranges)]


def apply_segs(segs, src: SourceSet, fps: Fraction, mc: Multicam, w: int, h: int):
    """本編の素材リスト（rc_render.Seg）を手元カメラの区間で分け、その部分の映像を2台目に差し替える"""
    import dataclasses
    if not mc or not mc.shots:
        return segs
    idx = {f.path: i for i, f in enumerate(src.files)}
    c2 = mc.cam2
    bounds = sorted({b for s, e in mc.shots for b in (s, e)}
                    | {off - c2.offset for off in c2.src.offsets[1:]})
    out = []
    for sg in segs:
        fi = idx.get(sg.path)
        if fi is None or sg.video is not None:
            out.append(sg)   # オープニング・エンディングなど
            continue
        g0 = src.offsets[fi] + sg.start
        g1 = g0 + sg.frames / float(fps)
        cuts = {0, sg.frames}
        for b in bounds:
            if g0 < b < g1:
                cuts.add(round((b - g0) * float(fps)))
        cuts = sorted(c for c in cuts if 0 <= c <= sg.frames)
        for k0, k1 in zip(cuts, cuts[1:]):
            if k1 <= k0:
                continue
            a = g0 + k0 / float(fps)
            mid = g0 + (k0 + k1) / 2 / float(fps)
            loc = c2.locate(a + 0.5 / float(fps))
            use = loc is not None and any(s <= mid < e for s, e in mc.shots)
            if not use:
                out.append(dataclasses.replace(sg, start=sg.start + k0 / float(fps), frames=k1 - k0))
                continue
            f2 = next(f for f in c2.src.files if f.path == loc[0])
            same_shape = abs(f2.width / f2.height - w / h) < 0.01
            fit = "blur" if mc.background == "blur" and not same_shape else "pad"
            out.append(dataclasses.replace(sg, start=sg.start + k0 / float(fps), frames=k1 - k0, zoom=1.0,
                                           video=loc[0], video_start=max(0.0, loc[1] - 0.5 / float(fps)), fit=fit))
    return out


# ============================================================
#  Premiere用XML（選んだ区間だけ V2 を有効にする）
# ============================================================
def write_xml(out_dir: Path, sess: dict, angles: list[dict], cam2: Cam2, min_shot: float) -> Path:
    from rc_export import write_xmeml
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    shots = compute_shots(tl, angles, cam2, min_shot)
    markers = [(m["t"], m["name"], m.get("comment", "")) for m in sess.get("markers", [])]
    stem = sess["stem"]
    path = Path(out_dir) / f"{stem}_荒編集.xml"
    write_xmeml(path, f"{stem}_荒編集", tl, markers, cam2.src, cam2.offset, cam2_shots=shots)
    return path


def xml_shots(out_dir: Path, tl: Timeline, cam2_src: SourceSet, offset: float,
              template: Path | None = None) -> list[tuple[float, float]] | None:
    """荒編集（roughcut.py）の書き直しで使う：前に決めたカメラの区間があれば、今のカットに当てはめる"""
    plan, _ = load_plan(out_dir)
    if not plan or "angles" not in plan:
        return None
    cfg = DEFAULTS
    if template:
        try:
            from rc_template import Template
            cfg = settings(Template(Path(template)))
        except (Exception, SystemExit):  # noqa: BLE001
            cfg = settings()
    return compute_shots(tl, sanitize_angles(plan["angles"]), Cam2(cam2_src, offset), float(cfg["min_shot"]))


def save_angles(out_dir: Path, angles: list[dict]) -> list[Path]:
    """演出プラン（plan_used.json と、あれば plan_edited.json）の angles を書き換える"""
    saved = []
    for name in PLAN_FILES:
        p = Path(out_dir) / name
        if not p.exists():
            continue
        plan = json.loads(p.read_text(encoding="utf-8-sig"))
        plan["angles"] = angles
        if isinstance(plan.get("_off"), dict):
            plan["_off"]["angles"] = []
        p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        saved.append(p)
    return saved
