"""見た目テンプレート（template.toml）の編集を助ける部品。

- PC に入っているフォントの一覧（ファイルの中の名前を直接読むので、追加のライブラリは不要）
- template.toml の書き出し（値の意味をコメントで添える）
- 字幕・テロップ4種・章カード・サムネイルを、背景の1コマに焼き込んだ見本画像
  （本番と同じ rc_ass.build_ass と FFmpeg の ass フィルターで作るので、仕上がりと同じ見た目）
- Claude に「チャンネルの雰囲気」からデザイン案を3つ作ってもらう（使えるフォントの中から選ぶ）
- 別名で保存（templates/<名前>/ に、BGM・効果音などのファイルごと複製）

画面（gui_ext_design.py）とコマンド（design.py）の両方から使う。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import rc_win  # noqa: F401  (Windowsで黒い画面を出さない)
from rc_template import Template

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
WORK_NAME = "_design_work"
FONT_EXT = {".ttf", ".otf", ".ttc", ".otc"}

TELOPS = [("emphasis", "強調"), ("info", "補足"), ("warning", "注意"), ("funny", "ツッコミ")]
POSITIONS = [("top", "上"), ("middle", "真ん中"), ("top-left", "左上"), ("bottom", "下")]
ANIMATIONS = [("pop", "ポンッと出る"), ("fade", "ふわっと出る"), ("slide", "すべり込む")]

# 見本画像の一覧（キー, 画面に出す名前）。上の4つは字幕＋テロップ、章カード、サムネイル2種
PANELS = [("emphasis", "強調"), ("info", "補足"), ("warning", "注意"), ("funny", "ツッコミ"),
          ("chapter", "章カード"), ("thumb", "サムネイル（下に文字）"), ("thumb_band", "サムネイル（上に帯）")]
# 見本に使う文字（scene の順番 = PANELS の先頭5つ）
SAMPLE_CAPTIONS = ["これは字幕のサンプルです", "ここが一番大事なポイントです", "ここは気をつけてください",
                   "思わず笑ってしまいました", "それでは実際に使ってみます"]
SAMPLE_TELOPS = {"emphasis": "重さ わずか1.2kg！", "info": "マキタ DF033D", "warning": "ここ注意",
                 "funny": "いや軽すぎ！"}
SAMPLE_CHAPTER = "実際に使ってみた"
SAMPLE_THUMB = "軽すぎて\n笑った"
PANEL_W, PANEL_H = 960, 540


# ============================================================
#  フォント一覧（TrueType / OpenType のファイルから名前を読む）
# ============================================================
@dataclass
class FontFace:
    path: Path
    index: int
    family: str                  # template.toml に書く名前（英語名があれば英語名）
    names: set[str] = field(default_factory=set)   # 英語・日本語などすべての名前
    local: str = ""              # 日本語の名前（あれば）
    weight: int = 400
    japanese: bool = False
    user_font: bool = False      # Windows の「このユーザーだけ」に入れたフォント
    in_template: bool = False    # テンプレートの fonts フォルダのフォント（日本語の印がなくても一覧に出す）


def _decode_name(pid: int, eid: int, raw: bytes) -> str | None:
    try:
        if pid in (0, 3):
            return raw.decode("utf-16-be")
        if pid == 1 and eid == 0:
            return raw.decode("mac_roman")
        if pid == 1 and eid == 1:
            return raw.decode("shift_jis")
    except UnicodeDecodeError:
        return None
    return None


def _read_face(f, base: int) -> dict | None:
    """sfnt（1つのフォント）の name / OS/2 テーブルを読む"""
    f.seek(base)
    head = f.read(12)
    if len(head) < 12:
        return None
    num = struct.unpack(">H", head[4:6])[0]
    if num > 200:
        return None
    tables = {}
    data = f.read(16 * num)
    for i in range(num):
        tag, _cs, off, length = struct.unpack(">4sIII", data[i * 16:i * 16 + 16])
        tables[tag] = (off, length)
    if b"name" not in tables:
        return None
    off, length = tables[b"name"]
    f.seek(off)
    nt = f.read(min(length, 1 << 20))
    if len(nt) < 6:
        return None
    _fmt, count, str_off = struct.unpack(">HHH", nt[:6])
    fam: dict[int, list[tuple[int, int, str]]] = {}
    for i in range(count):
        rec = nt[6 + i * 12:18 + i * 12]
        if len(rec) < 12:
            break
        pid, eid, lang, nid, ln, so = struct.unpack(">HHHHHH", rec)
        if nid not in (1, 2, 16):
            continue
        s = _decode_name(pid, eid, nt[str_off + so:str_off + so + ln])
        if s:
            s = s.replace("\x00", "").strip()
            if s:
                fam.setdefault(nid, []).append((pid, lang, s))
    if 1 not in fam:
        return None
    weight, ja = 400, False
    if b"OS/2" in tables:
        off, length = tables[b"OS/2"]
        f.seek(off)
        os2 = f.read(min(length, 100))
        if len(os2) >= 6:
            weight = struct.unpack(">H", os2[4:6])[0]
        if len(os2) >= 82 and struct.unpack(">H", os2[:2])[0] >= 1:
            cp1 = struct.unpack(">I", os2[78:82])[0]
            ja = bool(cp1 & (1 << 17))     # ビット17 = 日本語（Shift-JIS）
    return {"names": fam, "weight": weight, "ja": ja}


def _faces_in_file(path: Path) -> list[tuple[int, dict]]:
    out = []
    try:
        with open(path, "rb") as f:
            tag = f.read(4)
            if tag == b"ttcf":
                f.read(4)
                n = struct.unpack(">I", f.read(4))[0]
                offs = struct.unpack(f">{min(n, 64)}I", f.read(4 * min(n, 64)))
                for i, o in enumerate(offs):
                    r = _read_face(f, o)
                    if r:
                        out.append((i, r))
            elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                r = _read_face(f, 0)
                if r:
                    out.append((0, r))
    except (OSError, struct.error):
        pass
    return out


def _pick(names: list[tuple[int, int, str]], lang: int) -> str | None:
    for pid, lg, s in names:
        if pid == 3 and lg == lang:
            return s
    return None


def read_font_file(path: Path) -> list[FontFace]:
    faces = []
    for idx, r in _faces_in_file(Path(path)):
        n1 = r["names"].get(1, [])
        en = _pick(n1, 0x409) or next((s for pid, _l, s in n1 if pid == 1), None) or n1[0][2]
        ja_name = _pick(n1, 0x411) or ""
        names = {s for lst in r["names"].values() for _p, _l, s in lst} - {s for _p, _l, s in r["names"].get(2, [])}
        sub = " ".join(s for _p, _l, s in r["names"].get(2, [])).lower()
        weight = r["weight"] or (700 if "bold" in sub else 400)
        faces.append(FontFace(Path(path), idx, en, names, ja_name if ja_name != en else "", weight,
                              r["ja"] or bool(ja_name)))
    return faces


def system_font_dirs() -> list[tuple[Path, bool]]:
    """(フォルダ, ユーザー用か)"""
    home = Path.home()
    if os.name == "nt":
        return [(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", False),
                (Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "Microsoft" / "Windows" /
                 "Fonts", True)]
    if sys.platform == "darwin":
        return [(Path("/System/Library/Fonts"), False), (Path("/Library/Fonts"), False),
                (home / "Library" / "Fonts", False)]
    return [(Path("/usr/share/fonts"), False), (Path("/usr/local/share/fonts"), False),
            (home / ".fonts", False), (home / ".local" / "share" / "fonts", False)]


_SCAN_CACHE: dict[str, list[FontFace]] = {}


def scan_fonts(extra_dirs=()) -> list[FontFace]:
    """PC のフォントと、テンプレートの fonts フォルダのフォントを一覧にする（結果は覚えておく）"""
    dirs = [(Path(d), None) for d in extra_dirs if d] + system_font_dirs()
    out = []
    for d, user in dirs:
        key = (str(d.resolve()) if d.exists() else str(d)) + ("|t" if user is None else "")
        if key not in _SCAN_CACHE:
            faces = []
            if d.is_dir():
                for p in sorted(d.rglob("*")):
                    if p.suffix.lower() in FONT_EXT and p.is_file():
                        for fc in read_font_file(p):
                            fc.user_font = bool(user)
                            fc.in_template = user is None
                            faces.append(fc)
            _SCAN_CACHE[key] = faces
        out += _SCAN_CACHE[key]
    return out


def font_families(faces: list[FontFace], japanese_only: bool = True) -> list[dict]:
    """[{name: template に書く名前, label: 画面に出す名前, japanese}]（名前の重複はまとめる）"""
    seen: dict[str, dict] = {}
    for fc in faces:
        if japanese_only and not (fc.japanese or fc.in_template):
            continue
        k = fc.family.lower()
        if k not in seen:
            label = f"{fc.family}（{fc.local}）" if fc.local else fc.family
            seen[k] = {"name": fc.family, "label": label, "japanese": fc.japanese}
    return sorted(seen.values(), key=lambda x: (not x["japanese"], x["label"].lower()))


def find_face(faces: list[FontFace], family: str, prefer_weight: int = 700) -> FontFace | None:
    """名前（英語名・日本語名どちらでも）からフォントのファイルを探す。太さは prefer_weight に近いもの"""
    k = family.strip().lower()
    hits = [fc for fc in faces if k == fc.family.lower() or k in {n.lower() for n in fc.names}]
    if not hits:
        return None
    # 同じ太さなら、テンプレートの fonts フォルダのもの（ほかのPCに持って行っても使える）を優先
    return min(hits, key=lambda fc: (abs(fc.weight - prefer_weight), not fc.in_template, fc.user_font,
                                     str(fc.path), fc.index))


def used_families(cfg: dict) -> list[str]:
    fams = [cfg.get("caption", {}).get("font"), cfg.get("chapter_card", {}).get("font")]
    fams += [cfg.get("telop", {}).get(k, {}).get("font") for k, _ in TELOPS]
    out = []
    for f in fams:
        if f and f not in out:
            out.append(f)
    return out


def user_font_files(cfg: dict, faces: list[FontFace]) -> list[Path]:
    """「このユーザーだけ」に入れたフォント（FFmpeg が見つけられないことがある）のファイル"""
    files = []
    for fam in used_families(cfg):
        for fc in faces:
            if fc.user_font and (fam.lower() == fc.family.lower() or fam.lower() in {n.lower() for n in fc.names}):
                if fc.path not in files:
                    files.append(fc.path)
    return files


# ============================================================
#  template.toml の書き出し
# ============================================================
SECTION_NOTES = {
    "video": "動画の大きさ・書き出しの設定",
    "audio": "音量の設定",
    "fonts": "テンプレート専用のフォント",
    "caption": "字幕（画面下）",
    "telop": "テロップ（強調など。AIが大事な所に付ける）",
    "telop.emphasis": "強調テロップ（大事なポイント）",
    "telop.info": "補足テロップ（補足情報）",
    "telop.warning": "注意テロップ",
    "telop.funny": "ツッコミテロップ（面白い所）",
    "chapter_card": "章タイトルのカード（チャプターの頭に出る）",
    "zoom": "ズーム（喋りの強調でグッと寄る）",
    "intro": "オープニング（空なら使わない）",
    "outro": "エンディング（空なら使わない）",
    "logo": "ロゴ（空なら使わない）",
    "bgm": "BGM",
    "se": "効果音",
    "thumbnail": "サムネイル",
    # あとから足した機能の設定（画面で保存し直しても、見出しの説明が残るように）
    "thumbnail.pro": "人物の切り抜きサムネイル",
    "audio_clean": "声を聞き取りやすくする（仕上げの書き出しで、声のトラックにかける）",
    "color": "映像の明るさ・色（仕上げの書き出しで、本編の映像にかける）",
    "stabilize": "手ブレ補正（仕上げの書き出しで、本編の映像にかける）",
    "speedup": "工作の作業シーンの早送り（工作モードで、喋っていない作業の場面）",
    "shorts": "縦型ショートの自動生成",
    "multicam": "2台カメラの自動切り替え",
    "broll": "Bロール（差し込み素材）の自動配置",
    "translate": "英語字幕と多言語対応",
    "speakers": "話者の色分け（実験的な機能）",
    "mv": "MVモード（ビートに合わせたカット）",
    "telop.bubble": "吹き出しテロップ（心の声・ひとこと）",
    "telop.spec": "商品スペックのカード",
    "telop.compare": "比較表",
    "telop.step": "工作の「STEP」カード",
    "telop.place": "屋外の場所カード",
    "progress": "章の進み具合（画面の上の細いバーと、今の章の名前）",
    "opening": "オープニングの自動生成（python graphics.py で作れます）",
    "ending": "エンディングの自動生成",
}
KEY_NOTES = {
    "video.width": "横の画素数", "video.height": "縦の画素数",
    "video.fps": '"auto" = 素材に合わせる / "30" "60" なども可',
    "video.encoder": "auto / h264_nvenc / hevc_nvenc / av1_nvenc / libx264",
    "video.quality": "小さいほど高画質・大きいファイル（18〜23くらい）",
    "audio.loudness": "YouTubeの基準音量（LUFS）", "audio.voice_volume_db": "声の音量（dB）",
    "audio.bgm_volume_db": "BGMの基本音量（dB）", "audio.bgm_duck": "喋っている間にBGMを下げる強さ",
    "audio.se_volume_db": "効果音の音量（dB）",
    "fonts.dir": "ここに置いた .ttf / .otf も使えます",
    "font": "フォントの名前", "size": "文字の大きさ", "bold": "太字にするか（true / false）",
    "color": "文字の色（#RRGGBB）", "outline_color": "縁取りの色", "outline": "縁取りの太さ（0でなし）",
    "shadow": "影の濃さ（0でなし）", "margin_bottom": "画面の下からの距離",
    "max_chars": "字幕1枚の最大文字数", "box_color": "帯（文字の後ろの箱）の色。これがあると帯つきになる",
    "position": "top / middle / top-left / bottom", "animation": "pop / fade / slide",
    "margin_v": "画面の端（上または下）からの距離", "margin_h": "画面の左右の端からの距離",
    "duration": "表示する秒数",
    "zoom.small": "弱いズームの倍率", "zoom.medium": "強いズームの倍率",
    "file": "このフォルダからの相対パス（例: \"intro.mp4\"）",
    "logo.position": "top-left / top-right / bottom-left / bottom-right", "logo.width": "ロゴの幅（画素）",
    "logo.opacity": "不透明度（0〜1）",
    "bgm.dir": "bgm/calm, bgm/upbeat, bgm/tension, bgm/emotional に曲を入れる",
    "se.dir": "pop.wav / ding.wav / whoosh.wav / boing.wav",
    "thumbnail.font_file": "文字に使うフォントファイル（.ttf / .otf / .ttc）",
    "thumbnail.font_index": ".ttc の中の何番目のフォントか（0から数える）",
    "thumbnail.text_color": "文字の色", "thumbnail.outline_color": "文字の縁取り（内側）",
    "thumbnail.outer_color": "文字の縁取り（外側）", "thumbnail.accent_color": "帯の色（「上に帯」のデザイン）",
    "thumbnail.layouts": "bottom=下に大きな文字 / band=上に帯 / side=横に文字",
}
_BARE = re.compile(r"[A-Za-z0-9_-]+")


def _key(k: str) -> str:
    return k if _BARE.fullmatch(k) else _str(k)


def _str(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("数値が正しくありません")
        s = repr(v)
        return s if any(c in s for c in ".eE") else s + ".0"
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, dict) for x in v):
            raise ValueError("表の配列は書き出せません")
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise ValueError(f"書き出せない値です: {v!r}")


def _note(path: str, key: str) -> str:
    return KEY_NOTES.get(f"{path}.{key}") or KEY_NOTES.get(f"{path.split('.')[0]}.{key}") or KEY_NOTES.get(key, "")


def dump_toml(cfg: dict) -> str:
    """設定（dict）を template.toml の文字列にする。値の意味をコメントで添える"""
    lines = ["# ===========================================================",
             "#  チャンネルの見た目テンプレート",
             "#  （画面の「テンプレートを編集」で保存しました。メモ帳で直接書き換えてもかまいません）",
             "#  色は #RRGGBB（例: 白 = #FFFFFF）、文字は \"…\" で囲みます。",
             "# ===========================================================", ""]

    def scalars(d: dict, path: str):
        rows = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
        if not rows:
            return
        body = [f"{_key(k)} = {_value(v)}" for k, v in rows]
        width = min(max(len(b) for b in body) + 2, 44)
        for (k, _v), b in zip(rows, body):
            n = _note(path, k)
            lines.append(f"{b.ljust(width)}# {n}" if n else b)

    def table(d: dict, path: str):
        has_scalar = any(not isinstance(v, dict) for v in d.values())
        if has_scalar or not d:
            note = SECTION_NOTES.get(path, "")
            if note:
                lines.append(f"# ---- {note} ----")
            lines.append(f"[{'.'.join(_key(p) for p in path.split('.'))}]")
            scalars(d, path)
            lines.append("")
        elif SECTION_NOTES.get(path):
            lines.append(f"# ---- {SECTION_NOTES[path]} ----")
        for k, v in d.items():
            if isinstance(v, dict):
                table(v, f"{path}.{k}")

    top = [(k, v) for k, v in cfg.items() if not isinstance(v, dict)]
    if top:
        scalars(dict(top), "")
        lines.append("")
    for k, v in cfg.items():
        if isinstance(v, dict):
            table(v, k)
    return "\n".join(lines).rstrip() + "\n"


def load_cfg(tdir: Path) -> dict:
    return copy.deepcopy(Template(Path(tdir)).cfg)


# ============================================================
#  見本画像
# ============================================================
class MemTemplate(Template):
    """ファイルに保存する前の設定を、Template と同じように扱う"""

    def __init__(self, cfg: dict, folder: Path):   # noqa: D401  (ファイルは読まない)
        self.dir = Path(folder).resolve()
        self.cfg = cfg


def _run(cmd: list[str], cwd: Path | None = None, what: str = "") -> None:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"FFmpeg（{what}）が失敗しました:\n{tail}")


def sample_background(kind: str, w: int, h: int, out: Path) -> Path:
    """サンプルの背景（明るい部屋 / 夜の屋外）を作る。文字の読みやすさを確かめやすいよう、明暗の差をつける"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    if out.exists():
        try:
            with Image.open(out) as im:
                if im.size == (w, h):
                    return out
        except OSError:
            pass
    sw, sh = 480, 270
    y = np.linspace(0, 1, sh)[:, None]
    x = np.linspace(0, 1, sw)[None, :]
    if kind == "dark":
        top, bottom = np.array([18, 24, 48]), np.array([40, 34, 52])
    else:
        top, bottom = np.array([196, 214, 232]), np.array([226, 214, 196])
    base = top[None, None, :] * (1 - y[..., None]) + bottom[None, None, :] * y[..., None]
    base = base + (x[..., None] - 0.5) * 18
    img = Image.fromarray(np.clip(base, 0, 255).astype("uint8")).convert("RGB")
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(7)
    if kind == "dark":
        for _ in range(26):   # 街の明かりのぼけ
            cx, cy, r = rng.uniform(0, sw), rng.uniform(sh * 0.35, sh), rng.uniform(4, 16)
            c = tuple(int(v) for v in rng.choice([[255, 200, 120], [255, 240, 220], [120, 180, 255]]))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    else:
        d.rectangle([0, sh * 0.68, sw, sh], fill=(170, 124, 84))              # 机
        d.rectangle([sw * 0.62, sh * 0.18, sw * 0.9, sh * 0.6], fill=(250, 250, 248))   # 窓
        d.ellipse([sw * 0.12, sh * 0.42, sw * 0.38, sh * 0.78], fill=(60, 120, 200))     # 物
        d.rectangle([sw * 0.42, sh * 0.5, sw * 0.56, sh * 0.72], fill=(230, 80, 60))
    img = img.filter(ImageFilter.GaussianBlur(3)).resize((w, h), Image.BICUBIC)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def extract_frame(video: Path, t: float, out: Path) -> Path:
    """動画の t 秒目の1コマを取り出す（長い素材でも、その場所だけを読む）"""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(Path(video).resolve()),
          "-frames:v", "1", "-update", "1", str(out.resolve())], what="1コマの取り出し")
    if not out.exists():
        raise RuntimeError(f"{t:.1f}秒目のコマを取り出せませんでした（動画より長い時間かもしれません）")
    return out


