"""人物の切り抜きサムネイルの準備と、お試し。

使い方の例:
  python thumbspro.py --setup                                   # 切り抜き用のデータを先にダウンロードして、使えるか確かめる
  python thumbspro.py --try "D:\\写真\\自撮り.jpg" --text "軽すぎ！"   # 好きな画像で人物デザインを試す（背景5種類）

動画のサムネイル案（thumbs.py / 書き出しのあと）では、テンプレートの [thumbnail.pro] の設定で自動的に使われます。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401  (Windowsで黒い画面を出さない)
import rc_thumbspro as P
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def setup(tpl: Template) -> int:
    print("■ 人物の切り抜きの準備")
    if not P.available():
        print("  [エラー] OpenCV（opencv-python-headless）が入っていません。setup.bat をもう一度実行してください。")
        return 1
    print("  OpenCV: 使えます（顔の検出・簡易な切り抜きはこれだけで動きます）")
    cfg = P.settings(tpl)
    if str(cfg.get("model", "auto")) == "off":
        print("  テンプレートで model = \"off\" なので、顔の位置から推定する簡易な切り抜きを使います。")
        return 0
    m = P.find_model(cfg, allow_download=False) or P.download_model(force=True)
    if m:
        print(f"  切り抜き用のデータ: {m}")
        print("  準備ができました。サムネイル案を作ると、人物の切り抜きデザインも作られます。")
    else:
        print("  切り抜き用のデータは使えませんが、顔の位置から推定する簡易な切り抜きで作ります。")
        print(f"  （手動で入れる場合は {P.MODEL_NAME} を {P.model_dir()} に置いてください）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="人物の切り抜きサムネイルの準備・お試し")
    ap.add_argument("--setup", action="store_true", help="切り抜き用のデータをダウンロードして、使えるか確かめる")
    ap.add_argument("--try", dest="image", type=Path, help="この画像で人物デザインを試す")
    ap.add_argument("--text", default="ここに\\n文字", help="お試しの文字（2行にするときは \\n で区切る）")
    ap.add_argument("--out", type=Path, help="お試しの保存先（省略時は画像と同じ場所の「○○_人物デザイン」フォルダ）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--no-download", action="store_true", help="お試しのときにダウンロードしない")
    args = ap.parse_args()
    tpl = Template(args.template)
    if args.setup:
        sys.exit(setup(tpl))
    if args.image:
        if not args.image.exists():
            sys.exit(f"[エラー] 画像が見つかりません: {args.image}")
        out = args.out or args.image.parent / f"{args.image.stem}_人物デザイン"
        print("■ 人物デザインのお試し")
        outs = P.try_image(args.image, tpl, args.text, out, allow_download=not args.no_download)
        print(f"  {len(outs)}枚作りました: {out}")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
