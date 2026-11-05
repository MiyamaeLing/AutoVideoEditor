"""工作の作業シーンの早送り：一覧の表示と、Premiere 用の「早送り入り」XML の作成。

使い方の例:
  python speedup.py "D:\\撮影\\工作_roughcut"          # 早送りの一覧を表示
  python speedup.py "D:\\撮影\\工作_roughcut" --xml    # 早送り入りの Premiere 用 XML を作る

早送りの倍率は、演出プラン（plan_edited.json → plan_used.json の順に読む）の "speedups" に入っています。
プランがまだないときは、テンプレートの [speedup] の設定で自動で決めます。
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
import rc_speedup
from rc_media import SourceSet, probe, require_ffmpeg
from rc_template import Template
from rc_transcribe import fmt_ts

DEFAULT_TEMPLATE = Path(__file__).parent / "templates" / "default"


def load(out_dir: Path, template: Path) -> tuple[dict, list, list[dict]]:
    """(session, 作業区間, 早送りの一覧) を返す"""
    sess_path = out_dir / "session.json"
    if not sess_path.exists():
        sys.exit(f"[エラー] {sess_path} がありません。先に荒編集（roughcut.py）を実行してください。")
    sess = json.loads(sess_path.read_text(encoding="utf-8"))
    chunks = json.loads((out_dir / "chunks.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in chunks}
    cfg = rc_speedup.config(Template(template))
    total = None
    if "silent_work" not in sess:
        total = SourceSet([probe(Path(p)) for p in sess["inputs"]]).duration
    works = rc_speedup.work_ranges(sess, chunks, total) if cfg.get("enabled", True) else []
    plan = {}
    for name in ("plan_edited.json", "plan_used.json"):
        if (out_dir / name).exists():
            plan = json.loads((out_dir / name).read_text(encoding="utf-8-sig"))
            break
    return sess, works, rc_speedup.sanitize(plan, works, by_id, cfg)


def main():
    ap = argparse.ArgumentParser(description="工作の作業シーンの早送り（一覧・Premiere用XML）")
    ap.add_argument("out_dir", type=Path, help="roughcut.py の出力フォルダ（○○_roughcut）")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--xml", action="store_true", help="早送り入りの Premiere 用 XML を作る")
    args = ap.parse_args()
    require_ffmpeg()
    out_dir = args.out_dir.resolve()
    sess, works, speedups = load(out_dir, args.template)
    print(f"■ 作業シーンの早送り（{sess['stem']}）")
    if not works and not speedups:
        print("  無言の作業シーンはありません（工作モードで荒編集すると見つけます）。")
    for n, (s, e) in enumerate(works, 1):
        sp = next((x for x in speedups if x["start"] < e and x["end"] > s), None)
        how = (f"{'タイムラプス' if sp['style'] == 'timelapse' else '早送り'} ×{rc_speedup.fmt_speed(sp['speed'])}"
               f" → {(e - s) / sp['speed']:.1f}秒" if sp else "普通の速さ")
        print(f"  作業{n}: {fmt_ts(s)} から {e - s:.0f}秒  {how}")
    for x in speedups:
        if x.get("work", 0) == 0:
            print(f"  追加: {fmt_ts(x['start'])} から {x['end'] - x['start']:.0f}秒  ×{rc_speedup.fmt_speed(x['speed'])}")
    if args.xml:
        if not speedups:
            print("  早送りがないので、XML は作りません（いつもの荒編集の XML を使ってください）。")
            return
        p = rc_speedup.write_session_xml(out_dir, speedups)
        print(f"■ Premiere用（早送り入り）: {p}")
        print("  Premiere Pro の「ファイル → 読み込み」で選んでください。早送りの場所には印（マーカー）も付いています。")


if __name__ == "__main__":
    main()