def _cover(src: Path, w: int, h: int, out: Path) -> None:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        ImageOps.fit(im, (w, h), Image.BICUBIC).save(out)


def _sync_fonts(tpl: Template, extra: list[Path], dst: Path) -> bool:
    """作業フォルダの fonts に、テンプレートのフォントとユーザー用フォントを置く（変わったものだけコピー）"""
    files = []
    if tpl.fonts_dir:
        files += [p for p in sorted(tpl.fonts_dir.iterdir()) if p.suffix.lower() in FONT_EXT and p.is_file()]
    files += [p for p in extra if p.exists()]
    dst.mkdir(parents=True, exist_ok=True)
    names = set()
    for p in files:
        q = dst / p.name
        names.add(p.name)
        st = p.stat()
        if not q.exists() or q.stat().st_size != st.st_size:
            shutil.copy2(p, q)
    for q in dst.iterdir():
        if q.name not in names:
            try:
                q.unlink()
            except OSError:
                pass
    return bool(files)


def render_preview(cfg: dict, tpl_dir: Path, bg: Path | None = None, out_dir: Path | None = None,
                   prefix: str = "preview", bg_kind: str = "light", faces: list[FontFace] | None = None) -> dict:
    """見本画像を作る。戻り値は {パネルのキー: PNGのパス, "grid": 一覧画像のパス}

    cfg: テンプレートの設定 / tpl_dir: fonts などの基準になるテンプレートのフォルダ
    bg: 背景の画像（None ならサンプル）/ bg_kind: サンプルの種類（light / dark）
    """
    from PIL import Image

    from rc_ass import build_ass

    cfg = copy.deepcopy(cfg)
    tpl = MemTemplate(cfg, tpl_dir)
    w = int(cfg.get("video", {}).get("width", 1920))
    h = int(cfg.get("video", {}).get("height", 1080))
    work = tpl.dir / WORK_NAME
    work.mkdir(exist_ok=True)
    out_dir = Path(out_dir or work).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 背景（FFmpeg には作業フォルダからの相対パスで渡す）
    bg_png = work / f"{prefix}_bg.png"
    if bg:
        _cover(Path(bg), w, h, bg_png)
    else:
        _cover(sample_background(bg_kind, w, h, work / f"sample_{bg_kind}.png"), w, h, bg_png)

    # 字幕・テロップ・章カード（場面ごとに2秒ずつ。0.8秒後の、動きが落ち着いたところを撮る）
    caps = [(k * 2.0, k * 2.0 + 1.6, SAMPLE_CAPTIONS[k]) for k in range(5)]
    telops = [(k * 2.0, k * 2.0 + 1.6, SAMPLE_TELOPS[name], name) for k, (name, _) in enumerate(TELOPS)]
    cfg.setdefault("chapter_card", {})["duration"] = 2.5   # 見本では、短くしても撮れるようにする
    chapters = [(8.0, SAMPLE_CHAPTER)]
    (work / f"{prefix}.ass").write_text(build_ass(tpl, w, h, caps, telops, chapters), encoding="utf-8")
    has_fonts = _sync_fonts(tpl, user_font_files(cfg, faces) if faces else [], work / "fonts")
    vf = (f"settb=1/1000,setpts=PTS+0.8/TB,ass={prefix}.ass" + (":fontsdir=fonts" if has_fonts else "")
          + f",scale={PANEL_W}:{PANEL_H}:flags=lanczos")
    for old in work.glob(f"{prefix}_scene_*.png"):
        old.unlink()
    _run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", "1/2", "-t", "10", "-i", bg_png.name,
          "-vf", vf, "-fps_mode", "passthrough", "-frames:v", "5", f"{prefix}_scene_%d.png"], cwd=work,
         what="見本画像")
    result = {}
    for k, (key, _) in enumerate(PANELS[:5], 1):
        src = work / f"{prefix}_scene_{k}.png"
        if not src.exists():
            raise RuntimeError("見本画像を作れませんでした（FFmpeg の出力がありません）")
        dst = out_dir / f"{prefix}_{key}.png"
        shutil.move(str(src), str(dst))
        result[key] = dst

    # サムネイル（rc_thumb と同じ合成）
    import rc_thumb
    with Image.open(bg_png) as frame:
        frame = frame.convert("RGB")
        try:
            font = rc_thumb.find_font(tpl)
        except SystemExit:
            font = None
        for key, layout in (("thumb", "bottom"), ("thumb_band", "band")):
            dst = out_dir / f"{prefix}_{key}.png"
            if font:
                img = rc_thumb.compose(frame, SAMPLE_THUMB, layout, tpl, font)
            else:
                img = Image.new("RGB", (rc_thumb.W, rc_thumb.H), (60, 60, 60))
            img.resize((PANEL_W, PANEL_H), Image.LANCZOS).save(dst)
            result[key] = dst
    result["grid"] = make_grid(result, out_dir / f"{prefix}_grid.png", cfg=cfg)
    return result


