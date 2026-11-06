"""Bロール（差し込み素材）の自動配置。

素材フォルダ（初期値: 本編の動画と同じ場所の「broll」フォルダ）の画像・動画を Claude に見せて
中身を短く説明してもらい（画像はそのまま、動画は始め・中ごろ・終わりの3コマ。ファイル名も手がかり）、
発言の内容と照らし合わせて「どの発言のときに、どの素材を、どう出すか」を決めます。

- 演出プランの "broll" に入ります: {"file", "start_id", "end_id", "mode": "full"|"wipe", "reason", "sec"(任意)}
  - full = 画面いっぱいに出す / wipe = 画面の隅に小さく出す（ワイプ）
  - 画像はゆっくり寄る・引く（Ken Burns）。動画は音を消して流す
- 書き出し（finish.py）では、本編の区切りごとに素材を重ねる（3時間の素材でも、一度に開く素材は少しだけ）
- Premiere 用に、荒編集のシーケンスに素材を足した「○○_Bロール入り.xml」も作る
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".wmv"}
MODES = ("full", "wipe")
MODE_JA = {"full": "全画面", "wipe": "ワイプ（隅に小さく）"}
CATALOG = "broll_assets.json"      # 素材の説明（AIが見た結果）。素材が変わらなければ使い回す
SETTINGS = "broll_settings.json"   # 選んだ素材フォルダ
BATCH_DESCRIBE = 8                 # 1回の問い合わせで見せる素材の数

DEFAULTS = {
    "folder": "broll", "auto": True, "min_sec": 2.5, "max_sec": 7.0, "fade": 0.25, "kenburns": 1.12,
    "wipe_width": 640, "wipe_position": "top-right", "wipe_margin": 40, "wipe_border": 6,
    "wipe_border_color": "#FFFFFF",
}

MODE_HINT = {
    "talk": "レビュー・解説動画。話している商品・部品・数値・比較などの写真や映像を、話題に合わせてテンポよく（20〜40秒に1回くらい）。",
    "craft": "工作動画。部品・道具・完成品・作業のアップなど、説明を助ける所に（40〜90秒に1回くらい）。",
    "outdoor": "屋外企画。地図・場所の写真・食べ物など、状況が伝わる所に。リアクションの場面はワイプにする。",
}


def config(tpl=None) -> dict:
    c = dict(DEFAULTS)
    if tpl is not None:
        c.update(tpl.get("broll", {}) or {})
    return c


def _heif():
    """iPhone の写真（HEIC）を読めるようにする（pillow-heif が入っていれば）"""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        return True
    except Exception:  # noqa: BLE001
        return False


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


# ---------------------------------------------------------------- 素材フォルダ

def save_folder(out_dir: Path, folder: Path | str | None) -> None:
    p = Path(out_dir) / SETTINGS
    if folder:
        p.write_text(json.dumps({"folder": str(Path(folder).resolve())}, ensure_ascii=False), encoding="utf-8")
    else:
        p.unlink(missing_ok=True)


def saved_folder(out_dir: Path) -> Path | None:
    try:
        f = json.loads((Path(out_dir) / SETTINGS).read_text(encoding="utf-8")).get("folder")
        return Path(f) if f else None
    except Exception:  # noqa: BLE001
        return None


def find_folder(out_dir: Path, sess: dict, c: dict, override: Path | None = None) -> Path | None:
    """使う素材フォルダ: 指定 → 画面で選んだもの → 本編と同じ場所の「broll」（「Bロール」）フォルダ"""
    if override:
        return Path(override) if Path(override).is_dir() else None
    s = saved_folder(out_dir)
    if s and s.is_dir():
        return s
    if sess.get("inputs"):
        base = Path(sess["inputs"][0]).parent
        for name in dict.fromkeys([c.get("folder") or "broll", "broll", "Bロール", "B-roll", "b-roll"]):
            if (base / name).is_dir():
                return base / name
    return None


def scan(folder: Path) -> list[Path]:
    """フォルダ（その下のフォルダも）の画像・動画。隠しファイルや作業用の名前は除く"""
    heif = _heif()
    out = []
    for p in sorted(Path(folder).rglob("*")):
        if not p.is_file() or any(part.startswith((".", "_")) for part in p.relative_to(folder).parts):
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXT or (ext in IMAGE_EXT and (heif or ext not in (".heic", ".heif"))):
            out.append(p.resolve())
    return out


# ---------------------------------------------------------------- 素材の情報

@dataclass
class Asset:
    path: Path
    kind: str           # image / video
    width: int
    height: int
    duration: float = 0.0
    description: str = ""
    keywords: list[str] = field(default_factory=list)

    @property
    def sig(self) -> str:
        return _sig(self.path)


def _sig(p: Path) -> str:
    try:
        st = Path(p).stat()
        return f"{st.st_size}-{int(st.st_mtime)}"
    except OSError:
        return ""


def _hid(p: Path) -> str:
    return hashlib.sha1(f"{p}|{_sig(p)}".encode("utf-8")).hexdigest()[:16]


def probe_asset(path: Path) -> Asset | None:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        _heif()
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)   # スマホの縦写真の向きを直す
                return Asset(path, "image", im.width, im.height)
        except Exception:  # noqa: BLE001
            return None
    if ext in VIDEO_EXT:
        p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                            str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return None
        info = json.loads(p.stdout or "{}")
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if not v:
            return None
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if "rotate" in (v.get("tags") or {}):
            rot = int(v["tags"]["rotate"])
        if abs(rot) % 180 == 90:
            w, h = h, w
        dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
        return Asset(path, "video", w, h, dur)
    return None


def _grab_frame(path: Path, t: float, height: int = 288):
    from PIL import Image
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale=-2:{height}", "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return None
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


def thumb_path(out_dir: Path, path: Path) -> Path:
    return Path(out_dir) / "broll_thumbs" / f"{_hid(Path(path))}.jpg"


def make_thumb(out_dir: Path, a: Asset) -> Path | None:
    """AIに見せる・画面に出す小さな画像。動画は始め・中ごろ・終わりの3コマを横に並べる"""
    from PIL import Image, ImageOps
    dst = thumb_path(out_dir, a.path)
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if a.kind == "image":
            with Image.open(a.path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((768, 768))
                im.save(dst, "JPEG", quality=85)
            return dst
        frames = [f for f in (_grab_frame(a.path, a.duration * r) for r in (0.1, 0.5, 0.85)) if f]
        if not frames:
            return None
        tile = Image.new("RGB", (sum(f.width for f in frames) + 8 * (len(frames) - 1), frames[0].height), "black")
        x = 0
        for f in frames:
            tile.paste(f, (x, 0))
            x += f.width + 8
        tile.thumbnail((1152, 1152))
        tile.save(dst, "JPEG", quality=85)
        return dst
    except Exception:  # noqa: BLE001
        return None


def load_catalog(out_dir: Path) -> dict:
    try:
        return json.loads((Path(out_dir) / CATALOG).read_text(encoding="utf-8")).get("assets", {})
    except Exception:  # noqa: BLE001
        return {}


def save_catalog(out_dir: Path, cat: dict) -> None:
    (Path(out_dir) / CATALOG).write_text(json.dumps({"assets": cat}, ensure_ascii=False, indent=1), encoding="utf-8")


def catalog_assets(out_dir: Path, files: list[Path]) -> list[Asset]:
    """素材の一覧（大きさ・長さ）。前に調べた説明があれば付ける"""
    cat = load_catalog(out_dir)
    out = []
    for f in files:
        a = probe_asset(f)
        if not a:
            print(f"  [注意] 素材を読めませんでした（飛ばします）: {f.name}")
            continue
        old = cat.get(str(f))
        if old and old.get("sig") == a.sig:
            a.description, a.keywords = old.get("description", ""), old.get("keywords", [])
        out.append(a)
    return out


def _store(out_dir: Path, assets: list[Asset]) -> None:
    cat = load_catalog(out_dir)
    for a in assets:
        cat[str(a.path)] = {"sig": a.sig, "kind": a.kind, "width": a.width, "height": a.height,
                            "duration": round(a.duration, 2), "description": a.description, "keywords": a.keywords}
    save_catalog(out_dir, cat)


# ---------------------------------------------------------------- Claude: 素材を見て説明する

DESC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["assets"],
    "properties": {"assets": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["key", "description", "keywords"],
        "properties": {"key": {"type": "string"}, "description": {"type": "string"},
                       "keywords": {"type": "array", "items": {"type": "string"}}}}}},
}

DESC_SYSTEM = """あなたは動画編集のアシスタントです。YouTube動画の差し込み素材（Bロール）として用意された写真・動画を見て、
それぞれに何が写っているかを短く説明してください。あとで「どの発言の時にこの素材を出すか」を決めるのに使います。

