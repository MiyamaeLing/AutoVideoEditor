"""2台カメラの切り替え：どの場面で手元カメラ（2台目）を使うかを、選び直す。

演出プラン（plan_used.json / plan_edited.json）の "angles" と、Premiere用XMLの V2 を書き換えます。
動画の書き出しは finish.py（または画面の「完成動画を書き出す」）で行います。

使い方の例:
  python multicam.py "D:\\撮影\\工作_roughcut"               # 自動（無言の作業・「ここを」などの発言）
  python multicam.py "D:\\撮影\\工作_roughcut" --llm api     # Claude に両カメラの静止画と発言を見せて決める
  python multicam.py "D:\\撮影\\工作_roughcut" --show        # 今の設定を表示するだけ
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
import rc_llm
import rc_multicam
from rc_media import SourceSet, probe, require_ffmpeg
from rc_template import Template
from rc_timeline import Timeline
from rc_transcribe import Chunk, fmt_ts

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def main():
    ap = argparse.ArgumentParser(description="2台カメラの切り替え（手元カメラを使う場面を選び直す）")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--llm", choices=["api", "none"], default="none",
                    help="api=Claudeに静止画と発言を見せて決める / none=自動（無言の作業・手元を指す言葉）")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--min-shot", type=float, help="1つのカメラを続けて映す最短の秒数（初期値: テンプレートの設定）")
    ap.add_argument("--show", action="store_true", help="今の設定を表示するだけ")
    args = ap.parse_args()

    require_ffmpeg()
    out = args.out_dir.resolve()
    sess_path = out / "session.json"
    if not sess_path.exists():
        sys.exit(f"[エラー] {sess_path} がありません。先に roughcut.py を実行してください。")
    sess = json.loads(sess_path.read_text(encoding="utf-8"))
    cam2 = rc_multicam.load_cam2(out, sess)
    if cam2 is None:
        sys.exit("[エラー] 2台目カメラがありません。荒編集で --cam2 を付けて（画面では「2台目カメラ」の欄に入れて）"
                 "実行してください。")
    plan, plan_path = rc_multicam.load_plan(out)
    cfg = rc_multicam.settings(Template(args.template))
    if args.min_shot is not None:
        cfg["min_shot"] = max(0.0, args.min_shot)

    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    print(f"■ 2台カメラの切り替え（ズレ {cam2.offset:+.2f}秒 / 信頼度 {cam2.confidence:.1f}）")

    if args.show:
        angles = rc_multicam.sanitize_angles((plan or {}).get("angles", []))
    else:
        if plan is None:
            sys.exit("[エラー] 演出プランがありません。先に演出プランを作ってください"
                     f"（python finish.py \"{out}\" --plan-only）。")
        chunks = [Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))]
        usage = {"in": 0, "out": 0}
        if args.llm == "api":
            rc_llm._client()   # APIキーがなければ、ここで分かりやすく止める
        angles = rc_multicam.decide(tl, chunks, sess, cam2, cfg, args.llm == "api", args.effort, usage)
        saved = rc_multicam.save_angles(out, angles)
        xml = rc_multicam.write_xml(out, sess, angles, cam2, cfg["min_shot"])
        print(f"  演出プランを更新しました: {', '.join(p.name for p in saved)}")
        print(f"  Premiere用XMLを更新しました（選んだ所だけ V2 の手元カメラが映ります）: {xml}")
        if usage["in"]:
            print(f"  Claude API: 約${rc_llm.cost_usd(usage):.2f}")

    shots = rc_multicam.compute_shots(tl, angles, cam2, cfg["min_shot"])
    ed = rc_multicam.to_edit(tl, shots)
    print(f"■ 手元カメラを使う場面: {len(ed)}（合計 {sum(e - s for s, e in ed):.0f}秒 / 最短 {cfg['min_shot']:.1f}秒）")
    for s, e in ed:
        print(f"  {fmt_ts(s)} 〜 {fmt_ts(e)}（{e - s:.1f}秒）")
    for a in angles:
        print(f"    元の時刻 {fmt_ts(a['start'])} 〜 {fmt_ts(a['end'])}  {a.get('why', '')}")


if __name__ == "__main__":
    main()