def palette(cfg: dict) -> list[tuple[str, str]]:
    """使っている主な色（名前, #RRGGBB）"""
    out = []
    cap, tel = cfg.get("caption", {}), cfg.get("telop", {})
    out.append(("字幕", cap.get("color", "#FFFFFF")))
    for k, label in TELOPS:
        c = tel.get(k, {})
        out.append((label, c.get("color", "#FFFFFF")))
        out.append((label + ("の帯" if "box_color" in c else "の縁"), c.get("box_color", c.get("outline_color", "#000000"))))
    out.append(("章カード", cfg.get("chapter_card", {}).get("box_color", "#1A1A1A")))
    out.append(("サムネ", cfg.get("thumbnail", {}).get("text_color", "#FFE14D")))
    return [(n, norm_color(c, "#000000")) for n, c in out]


def make_grid(panels: dict, out: Path, cols: int = 4, cell_w: int = 480, cfg: dict | None = None) -> Path:
    """見本画像を並べた一覧（名前つき）。空いたところに使っている色を並べる"""
    from PIL import Image, ImageDraw, ImageFont
    cell_h = cell_w * 9 // 16
    label_h = 34
    rows = (len(PANELS) + cols - 1) // cols
    grid = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (32, 32, 36))
    d = ImageDraw.Draw(grid)
    font = None
    for p in _label_font_candidates():
        try:
            font = ImageFont.truetype(str(p[0]), 20, index=p[1])
            break
        except OSError:
            continue
    for i, (key, label) in enumerate(PANELS):
        if key not in panels:
            continue
        x, y = (i % cols) * cell_w, (i // cols) * (cell_h + label_h)
        with Image.open(panels[key]) as im:
            grid.paste(im.convert("RGB").resize((cell_w, cell_h), Image.LANCZOS), (x, y + label_h))
        d.text((x + 10, y + 6), label, fill=(235, 235, 235), font=font)
    if cfg and len(PANELS) < rows * cols:
        i = len(PANELS)
        x, y = (i % cols) * cell_w, (i // cols) * (cell_h + label_h)
        d.text((x + 10, y + 6), "使っている色", fill=(235, 235, 235), font=font)
        for j, (name, col) in enumerate(palette(cfg)):
            cx, cy = x + 12 + (j // 6) * (cell_w // 2), y + label_h + 8 + (j % 6) * 38
            rgb = tuple(int(col[k:k + 2], 16) for k in (1, 3, 5))
            d.rectangle([cx, cy, cx + 30, cy + 30], fill=rgb, outline=(120, 120, 120))
            d.text((cx + 40, cy + 4), f"{name} {col}", fill=(220, 220, 220), font=font)
    grid.save(out)
    return out


def _label_font_candidates() -> list[tuple[Path, int]]:
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    c = [(win / "BIZ-UDGothicR.ttc", 0), (win / "YuGothM.ttc", 0), (win / "meiryo.ttc", 0),
         (Path("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"), 0),
         (Path("/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf"), 0)]
    return [x for x in c if x[0].exists()]


# ============================================================
#  AI のデザイン案
# ============================================================
_COLOR = {"type": "string", "description": "#RRGGBB 形式の色"}


def _telop_schema(fonts) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["font", "size", "color", "look", "accent_color", "outline", "position", "animation"],
            "properties": {
                "font": fonts, "size": {"type": "integer"}, "color": _COLOR,
                "look": {"type": "string", "enum": ["outline", "box"],
                         "description": "outline=文字に縁取り / box=文字の後ろに帯（箱）"},
                "accent_color": {**_COLOR, "description": "look=outline なら縁取りの色、box なら帯の色"},
                "outline": {"type": "integer", "description": "縁取りの太さ（look=outline のとき。4〜12）"},
                "position": {"type": "string", "enum": [p for p, _ in POSITIONS]},
                "animation": {"type": "string", "enum": [a for a, _ in ANIMATIONS]},
            }}


def design_schema(font_names: list[str]) -> dict:
    fonts = {"type": "string", "enum": font_names} if font_names else {"type": "string"}
    obj = {"type": "object", "additionalProperties": False}
    proposal = {**obj, "required": ["name", "concept", "caption", "telops", "chapter_card", "thumbnail"],
                "properties": {
                    "name": {"type": "string", "description": "案の名前（10字くらい）"},
                    "concept": {"type": "string", "description": "どんな雰囲気か、やさしい言葉で1〜2文"},
                    "caption": {**obj, "required": ["font", "size", "color", "outline_color", "outline"],
                                "properties": {"font": fonts, "size": {"type": "integer"}, "color": _COLOR,
                                               "outline_color": _COLOR, "outline": {"type": "integer"}}},
                    "telops": {**obj, "required": [k for k, _ in TELOPS],
                               "properties": {k: _telop_schema(fonts) for k, _ in TELOPS}},
                    "chapter_card": {**obj, "required": ["font", "size", "color", "box_color"],
                                     "properties": {"font": fonts, "size": {"type": "integer"}, "color": _COLOR,
                                                    "box_color": _COLOR}},
                    "thumbnail": {**obj, "required": ["font", "text_color", "outline_color", "outer_color",
                                                      "accent_color"],
                                  "properties": {"font": fonts, "text_color": _COLOR, "outline_color": _COLOR,
                                                 "outer_color": _COLOR, "accent_color": _COLOR}},
                }}
    return {**obj, "required": ["proposals"],
            "properties": {"proposals": {"type": "array", "items": proposal,
                                         "description": "デザイン案。ちょうど3つ"}}}


def look_summary(cfg: dict) -> dict:
    """AI に見せる今のテンプレート（見た目に関係する所だけ）"""
    keys = ("font", "size", "bold", "color", "outline_color", "outline", "box_color", "position", "animation")
    out = {"caption": {k: v for k, v in cfg.get("caption", {}).items() if k in keys}, "telops": {}}
    for k, _ in TELOPS:
        out["telops"][k] = {kk: v for kk, v in cfg.get("telop", {}).get(k, {}).items() if kk in keys}
    out["chapter_card"] = {k: v for k, v in cfg.get("chapter_card", {}).items() if k in keys}
    out["thumbnail"] = {k: v for k, v in cfg.get("thumbnail", {}).items() if k.endswith("color")}
    return out


def design_instructions() -> str:
    return """あなたは日本のYouTubeチャンネルの見た目を考えるデザイナーです。
チャンネルの雰囲気の説明から、動画の字幕・テロップ・章タイトルのカード・サムネイルの文字の「配色・フォント・見せ方」の組み合わせを、はっきり違う3つの案にしてください。

## 前提
- 動画は 1920x1080。字幕は画面の下、テロップは上や左上に出ます。背景は明るい部屋のことも、暗い屋外のこともあります
- テロップは4種類: emphasis=大事なポイント（いちばん目立たせる）、info=補足情報（落ち着いた見た目。帯がおすすめ）、warning=注意（赤系など警告とわかる色）、funny=ツッコミ・面白い所（遊びのある色）
- フォントは「使えるフォント」の中から、名前をそのまま選んでください。1つの案で使うフォントは2種類までにすると、まとまりが出ます
- 色は #RRGGBB で書きます

## 読みやすさの決まり
- どんな背景でも読めるように、文字の色と縁取り（または帯）の色は、明るさをはっきり変える
- look=outline のときは outline を 4〜12 にする。look=box のときは帯の色と文字の色の差を大きくする
- 大きさの目安: 字幕 54〜80、強調 84〜120、補足 44〜64、注意・ツッコミ 72〜110、章カード 64〜110

## 3つの案
- 説明された雰囲気に合わせつつ、3案は配色・フォント・見せ方（縁取りか帯か、動き）がはっきり違うようにする
- name は短い日本語の名前、concept はどんな見た目かをやさしい言葉で1〜2文

出力はJSONのみです。"""


def design_user_message(mood: str, base_cfg: dict, font_names: list[str], genre: str = "") -> str:
    fonts = "\n".join(f"- {f}" for f in font_names) if font_names else "（一覧なし。Windows 標準の日本語フォントから選んでください）"
    g = f"\n## 主な動画のジャンル\n{genre}\n" if genre else ""
    return (f"## チャンネルの雰囲気\n{mood.strip() or 'おまかせ'}\n{g}\n"
            f"## 今のテンプレート（参考）\n```json\n{json.dumps(look_summary(base_cfg), ensure_ascii=False, indent=1)}\n```\n\n"
            f"## 使えるフォント\n{fonts}\n")


def propose_api(mood: str, base_cfg: dict, font_names: list[str], effort: str = "medium",
                genre: str = "") -> tuple[list[dict], dict]:
    import rc_llm
    anthropic, client = rc_llm._client()
    usage = {"in": 0, "out": 0}
    print("  Claudeでデザイン案を作成中…", flush=True)
    try:
        res = rc_llm._call(client, anthropic, design_instructions(), design_user_message(mood, base_cfg, font_names, genre),
                           design_schema(font_names), effort, usage)
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API: {e.status_code} {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"Claude APIに接続できません: {e}") from e
    return list(res.get("proposals", []))[:3], usage


def write_manual_prompt(mood: str, base_cfg: dict, font_names: list[str], path: Path, genre: str = "") -> None:
    schema = json.dumps(design_schema(font_names), ensure_ascii=False, indent=1)
    path.write_text(design_instructions() + "\n\n" + design_user_message(mood, base_cfg, font_names, genre)
                    + f"\n## 出力するJSONの形式（JSON Schema）\n```json\n{schema}\n```\n", encoding="utf-8")


def parse_answer(text: str) -> list[dict]:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("返答にJSONが見つかりません")
    data = json.loads(text[a:b + 1])
    props = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(props, list) or not props:
        raise ValueError("返答に proposals（デザイン案）がありません")
    return props[:3]


_HEX = re.compile(r"#?([0-9A-Fa-f]{6})")


def norm_color(v, default: str) -> str:
    m = _HEX.fullmatch(str(v).strip()) if v is not None else None
    return f"#{m.group(1).upper()}" if m else default


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def _font(v, allowed: set[str] | None, default: str) -> str:
    if not isinstance(v, str) or not v.strip():
        return default
    v = v.strip()
    if allowed is None:
        return v
    low = {a.lower(): a for a in allowed}
    return low.get(v.lower(), default)


def apply_proposal(base_cfg: dict, p: dict, font_names: list[str] | None = None,
                   faces: list[FontFace] | None = None, tpl_dir: Path | None = None) -> dict:
    """AI の案を、元のテンプレートに当てはめた新しい設定にする（おかしな値は元の値のまま）"""
    cfg = copy.deepcopy(base_cfg)
    allowed = set(font_names) if font_names else None
    p = p if isinstance(p, dict) else {}

    cap = cfg.setdefault("caption", {})
    pc = p.get("caption") if isinstance(p.get("caption"), dict) else {}
    cap["font"] = _font(pc.get("font"), allowed, cap.get("font", "BIZ UDPGothic"))
    cap["size"] = _clamp(pc.get("size"), 40, 110, cap.get("size", 66))
    cap["color"] = norm_color(pc.get("color"), cap.get("color", "#FFFFFF"))
    cap["outline_color"] = norm_color(pc.get("outline_color"), cap.get("outline_color", "#1A1A1A"))
    cap["outline"] = _clamp(pc.get("outline"), 0, 16, cap.get("outline", 6))

    tel = cfg.setdefault("telop", {})
    pt = p.get("telops") if isinstance(p.get("telops"), dict) else {}
    for k, _ in TELOPS:
        c = tel.setdefault(k, {})
        q = pt.get(k) if isinstance(pt.get(k), dict) else {}
        c["font"] = _font(q.get("font"), allowed, c.get("font", cap["font"]))
        c["size"] = _clamp(q.get("size"), 36, 150, c.get("size", 80))
        c["color"] = norm_color(q.get("color"), c.get("color", "#FFFFFF"))
        look = q.get("look") if q.get("look") in ("outline", "box") else ("box" if "box_color" in c else "outline")
        accent = norm_color(q.get("accent_color"), c.get("box_color" if look == "box" else "outline_color",
                                                         "#1A1A1A"))
        if look == "box":
            c["box_color"] = accent
        else:
            c.pop("box_color", None)
            c["outline_color"] = accent
            c["outline"] = _clamp(q.get("outline"), 2, 16, c.get("outline", 8))
        if q.get("position") in {x for x, _ in POSITIONS}:
            c["position"] = q["position"]
        if q.get("animation") in {x for x, _ in ANIMATIONS}:
            c["animation"] = q["animation"]

    ch = cfg.setdefault("chapter_card", {})
    pch = p.get("chapter_card") if isinstance(p.get("chapter_card"), dict) else {}
    ch["font"] = _font(pch.get("font"), allowed, ch.get("font", cap["font"]))
    ch["size"] = _clamp(pch.get("size"), 48, 140, ch.get("size", 88))
    ch["color"] = norm_color(pch.get("color"), ch.get("color", "#FFFFFF"))
    ch["box_color"] = norm_color(pch.get("box_color"), ch.get("box_color", "#1A1A1A"))

    th = cfg.setdefault("thumbnail", {})
    pth = p.get("thumbnail") if isinstance(p.get("thumbnail"), dict) else {}
    for k, d in (("text_color", "#FFE14D"), ("outline_color", "#111111"), ("outer_color", "#FFFFFF"),
                 ("accent_color", "#2B6CB0")):
        th[k] = norm_color(pth.get(k), th.get(k, d))
    fam = _font(pth.get("font"), allowed, "")
    if fam and faces:
        set_thumbnail_font(cfg, fam, faces, tpl_dir)
    return cfg


def set_thumbnail_font(cfg: dict, family: str, faces: list[FontFace], tpl_dir: Path | None = None) -> bool:
    """サムネイルの文字のフォントを、名前からファイルを探して設定する（太字を優先）"""
    fc = find_face(faces, family, 800)
    if not fc:
        return False
    th = cfg.setdefault("thumbnail", {})
    p = fc.path.resolve()
    rel = None
    if tpl_dir:
        try:
            rel = p.relative_to(Path(tpl_dir).resolve()).as_posix()
        except ValueError:
            rel = None
    th["font_file"] = rel or p.as_posix()
    th["font_index"] = fc.index
    return True


def thumbnail_family(cfg: dict, tpl_dir: Path) -> str | None:
    """今のサムネイル用フォントファイルの名前（ファイルがなければ None）"""
    ff = cfg.get("thumbnail", {}).get("font_file", "")
    if not ff:
        return None
    p = Path(ff)
    if not p.is_absolute():
        p = Path(tpl_dir) / p
    if not p.exists():
        return None
    idx = int(cfg.get("thumbnail", {}).get("font_index", 0))
    faces = read_font_file(p)
    fc = next((f for f in faces if f.index == idx), faces[0] if faces else None)
    return fc.family if fc else None


# AI を使わないときの案（フォントは今のテンプレートのまま、色と見せ方だけ変える）
PRESETS = [
    {"keywords": ["落ち着", "シンプル", "大人", "上品", "calm", "解説"],
     "name": "落ち着いたネイビー", "concept": "白い文字に濃い紺の縁取り。補足は紺の帯で、目にやさしい見た目です。",
     "caption": {"color": "#FFFFFF", "outline_color": "#1C2B4A", "outline": 6},
     "telops": {"emphasis": {"color": "#FFFFFF", "look": "outline", "accent_color": "#1C2B4A", "outline": 10,
                             "animation": "fade"},
                "info": {"color": "#FFFFFF", "look": "box", "accent_color": "#34507A", "animation": "slide"},
                "warning": {"color": "#FFFFFF", "look": "outline", "accent_color": "#B83A3A", "outline": 9},
                "funny": {"color": "#FFF4D6", "look": "outline", "accent_color": "#6B4E8E", "outline": 9}},
     "chapter_card": {"color": "#FFFFFF", "box_color": "#1C2B4A"},
     "thumbnail": {"text_color": "#FFFFFF", "outline_color": "#1C2B4A", "outer_color": "#C9D6EA",
                   "accent_color": "#1C2B4A"}},
    {"keywords": ["ポップ", "明る", "元気", "かわい", "楽し", "pop"],
     "name": "ポップなビタミン", "concept": "黄色とピンクの元気な配色。強調はポンッと大きく飛び出します。",
     "caption": {"color": "#FFFFFF", "outline_color": "#E0457B", "outline": 7},
     "telops": {"emphasis": {"color": "#FFE14D", "look": "outline", "accent_color": "#E0457B", "outline": 11,
                             "animation": "pop"},
                "info": {"color": "#FFFFFF", "look": "box", "accent_color": "#FF8A3D", "animation": "slide"},
                "warning": {"color": "#FFFFFF", "look": "outline", "accent_color": "#E53E3E", "outline": 10},
                "funny": {"color": "#FFFFFF", "look": "outline", "accent_color": "#38B2AC", "outline": 10}},
     "chapter_card": {"color": "#FFFFFF", "box_color": "#E0457B"},
     "thumbnail": {"text_color": "#FFE14D", "outline_color": "#E0457B", "outer_color": "#FFFFFF",
                   "accent_color": "#FF8A3D"}},
    {"keywords": ["ガジェット", "テック", "クール", "かっこ", "メカ", "工具", "レビュー"],
     "name": "ガジェット系ネオン", "concept": "黒に近い帯と水色のアクセント。機械やガジェットが映える、クールな見た目です。",
     "caption": {"color": "#FFFFFF", "outline_color": "#0B0F17", "outline": 6},
     "telops": {"emphasis": {"color": "#5CE1FF", "look": "outline", "accent_color": "#0B0F17", "outline": 10,
                             "animation": "slide"},
                "info": {"color": "#5CE1FF", "look": "box", "accent_color": "#11161F", "animation": "slide"},
                "warning": {"color": "#FFD23F", "look": "box", "accent_color": "#1A1A1A", "animation": "pop"},
                "funny": {"color": "#FFFFFF", "look": "outline", "accent_color": "#7B3FE4", "outline": 9}},
     "chapter_card": {"color": "#5CE1FF", "box_color": "#0B0F17"},
     "thumbnail": {"text_color": "#5CE1FF", "outline_color": "#0B0F17", "outer_color": "#FFFFFF",
                   "accent_color": "#11161F"}},
    {"keywords": ["ナチュラル", "自然", "屋外", "アウトドア", "キャンプ", "ほっこり", "工作", "木"],
     "name": "ナチュラルグリーン", "concept": "生成り色の文字と深い緑。屋外や手作りの雰囲気になじむ見た目です。",
     "caption": {"color": "#FFFDF5", "outline_color": "#2F3E2C", "outline": 6},
     "telops": {"emphasis": {"color": "#FFF1B8", "look": "outline", "accent_color": "#2F5D3A", "outline": 10,
                             "animation": "pop"},
                "info": {"color": "#FFFDF5", "look": "box", "accent_color": "#5B7F4A", "animation": "fade"},
                "warning": {"color": "#FFFFFF", "look": "outline", "accent_color": "#B5532F", "outline": 9},
                "funny": {"color": "#FFFFFF", "look": "outline", "accent_color": "#C27C2C", "outline": 9}},
     "chapter_card": {"color": "#FFFDF5", "box_color": "#2F5D3A"},
     "thumbnail": {"text_color": "#FFF1B8", "outline_color": "#2F3E2C", "outer_color": "#FFFFFF",
                   "accent_color": "#2F5D3A"}},
    {"keywords": ["音楽", "MV", "エモ", "しっとり", "夜", "おしゃれ"],
     "name": "夜のモノトーン", "concept": "白と黒だけの、写真のような静かな見た目。音楽や作品が主役になります。",
     "caption": {"color": "#FFFFFF", "outline_color": "#000000", "outline": 4},
     "telops": {"emphasis": {"color": "#FFFFFF", "look": "box", "accent_color": "#000000", "animation": "fade"},
                "info": {"color": "#000000", "look": "box", "accent_color": "#F2F2F2", "animation": "fade"},
                "warning": {"color": "#FFFFFF", "look": "box", "accent_color": "#9B1C1C", "animation": "fade"},
                "funny": {"color": "#FFFFFF", "look": "outline", "accent_color": "#333333", "outline": 8,
                          "animation": "fade"}},
     "chapter_card": {"color": "#FFFFFF", "box_color": "#000000"},
     "thumbnail": {"text_color": "#FFFFFF", "outline_color": "#000000", "outer_color": "#555555",
                   "accent_color": "#000000"}},
]


def preset_proposals(mood: str) -> list[dict]:
    """AIなし：雰囲気の言葉に合う用意済みの案を3つ（足りない分は順番に）"""
    m = mood.lower()
    hit = [p for p in PRESETS if any(k.lower() in m for k in p["keywords"])]
    rest = [p for p in PRESETS if p not in hit]
    return [{k: v for k, v in p.items() if k != "keywords"} for p in (hit + rest)[:3]]


# ============================================================
#  保存
# ============================================================
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def check_name(name: str) -> str | None:
    """テンプレート名（フォルダ名）として使えないときは、その理由を返す"""
    n = name.strip()
    if not n:
        return "名前を入れてください。"
    if _BAD.search(n):
        return '名前に \\ / : * ? " < > | は使えません。'
    if n.startswith(".") or n.endswith(".") or n.startswith("_"):
        return "名前の最初に「.」「_」、最後に「.」は使えません。"
    if n.lower() in _RESERVED:
        return "その名前はWindowsで使えません。別の名前にしてください。"
    if len(n) > 60:
        return "名前が長すぎます（60文字まで）。"
    return None


def safe_name(name: str) -> str:
    n = _BAD.sub("", name).strip().strip(".").lstrip("_")
    return n[:40] or "新しいテンプレート"


COPY_IGNORE = shutil.ignore_patterns(WORK_NAME, "_preview_work", "template_preview.mp4", "preview_*.png",
                                     "template.toml.bak", "design_preview.png")


def save_template(cfg: dict, base_dir: Path, dest_dir: Path, faces: list[FontFace] | None = None) -> Path:
    """設定を dest_dir/template.toml に保存する。

    dest_dir が新しいフォルダなら、base_dir の BGM・効果音・フォント・ロゴなどを丸ごと複製する。
    「このユーザーだけ」に入れたフォントは、FFmpeg が見つけられるよう fonts フォルダにコピーする。
    上書きのときは、前の template.toml を template.toml.bak に残す。
    """
    base_dir, dest_dir = Path(base_dir).resolve(), Path(dest_dir).resolve()
    cfg = copy.deepcopy(cfg)
    text = dump_toml(cfg)
    import tomllib
    tomllib.loads(text)    # 書き出したものが読めることを先に確かめる
    if not dest_dir.exists():
        shutil.copytree(base_dir, dest_dir, ignore=COPY_IGNORE)
        th = cfg.get("thumbnail", {})
        ff = th.get("font_file", "")
        if ff and not Path(ff).is_absolute() and not (dest_dir / ff).exists() and (base_dir / ff).exists():
            th["font_file"] = (base_dir / ff).resolve().as_posix()
    elif (dest_dir / "template.toml").exists():
        shutil.copy2(dest_dir / "template.toml", dest_dir / "template.toml.bak")
    dest_dir.mkdir(parents=True, exist_ok=True)

    if faces:
        fonts = dest_dir / cfg.get("fonts", {}).get("dir", "fonts")
        files = user_font_files(cfg, faces)
        th = cfg.get("thumbnail", {})
        if th.get("font_file"):
            tp = Path(th["font_file"])
            if tp.is_absolute() and any(fc.user_font and fc.path.resolve() == tp.resolve() for fc in faces):
                files.append(tp)
                th["font_file"] = f"{fonts.relative_to(dest_dir).as_posix()}/{tp.name}"
        for f in files:
            fonts.mkdir(parents=True, exist_ok=True)
            if not (fonts / f.name).exists():
                shutil.copy2(f, fonts / f.name)
        text = dump_toml(cfg)
    (dest_dir / "template.toml").write_text(text, encoding="utf-8")
    Template(dest_dir)     # 読み込めることを確かめる
    return dest_dir


def list_templates(root: Path = TEMPLATES_DIR) -> list[Path]:
    return sorted(d.parent for d in Path(root).glob("*/template.toml"))
