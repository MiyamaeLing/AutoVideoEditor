"""見た目テンプレート（template.toml）の読み込み。"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

MOODS = ["calm", "upbeat", "tension", "emotional"]
SE_KINDS = ["pop", "ding", "whoosh", "boing"]
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


class Template:
    def __init__(self, folder: Path):
        self.dir = folder.resolve()
        f = self.dir / "template.toml"
        if not f.exists():
            sys.exit(f"[エラー] テンプレートが見つかりません: {f}")
        # メモ帳で「UTF-8 (BOM 付き)」で保存されても読めるように、BOM を取り除いてから読む
        try:
            text = f.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            sys.exit(f"[エラー] テンプレートの文字コードが UTF-8 ではありません: {f}\n"
                     "  メモ帳の「名前を付けて保存」で、文字コードを「UTF-8」にして保存し直してください。")
        try:
            self.cfg = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            sys.exit(f"[エラー] テンプレートの書き方に誤りがあります: {f}\n  {e}\n"
                     "  直前に書き換えた所を確認してください（文字は \"…\" で囲みます）。")

    def __getitem__(self, k):
        return self.cfg[k]

    def get(self, k, default=None):
        return self.cfg.get(k, default)

    def path(self, rel: str) -> Path | None:
        if not rel:
            return None
        p = (self.dir / rel).resolve()
        if not p.exists():
            print(f"  [注意] テンプレートのファイルが見つかりません: {p}")
            return None
        return p

    @property
    def fonts_dir(self) -> Path | None:
        p = self.dir / self.cfg.get("fonts", {}).get("dir", "fonts")
        return p if p.is_dir() else None

    def bgm_files(self) -> dict[str, list[Path]]:
        root = self.dir / self.cfg.get("bgm", {}).get("dir", "bgm")
        out = {}
        for m in MOODS:
            d = root / m
            out[m] = sorted(p for p in d.glob("*") if p.suffix.lower() in AUDIO_EXT) if d.is_dir() else []
        return out

    def se_file(self, kind: str) -> Path | None:
        root = self.dir / self.cfg.get("se", {}).get("dir", "se")
        for ext in (".wav", ".mp3", ".m4a", ".ogg", ".flac"):
            p = root / f"{kind}{ext}"
            if p.exists():
                return p
        return None


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB → ASS形式 &HAABBGGRR"""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()