- key: 素材の記号（入力の [A1] などの中身。例: A1）
- description: 写っているもの・場面を40字以内で（商品名・型番・場所・料理名など、分かれば具体的に。ファイル名も手がかりにする）
- keywords: 発言と照らし合わせるための言葉を3〜8個（話し言葉で出てきそうな言い方も。例: ドリル, 電動ドリル, マキタ）
- 動画は、左から「始め・中ごろ・終わり」の3コマを並べた画像です
出力はJSONのみです。"""


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def describe_api(out_dir: Path, assets: list[Asset], effort: str, usage: dict) -> None:
    """説明がまだ無い素材を Claude に見せて、説明とキーワードを付ける（結果は保存して使い回す）"""
    import rc_llm
    todo = [a for a in assets if not a.description]
    if not todo:
        return
    anthropic, client = rc_llm._client()
    batches = [todo[i:i + BATCH_DESCRIBE] for i in range(0, len(todo), BATCH_DESCRIBE)]
    for n, batch in enumerate(batches, 1):
        print(f"  Claudeで素材を見ています… ({n}/{len(batches)})", flush=True)
        content, keys = [], {}
        for k, a in enumerate(batch, 1):
            key = f"A{k}"
            keys[key] = a
            what = "写真" if a.kind == "image" else f"動画 {a.duration:.1f}秒（左から始め・中ごろ・終わりのコマ）"
            content.append({"type": "text", "text": f"[{key}] ファイル名: {a.path.name}（{what}）"})
            t = make_thumb(out_dir, a)
            if t:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": _b64(t)}})
        res = rc_llm._call(client, anthropic, DESC_SYSTEM, content, DESC_SCHEMA, effort, usage)
        for r in res.get("assets", []):
            a = keys.get(str(r.get("key", "")).strip("[] "))
            if a:
                a.description = str(r.get("description", ""))[:120]
                a.keywords = [str(x) for x in r.get("keywords", [])][:12]
        for a in batch:   # 説明が返ってこなかった素材は、ファイル名を説明の代わりにする
            if not a.description:
                a.description = a.path.stem
        _store(out_dir, batch)


# ---------------------------------------------------------------- Claude: 発言と照らし合わせる

MATCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["broll"],
    "properties": {"broll": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["asset", "start_id", "end_id", "mode", "reason"],
        "properties": {"asset": {"type": "string"}, "start_id": {"type": "integer"}, "end_id": {"type": "integer"},
                       "mode": {"type": "string", "enum": list(MODES)}, "reason": {"type": "string"}}}}},
}


def _match_system(mode: str, c: dict) -> str:
    return f"""あなたはYouTube動画の編集者です。カット済みの動画の文字起こしと、差し込み素材（Bロール）の一覧を読んで、
