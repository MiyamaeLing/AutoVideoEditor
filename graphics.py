"""オープニング・エンディングの動画と、テロップの種類の見本をテンプレートの設定から作る。

使い方の例:
  python graphics.py                                   # templates/default にオープニングとエンディングを作る
  python graphics.py --template templates/新デザイン     # 別のテンプレートで作る
  python graphics.py --only intro                      # オープニングだけ作る
  python graphics.py --samples                         # テロップの種類と章の進み具合の見本（静止画）を作る

できたファイルは、テンプレートのフォルダの generated フォルダに入ります。
template.toml の [opening] / [ending] で enabled = true にすると、仕上げ（finish.py）で自動で使われます。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401
import rc_graphics
from rc_media import require_ffmpeg
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def make_clips(tpl: Template, only: str | None) -> None:
    gen = tpl.dir / rc_graphics.GEN_DIR
    for kind in ("intro", "outro"):
        if only and only != kind:
            continue
        try:
            p = rc_graphics.ensure(tpl, kind, force=True)
        except RuntimeError as e:
            sys.exit(f"[エラー] {e}")
        if kind == "intro":
            rc_graphics.still(p, 1.4, gen / "intro_preview.png")
            print(f"  オープニング: {p}")
        else:
            rc_graphics.still(p, 3.0, gen / "outro_preview.png")
            print(f"  エンディング: {p}")
            guide = p.with_name(p.stem + "_終了画面の枠.json")
            if guide.exists():
                print(f"  終了画面の枠の位置: {guide}")
    print("■ できあがり")
    print("  使い方: template.toml の [opening] / [ending] で enabled = true にすると、仕上げで自動で使われます")
    print(f"  （または [intro] / [outro] の file に \"{rc_graphics.GEN_DIR}/intro.mp4\" などと書いても使えます）")
    print("  エンディングは YouTube Studio の「終了画面」で、動画とチャンネル登録の要素を枠に合わせて置いてください")


def make_samples(tpl: Template) -> None:
    """テロップの種類と章の進み具合のバーの見本（16秒の動画と静止画5枚）"""
    from rc_ass import build_ass
    import rc_render
    w, h = tpl["video"]["width"], tpl["video"]["height"]
    gen = tpl.dir / rc_graphics.GEN_DIR
    gen.mkdir(exist_ok=True)
    work = gen / "_work_samples"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    d = 16
    rc_render.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   f"gradients=s={w}x{h}:d={d}:speed=0.01:c0=0x556677:c1=0x8899AA", "-t", str(d), "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "bg.mp4"], cwd=work, what="背景の作成")
    caps = [(0.3, 3.8, "これは字幕のサンプルです"), (8.0, 11.8, "ここで穴をあけていきます")]
    chapters = [(0.0, "スペック"), (4.0, "比べてみた"), (8.0, "作ってみた"), (12.0, "感想")]
    ass = build_ass(tpl, w, h, caps, rc_graphics.SAMPLE_TELOPS, [], progress=(chapters, 0.0, float(d)),
                    progress_force=True)
    (work / "subs.ass").write_text(ass, encoding="utf-8")
    fontsdir = ""
    if tpl.fonts_dir:
        shutil.copytree(tpl.fonts_dir, work / "fonts")
        fontsdir = ":fontsdir=fonts"
    out = gen / "telop_samples.mp4"
    rc_render.run(["ffmpeg", "-y", "-v", "error", "-i", "bg.mp4", "-vf", f"ass=subs.ass{fontsdir}", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", str(out.resolve())], cwd=work, what="見本の書き出し")
    for i, t in enumerate((2.0, 6.0, 10.0, 14.0), 1):
        rc_graphics.still(out, t, gen / f"telop_sample_{i}.png")
    shutil.rmtree(work, ignore_errors=True)
    print(f"  テロップの見本: {out}")
    print(f"  静止画: {gen / 'telop_sample_1.png'} など4枚")


def main():
    ap = argparse.ArgumentParser(description="オープニング・エンディング・テロップの見本を作る")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--only", choices=["intro", "outro"], help="intro=オープニングだけ / outro=エンディングだけ")
    ap.add_argument("--samples", action="store_true", help="テロップの種類と章の進み具合の見本を作る")
    args = ap.parse_args()
    require_ffmpeg()
    tpl = Template(args.template)
    print(f"■ テンプレート: {tpl.dir.name}")
    if args.samples:
        make_samples(tpl)
    else:
        make_clips(tpl, args.only)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"[エラー] {e}")
