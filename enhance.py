"""声と映像の補正の補助ツール。

使い方の例:
  python enhance.py check --template templates/default --mode outdoor   # どの補正が使われるかを表示
  python enhance.py compare "D:\\撮影\\素材.mp4"                           # 色の補正の前後を並べた画像を作る
  python enhance.py cam2 "D:\\撮影\\素材_roughcut"                         # 2台目カメラの色を本編に寄せる LUT を作る

完成動画への補正（声・色・手ブレ）は、finish.py の書き出しで自動でかかります。
切り替えはテンプレートの [audio_clean] [color] [stabilize]、または finish.py の --audio-clean / --color / --stabilize。
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

import rc_win  # noqa: F401
import rc_enhance
from rc_media import require_ffmpeg
from rc_template import Template

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def _overrides(args) -> dict:
    return {k: v == "on" for k, v in (("audio_clean", args.audio_clean), ("color", args.color),
                                     ("stabilize", args.stabilize)) if v != "auto"}


def cmd_check(args) -> None:
    tpl = Template(args.template)
    st = rc_enhance.resolve(tpl, args.mode, _overrides(args))
    have = rc_enhance.available_filters()
    print(f"■ 補正の設定（テンプレート: {tpl.dir.name} / 撮影ジャンル: {args.mode}）")
    print(f"  使う補正: {st.describe()}")
    if st.audio:
        print(f"  声のフィルター: {rc_enhance.voice_chain(st.audio)}")
        print(f"  ノイズ除去による音の遅れ: {rc_enhance.measure_delay(rc_enhance.voice_chain(st.audio))} サンプル"
              "（書き出しでは自動で取り除きます）")
    for n in st.notes:
        print(f"  [注意] {n}")
    print("  FFmpeg の機能: " + " / ".join(f"{n} {'あり' if n in have else 'なし'}"
                                       for n in ("afftdn", "deesser", "lut3d", "vidstabdetect", "deshake")))


def cmd_compare(args) -> None:
    tpl = Template(args.template)
    ov = _overrides(args)
    in_export = rc_enhance.resolve(tpl, args.mode, ov).color   # 完成動画の書き出しで色の補正がかかるか
    ov.setdefault("color", True)
    st = rc_enhance.resolve(tpl, args.mode, ov)
    if not st.color:
        sys.exit("[エラー] 色の補正が使われない設定です（--color off になっていないか確認してください）。")
    video = Path(args.video).resolve()
    out = Path(args.out).resolve() if args.out else video.parent / f"{video.stem}_補正の比較.jpg"
    rc_enhance.compare_image(video, st, out, args.time)
    print(f"■ 比較画像を作りました（左: そのまま / 右: 補正後）: {out}")
    if not in_export:
        print("  [お知らせ] 今の設定では、完成動画に色の補正はかかりません（これは見本です）。"
              "使うときは「明るさ・色」を「使う」にするか、--color on を付けてください。")


def cmd_cam2(args) -> None:
    print("■ 2台目カメラの色を本編に寄せる LUT を作っています")
    dst = rc_enhance.match_cam2(args.out_dir, args.strength)
    print(f"■ できました: {dst}")
    print("  Premiere Pro での使い方: V2（2台目）のクリップを選ぶ → Lumetri カラー → 基本補正 →")
    print("  「入力LUT」の「参照…」でこのファイルを選ぶ（調整レイヤーに当てても使えます）")


def main():
    require_ffmpeg()
    ap = argparse.ArgumentParser(description="声と映像の補正の補助ツール")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
        p.add_argument("--mode", choices=["talk", "craft", "outdoor"], default="talk", help="撮影ジャンル")
        for opt in ("--audio-clean", "--color", "--stabilize"):
            p.add_argument(opt, choices=["auto", "on", "off"], default="auto")

    p = sub.add_parser("check", help="どの補正が使われるかを表示する")
    common(p)
    p = sub.add_parser("compare", help="色の補正の前後を並べた画像を作る")
    p.add_argument("video", type=Path)
    p.add_argument("--time", type=float, help="何秒目の画面で比べるか（初期値: 真ん中）")
    p.add_argument("--out", type=Path, help="保存先（初期値: 動画と同じフォルダ）")
    common(p)
    p = sub.add_parser("cam2", help="2台目カメラの色を本編に寄せる LUT（.cube）を作る")
    p.add_argument("out_dir", type=Path, help="荒編集の出力フォルダ（○○_roughcut）")
    p.add_argument("--strength", type=float, default=0.8, help="寄せる強さ（0〜1）")
    args = ap.parse_args()
    {"check": cmd_check, "compare": cmd_compare, "cam2": cmd_cam2}[args.cmd](args)


if __name__ == "__main__":
    main()