どの発言の時にどの素材を画面に出すかを決めてください。
{MODE_HINT.get(mode, MODE_HINT['talk'])}

## 入力
- 素材: `[記号] 種類 ファイル名: 説明（キーワード）`
- 文字起こし: `#ID [編集後の時刻] テキスト`

## 決め方
- 発言が素材の中身にはっきり触れている所にだけ出す（関係の薄い所に無理に出さない）
- start_id〜end_id の発言の間、素材が出ます。{c['min_sec']:.0f}〜{c['max_sec']:.0f}秒くらい（発言1〜3個）にする
- mode: full=画面いっぱい（物や場所そのものを見せたい時）/ wipe=画面の隅に小さく（話している人の表情やリアクションも見せたい時）
- 同じ素材は基本1回まで。素材どうしが重ならないように、間をあける
- 動画の最初の発言（あいさつ）には出さない
- reason: 選んだ理由を20字以内で
出力はJSONのみです。"""


def _asset_lines(assets: list[Asset]) -> tuple[str, dict]:
    keys, lines = {}, []
    for k, a in enumerate(assets, 1):
        key = f"B{k}"
        keys[key] = a
        kind = "写真" if a.kind == "image" else f"動画{a.duration:.0f}秒"
        kw = f"（{'、'.join(a.keywords)}）" if a.keywords else ""
        lines.append(f"[{key}] {kind} {a.path.name}: {a.description or a.path.stem}{kw}")
    return "\n".join(lines), keys


def match_api(items, assets: list[Asset], mode: str, c: dict, effort: str, usage: dict) -> list[dict]:
    import rc_llm
    import rc_plan
    anthropic, client = rc_llm._client()
    text, keys = _asset_lines(assets)
    print("  Claudeで素材を出す場所を決めています…", flush=True)
    res = rc_llm._call(client, anthropic, _match_system(mode, c),
                       f"## 素材\n{text}\n\n## 文字起こし\n{rc_plan.plan_lines(items)}", MATCH_SCHEMA, effort, usage)
    out = []
    for r in res.get("broll", []):
        a = keys.get(str(r.get("asset", "")).strip("[] "))
        if a:
            out.append({"file": str(a.path), "start_id": r.get("start_id"), "end_id": r.get("end_id"),
                        "mode": r.get("mode", "full"), "reason": str(r.get("reason", ""))[:60]})
    return out


# ---------------------------------------------------------------- AIなし: ファイル名で照らし合わせる

_GENERIC = {"img", "dsc", "dscn", "mov", "pxl", "vid", "video", "image", "photo", "broll", "copy", "コピー",
            "screenshot", "スクリーンショット", "jpg", "png", "mp4"}


def _words(a: Asset) -> list[str]:
    ws = [w for w in re.split(r"[\s_\-－ー・,，.。()（）\[\]【】「」+＋&＆0-9０-９]+", a.path.stem)
          if len(w) >= 2 and w.lower() not in _GENERIC]
    return list(dict.fromkeys(ws + [k for k in a.keywords if len(k) >= 2]))


def match_by_name(items, assets: list[Asset], c: dict) -> list[dict]:
    """ファイル名（と、前にAIが付けたキーワード）が発言に出てくる所に、画面いっぱいで出す"""
    out, used_t = [], []
    for a in assets:
        words = [w.lower() for w in _words(a)]
        if not words:
            continue
        for k, (cid, s, _e, text) in enumerate(items):
            if k == 0:
                continue   # 最初のあいさつには出さない
            t = text.lower()
            if any(w in t for w in words) and all(abs(s - u) > c["max_sec"] + 1 for u in used_t):
                hit = next(w for w in words if w in t)
                out.append({"file": str(a.path), "start_id": cid, "end_id": cid, "mode": "full",
                            "reason": f"「{hit}」と話している所"})
                used_t.append(s)
                break
    return out


# ---------------------------------------------------------------- プランへの反映

def sanitize(items: list, valid_ids: set[int]) -> list[dict]:
    out = []
    for b in items or []:
        if not isinstance(b, dict) or not b.get("file") or b.get("start_id") not in valid_ids:
            continue
        b = dict(b)
        if b.get("end_id") not in valid_ids or b["end_id"] < b["start_id"]:
            b["end_id"] = b["start_id"]
        if b.get("mode") not in MODES:
            b["mode"] = "full"
        b.setdefault("reason", "")
        out.append(b)
    return out


def load_items(out_dir: Path, sess: dict, fps: Fraction | None = None):
    """(タイムライン, 発言ID→チャンク, [(id, 編集後の開始, 終了, テキスト)]) を作る（finish.py と同じ計算）"""
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    from rc_transcribe import Chunk
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], fps or src.main.fps)
    chunks = [Chunk(**c) for c in json.loads((Path(out_dir) / "chunks.json").read_text(encoding="utf-8"))]
    cut = set(sess["cut_ids"])
    corr = {int(k): v for k, v in sess.get("corrections", {}).items()}
    items = []
    for c in chunks:
        if c.id in cut:
            continue
        r = tl.map_range(c.start, c.end)
        if r:
            items.append((c.id, r[0][0], r[-1][1], corr.get(c.id, c.text)))
    return tl, {c.id: c for c in chunks}, items


def place(out_dir: Path, sess: dict, items, c: dict, llm: str, effort: str, usage: dict,
          folder: Path | None = None) -> list[dict] | None:
    """素材フォルダの素材を発言に割り当てる。フォルダが無ければ None"""
    folder = find_folder(out_dir, sess, c, folder)
    if not folder:
        return None
    files = scan(folder)
    print(f"■ Bロール: {folder}（素材 {len(files)}個）")
    if not files:
        return []
    assets = catalog_assets(out_dir, files)
    if not assets:
        return []
    if llm == "api":
        try:
            describe_api(out_dir, assets, "low", usage)
            res = match_api(items, assets, sess.get("mode", "talk"), c, effort, usage)
        except Exception as e:  # noqa: BLE001  Bロールの失敗で全体を止めない
            print(f"  [注意] AIでBロールを決められませんでした（ファイル名で合わせます）: {e}")
            res = match_by_name(items, assets, c)
    else:
        if llm == "manual":
            print("  [お知らせ] Bロールは、ファイル名が発言に出てくる所に入れます（画面の「Bロール」で変更できます）")
        res = match_by_name(items, assets, c)
    for a in assets:   # 画面に出す小さな画像を用意しておく
        make_thumb(out_dir, a)
    res = sanitize(res, {i for i, *_ in items})
    print(f"  Bロール {len(res)}か所")
    return res


def auto_place(out_dir: Path, sess: dict, tpl, items, plan: dict, llm: str, effort: str, usage: dict) -> None:
    """finish.py から: 新しく演出プランを作るとき、素材フォルダがあれば Bロールも決める"""
    c = config(tpl)
    if not c.get("auto", True) or "broll" in plan:
        return
    try:
        res = place(out_dir, sess, items, c, llm, effort, usage)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        print(f"  [注意] Bロールを決められませんでした: {e}")
        return
    if res is not None:
        plan["broll"] = res


# ---------------------------------------------------------------- 時刻を決める

@dataclass
class Placement:
    t0: float
    t1: float
    item: dict
    asset: Asset


def resolve(items: list[dict], span: dict, body_dur: float, c: dict, metas: dict) -> list[Placement]:
    """プランの Bロール → 編集後の時刻（重なったら前のものを短くする）"""
    out = []
    for it in items or []:
        a = metas.get(str(it.get("file")))
        sid = it.get("start_id")
        if not a or sid not in span:
            continue
        t0 = span[sid][0]
        if it.get("sec"):
            t1 = t0 + float(it["sec"])
        else:
            t1 = span.get(it.get("end_id"), span[sid])[1]
            t1 = min(max(t1, t0 + float(c["min_sec"])), t0 + float(c["max_sec"]))
        if a.kind == "video":
            t1 = min(t1, t0 + max(0.5, a.duration - float(it.get("start", 0) or 0)))
        t1 = min(t1, body_dur)
        if t1 - t0 >= 1.0:
            out.append(Placement(t0, t1, it, a))
    out.sort(key=lambda p: p.t0)
    res: list[Placement] = []
    for p in out:
        while res and p.t0 < res[-1].t1:
            res[-1].t1 = p.t0
            if res[-1].t1 - res[-1].t0 < 1.0:
                res.pop()
            else:
                break
        res.append(p)
    return res


def probe_files(items: list[dict], out_dir: Path | None = None) -> dict[str, Asset]:
    metas = {}
    for f in dict.fromkeys(str(it.get("file")) for it in items or []):
        if not Path(f).exists():
            print(f"  [注意] Bロールの素材が見つかりません（飛ばします）: {f}")
            continue
        a = probe_asset(Path(f))
        if a:
            metas[f] = a
    return metas


# ---------------------------------------------------------------- 書き出し（FFmpeg）

@dataclass
class Overlay:
    """本編の1区切りに重ねる素材（rc_render.Seg.broll）"""
    src: Path            # FFmpeg が読むファイル（写真は縮小・向きを直したもの）
    kind: str            # image / video
    mode: str            # full / wipe
    item_frames: int     # この素材を出す長さ（フレーム）
    f0: int = 0          # この区切りが、素材を出し始めてから何フレーム目か
    src_start: float = 0.0
    fit: str = "cover"   # cover=はみ出す所を切る / blur=ぼかした背景の上に全体を出す
    zoom_from: float = 1.0
    zoom_to: float = 1.0
    tw: int = 1920       # 素材を出す大きさ（ふちを除く）
    th: int = 1080
    x: int = 0
    y: int = 0
    border: int = 0
    border_color: str = "0xFFFFFF"
    fade_frames: int = 0


def prepare_image(path: Path, cache: Path, longest: int) -> Path:
    """写真の向きを直し、書き出しに十分な大きさまで縮小した JPEG を作る（毎コマ読み直すので軽くする）"""
    from PIL import Image, ImageOps
    _heif()
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / f"{_hid(path)}_{longest}.jpg"
    if dst.exists():
        return dst
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (17, 17, 17, 255))
            bg.alpha_composite(im)
            im = bg
        im = im.convert("RGB")
        im.thumbnail((longest, longest), Image.LANCZOS)
        im.save(dst, "JPEG", quality=92)
    return dst


def _wipe_corner(c: dict, tpl) -> str:
    pos = c.get("wipe_position", "top-right")
    lg = (tpl.get("logo", {}) if tpl is not None else {}) or {}
    if lg.get("file") and lg.get("position", "top-right") == pos:   # ロゴと重ならないように反対側へ
        pos = pos.replace("right", "@").replace("left", "right").replace("@", "left")
    return pos


def make_overlays(placements: list[Placement], c: dict, tpl, fps: Fraction, w: int, h: int,
                  cache: Path) -> list[tuple[int, int, Overlay]]:
    out = []
    kb = max(1.0, float(c.get("kenburns", 1.12)))
    for k, p in enumerate(placements):
        a, mode = p.asset, p.item.get("mode", "full")
        P0, P1 = round(p.t0 * fps), round(p.t1 * fps)
        if P1 - P0 < 2:
            continue
        ar = a.width / max(a.height, 1)
        if mode == "wipe":
            bw = int(c.get("wipe_width", 640))
            bh = bw * 9 / 16
            tw, th = (bw, bw / ar) if ar >= bw / bh else (bh * ar, bh)
            tw, th = _even(tw), _even(th)
            fit, b = "cover", int(c.get("wipe_border", 6))
            m = int(c.get("wipe_margin", 40))
            pos = _wipe_corner(c, tpl)
            x = m if "left" in pos else w - tw - 2 * b - m
            y = m if "top" in pos else h - th - 2 * b - m
            z = 1 + (kb - 1) * 0.5
        else:
            tw, th, x, y, b = w, h, 0, 0, 0
            fit = "cover" if abs(math.log(ar / (w / h))) < math.log(1.4) else "blur"
            z = kb
        src = a.path
        if a.kind == "image":
            src = prepare_image(a.path, cache, 2 * max(tw, th))
        zf, zt = (1.0, z) if k % 2 == 0 else (z, 1.0)   # 寄る・引くを交互に
        col = "0x" + str(c.get("wipe_border_color", "#FFFFFF")).lstrip("#")
        ov = Overlay(src, a.kind, mode, P1 - P0, 0, float(p.item.get("start", 0) or 0), fit, zf, zt,
                     tw, th, x, y, b, col, min(round(float(c.get("fade", 0.25)) * fps), (P1 - P0) // 3))
        out.append((P0, P1, ov))
    return out


def attach_to_segs(segs: list, plan_items: list[dict], span: dict, tpl, fps: Fraction, w: int, h: int,
                   out_dir: Path) -> list:
    """本編の区切り（rc_render.Seg）を Bロールの出入りの位置で分け、重ねる素材を付ける"""
    if not plan_items:
        return segs
    c = config(tpl)
    metas = probe_files(plan_items)
    body_dur = float(sum(s.frames for s in segs) / fps)
    placements = resolve(plan_items, span, body_dur, c, metas)
    ovs = make_overlays(placements, c, tpl, fps, w, h, Path(out_dir) / "broll_cache")
    if not ovs:
        return segs
    print(f"  Bロール {len(ovs)}か所を重ねます")
    new, F = [], 0
    for s in segs:
        n = s.frames
        cuts = {0, n}
        for P0, P1, _ in ovs:
            for bnd in (P0, P1):
                if F < bnd < F + n:
                    cuts.add(bnd - F)
        cuts = sorted(cuts)
        for k0, k1 in zip(cuts, cuts[1:]):
            mid = F + (k0 + k1) / 2
            hit = next(((P0, ov) for P0, P1, ov in ovs if P0 <= mid < P1), None)
            # 早送り（speed）・2台目カメラ（video_start）の区切りも、正しい位置で分ける
            import rc_render
            sub = rc_render.sub_seg(s, k0, k1, fps)
            if hit:
                sub.broll = dataclasses.replace(hit[1], f0=F + k0 - hit[0])
            new.append(sub)
        F += n
    return new


def seg_filter(ov: Overlay, idx: int, in_label: str, out_label: str, fps: Fraction, frames: int,
               tag: str) -> tuple[list[str], str]:
    """本編の1区切り（in_label）に素材を重ねる FFmpeg の入力引数とフィルター。パスはフィルターに書かない"""
    dur = float(Fraction(frames) / fps) + 0.5
    if ov.kind == "image":
        inp = ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}", "-i", str(ov.src)]
        k = 2   # 2倍の大きさから切り出して、ゆっくり動かしてもガタつかないようにする
    else:
        inp = ["-ss", f"{ov.src_start + float(Fraction(ov.f0) / fps):.4f}", "-t", f"{dur:.4f}", "-i", str(ov.src)]
        k = 1
    W2, H2 = ov.tw * k, ov.th * k
    parts = []
    if ov.fit == "blur":
        sw, sh = _even(W2 / 8), _even(H2 / 8)
        parts.append(f"[{idx}:v]split[{tag}a][{tag}b]")
        parts.append(f"[{tag}a]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
                     f"boxblur=10:3,colorchannelmixer=rr=0.8:gg=0.8:bb=0.8,scale={W2}:{H2},setsar=1,format=yuv420p[{tag}g]")
        parts.append(f"[{tag}b]scale={W2}:{H2}:force_original_aspect_ratio=decrease,setsar=1,format=yuv420p[{tag}f]")
        parts.append(f"[{tag}g][{tag}f]overlay=(W-w)/2:(H-h)/2,format=yuv420p[{tag}p]")
    else:
        parts.append(f"[{idx}:v]scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2},setsar=1,"
                     f"format=yuv420p[{tag}p]")
    if ov.kind == "image":
        dz = ov.zoom_to - ov.zoom_from
        n = max(ov.item_frames - 1, 1)
        chain = (f"[{tag}p]zoompan=z='{ov.zoom_from:.5f}+({dz:.5f})*min(1,(in+{ov.f0})/{n})'"
                 f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={ov.tw}x{ov.th}:fps={fps},setsar=1")
    else:
        chain = f"[{tag}p]fps={fps},tpad=stop_mode=clone:stop_duration={dur:.3f}"
    chain += f",trim=end_frame={frames},setpts=PTS-STARTPTS"
    if ov.border:
        b = ov.border
        chain += f",pad=iw+{2 * b}:ih+{2 * b}:{b}:{b}:color={ov.border_color}"
    chain += ",format=yuva420p"
    if ov.fade_frames and ov.f0 == 0:
        chain += f",fade=t=in:s=0:n={ov.fade_frames}:alpha=1"
    if ov.fade_frames and ov.f0 + frames >= ov.item_frames:
        chain += f",fade=t=out:s={max(0, frames - ov.fade_frames)}:n={ov.fade_frames}:alpha=1"
    parts.append(chain + f"[{tag}o]")
    # 重ねる素材は本編の区切りと同じコマ数に切りそろえてあるので、両方が同時に終わる。
    # eof_action=pass は FFmpeg 9 で書き出しが止まる（デッドロック）ことがあるので、既定の repeat にする
    parts.append(f"{in_label}[{tag}o]overlay=x={ov.x}:y={ov.y}:eof_action=repeat,format=yuv420p{out_label}")
    return inp, ";".join(parts)


# ---------------------------------------------------------------- Premiere 用 XML

def _rate(fps: Fraction) -> str:
    from rc_export import _rate as r
    return r(fps)


def _motion(scale_from: float, scale_to: float, length: int, horiz: float = 0.0, vert: float = 0.0) -> str:
    kf = ""
    if abs(scale_to - scale_from) > 0.01:
        kf = (f"<keyframe><when>0</when><value>{scale_from:.2f}</value></keyframe>"
              f"<keyframe><when>{length}</when><value>{scale_to:.2f}</value></keyframe>")
    return ("<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion</effectcategory>"
            "<effecttype>motion</effecttype><mediatype>video</mediatype>"
            f"<parameter><parameterid>scale</parameterid><name>Scale</name><valuemin>0</valuemin>"
            f"<valuemax>1000</valuemax><value>{scale_from:.2f}</value>{kf}</parameter>"
            f"<parameter><parameterid>center</parameterid><name>Center</name>"
            f"<value><horiz>{horiz:.4f}</horiz><vert>{vert:.4f}</vert></value></parameter>"
            "</effect></filter>")


def broll_tracks_xml(placements: list[Placement], c: dict, tpl, seq_fps: Fraction, W: int, H: int) -> str:
    """Bロールのトラック（全画面 → ワイプの順。どちらも空なら作らない）"""
    from xml.sax.saxutils import escape

    from rc_export import _pathurl
    still_len = 1079892   # Premiere が静止画に付ける長さと同じ（約10時間）
    kb = max(1.0, float(c.get("kenburns", 1.12)))
    tracks = {"full": [], "wipe": []}
    defined, fids = set(), {}
    for k, p in enumerate(placements):
        a = p.asset
        mode = p.item.get("mode", "full")
        st, en = round(p.t0 * seq_fps), round(p.t1 * seq_fps)
        if en <= st:
            continue
        key = str(a.path)
        if key not in fids:
            fids[key] = f"file-broll-{len(fids) + 1}"
        fid = fids[key]
        if a.kind == "image":
            frate, fdur, fin = seq_fps, still_len, 0
        else:
            from rc_media import snap_fps
            fr = _video_fps(a.path) or seq_fps
            frate = snap_fps(fr)
            fdur, fin = round(a.duration * frate), round(float(p.item.get("start", 0) or 0) * frate)
        fout = fin + round((en - st) * frate / seq_fps)
        if fid in defined:
            fxml = f'<file id="{fid}"/>'
        else:
            defined.add(fid)
            fxml = (f'<file id="{fid}"><name>{escape(a.path.name)}</name><pathurl>{_pathurl(a.path)}</pathurl>'
                    f"{_rate(frate)}<duration>{fdur}</duration><media><video><samplecharacteristics>{_rate(frate)}"
                    f"<width>{a.width}</width><height>{a.height}</height></samplecharacteristics></video></media></file>")
        if mode == "wipe":
            bw = int(c.get("wipe_width", 640))
            bh = bw * 9 / 16
            ar = a.width / max(a.height, 1)
            tw, th = (bw, bw / ar) if ar >= bw / bh else (bh * ar, bh)
            s0 = tw / max(a.width, 1) * 100
            m = int(c.get("wipe_margin", 40)) + int(c.get("wipe_border", 6))
            pos = _wipe_corner(c, tpl)
            cx = m + tw / 2 if "left" in pos else W - m - tw / 2
            cy = m + th / 2 if "top" in pos else H - m - th / 2
            horiz, vert = (cx - W / 2) / W, (cy - H / 2) / H
            z = 1 + (kb - 1) * 0.5
        else:
            s0 = max(W / max(a.width, 1), H / max(a.height, 1)) * 100
            horiz = vert = 0.0
            z = kb
        zf, zt = (1.0, z) if (a.kind == "image" and k % 2 == 0) else ((z, 1.0) if a.kind == "image" else (1, 1))
        cid = f"clipitem-broll-{k + 1}"
        tracks[mode].append(
            f'<clipitem id="{cid}"><name>{escape(a.path.name)}</name><enabled>TRUE</enabled>'
            f"<duration>{fdur}</duration>{_rate(frate)}<start>{st}</start><end>{en}</end>"
            f"<in>{fin}</in><out>{fout}</out>{fxml}{_motion(s0 * zf, s0 * zt, en - st, horiz, vert)}</clipitem>")
    return "".join(f"<track>{''.join(v)}<enabled>TRUE</enabled><locked>FALSE</locked></track>"
                   for v in tracks.values() if v)


def _video_fps(path: Path) -> Fraction | None:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        r = Fraction(p.stdout.strip())
        return r if r > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def write_xml(out_dir: Path, sess: dict, plan: dict, tpl=None) -> Path | None:
    """荒編集のシーケンス（カメラ2台目・マーカーも）に、Bロールのトラックを足した XML を作る"""
    from rc_export import write_xmeml
    from rc_media import SourceSet, probe
    items_plan = plan.get("broll") or []
    out_dir = Path(out_dir)
    path = out_dir / f"{sess['stem']}_Bロール入り.xml"
    if not items_plan:
        return None
    c = config(tpl)
    tl, by_id, items = load_items(out_dir, sess)
    span = {i: (s, e) for i, s, e, _ in items}
    placements = resolve(items_plan, span, tl.duration, c, probe_files(items_plan))
    if not placements:
        return None
    cam2, offset = None, 0.0
    cam2_files = (sess.get("args") or {}).get("cam2") or []
    if cam2_files and all(Path(p).exists() for p in cam2_files):
        try:
            cam2 = SourceSet([probe(Path(p)) for p in cam2_files])
            offset = json.loads((out_dir / "sync.json").read_text(encoding="utf-8")).get("offset_sec", 0.0)
        except (Exception, SystemExit):  # noqa: BLE001
            cam2 = None
    markers = [(m["t"], m["name"], m.get("comment", "")) for m in sess.get("markers", [])]
    write_xmeml(path, f"{sess['stem']}_Bロール入り", tl, markers, cam2, offset)
    xml = path.read_text(encoding="utf-8")
    main = tl.sources.main
    tracks = broll_tracks_xml(placements, c, tpl, tl.fps, main.width, main.height)
    anchor = "</video><audio><numOutputChannels>"
    xml = xml.replace(anchor, tracks + anchor, 1)
    path.write_text(xml, encoding="utf-8")
    return path


# ---------------------------------------------------------------- プランのファイル

def update_plan_files(out_dir: Path, broll: list[dict]) -> list[Path]:
    """plan_used.json と、あれば plan_edited.json（画面の手直し）の Bロールを入れ替える"""
    done = []
    for name in ("plan_used.json", "plan_edited.json"):
        p = Path(out_dir) / name
        if not p.exists():
            continue
        plan = json.loads(p.read_text(encoding="utf-8"))
        plan["broll"] = broll
        if isinstance(plan.get("_off"), dict):
            plan["_off"].pop("broll", None)
        p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        done.append(p)
    return done
