"""話者の区別（実験的な機能）：発言ごとに話者を推定して、字幕を人ごとに色分けする。

使い方の例:
  python speakers.py "D:\\撮影\\屋外_roughcut"                  # 人数を自動で推定
  python speakers.py "D:\\撮影\\屋外_roughcut" --num 3          # 3人と指定（こちらのほうが正確）
  python speakers.py "D:\\撮影\\屋外_roughcut" --srt-names on   # Premiere用の字幕（SRT）に名前を入れる
  python speakers.py "D:\\撮影\\屋外_roughcut" --off            # 色分けをやめる（推定の結果は残す）

結果は ○○_roughcut フォルダの speakers.json に保存されます。名前の変更や発言ごとの手直しは、
画面（GUI）の「確認・仕上げ」タブの「話者の色分け」でできます。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import rc_win  # noqa: F401  (Windowsでコンソール画面を出さない)
import rc_speakers
from rc_media import SourceSet, probe, require_ffmpeg

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def _template(p: Path | None):
    from rc_template import Template
    p = p or DEFAULT_TEMPLATE
    return Template(p) if (p / "template.toml").exists() else None


def main():
    ap = argparse.ArgumentParser(description="話者の区別（字幕の色分け・実験的な機能）")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--num", default=None, help="話している人数（2 など）。auto または省略で自動推定")
    ap.add_argument("--max", type=int, default=6, help="自動推定のときの最大人数（初期値: 6）")
    ap.add_argument("--no-detect", action="store_true", help="推定はしない（設定の変更や字幕の書き直しだけ）")
    ap.add_argument("--srt-names", choices=["on", "off"], help="Premiere用の字幕（SRT）に話者名を入れる")
    ap.add_argument("--off", action="store_true", help="完成動画の字幕の色分けをやめる")
    ap.add_argument("--on", action="store_true", help="完成動画の字幕を色分けする（推定したときは自動で on）")
    ap.add_argument("--names", help="話者の名前を「、」区切りで（例: たろう、はなこ）")
    ap.add_argument("--write-srt", action="store_true", help="Premiere用の字幕（○○_字幕.srt）を今の設定で書き直す")
    ap.add_argument("--template", type=Path, help="見た目テンプレート（SRTの名前の書き方に使う）")
    args = ap.parse_args()

    out = args.out_dir.resolve()
    if not (out / "session.json").exists() or not (out / "chunks.json").exists():
        sys.exit(f"[エラー] {out} に session.json / chunks.json がありません。先に roughcut.py を実行してください。")
    require_ffmpeg()
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    chunks = rc_speakers._chunks_of(out)

    if not args.no_detect:
        num = args.num
        if num not in (None, "auto", "自動"):
            try:
                num = int(num)
            except ValueError:
                sys.exit(f"[エラー] --num には人数（2 など）か auto を指定してください: {args.num}")
            if num < 1:
                sys.exit("[エラー] --num は 1 以上にしてください。")
        src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
        rc_speakers.detect(src, chunks, out, num, args.max)

    data = rc_speakers.load(out, chunks)
    if data is None:
        sys.exit("[エラー] 話者の推定結果（speakers.json）がありません。--no-detect を外して実行してください。")
    changed = False
    if args.names:
        names = [n.strip() for n in args.names.replace(",", "、").split("、")]
        for i, n in enumerate(names):
            if not n:
                continue
            while len(data["names"]) <= i:
                data["names"].append(f"話者{len(data['names']) + 1}")
            data["names"][i] = n
        changed = True
    if args.off or args.on:
        data["enabled"] = bool(args.on and not args.off)
        changed = True
    if args.srt_names:
        data["srt_names"] = args.srt_names == "on"
        changed = True
    if changed:
        rc_speakers.save(out, data)
    if args.write_srt or args.srt_names or (not args.no_detect and data.get("srt_names")):
        p = rc_speakers.rewrite_premiere_srt(out, _template(args.template))
        print(f"  Premiere用の字幕を書き直しました: {p}")

    lab = rc_speakers.labels_of(data)
    print("■ 完了")
    print("  話者: " + "、".join(f"{rc_speakers.name_of(data, j)}（{sum(1 for v in lab.values() if v == j)}発言）"
                                   for j in range(len(data["names"]))))
    print(f"  完成動画の字幕の色分け: {'する' if data.get('enabled', True) else 'しない'}"
          f" / Premiere用SRTの名前: {'入れる' if data.get('srt_names') else '入れない'}")
    print(f"  結果: {out / rc_speakers.FILE}")


if __name__ == "__main__":
    main()
