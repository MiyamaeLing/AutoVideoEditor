"""まとめて処理：複数の動画を順番待ちに入れて、寝ている間に1本ずつ処理する。

使い方の例:
  python batch.py add 動画1.mp4 動画2.mp4 --goal finish          # 2本を別々の動画として追加（完成動画まで）
  python batch.py add C0001.MP4 C0002.MP4 --join --mode outdoor  # 分割ファイルを1本の動画として追加
  python batch.py add 工作.mp4 --cam2 スマホ.mp4 --mode craft --goal thumbs
  python batch.py list                                           # 一覧と結果を見る
  python batch.py run                                            # 順番に処理する（終わるまで）
  python batch.py run --shutdown                                 # 全部終わったらPCの電源を切る
  python batch.py retry <id> / remove <id> / clear               # やり直す・消す・終わったものを片付ける

goal（どこまで作るか）: rough=荒編集だけ / finish=完成動画まで / thumbs=サムネイルまで
途中で止まっても、もう一度 run すると続きから進めます。処理中は Windows がスリープしません。
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
import rc_ops
from rc_ops_queue import GOALS, Queue, lock_path, run_all, shutdown_pc, summary_lines


def log(s: str) -> None:
    print(s, flush=True)


def main():
    ap = argparse.ArgumentParser(description="まとめて処理（順番待ち）")
    ap.add_argument("--queue", type=Path, help="順番待ちの一覧のファイル（初期値: %%APPDATA%%\\AutoVideoEditor\\queue.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="順番待ちに追加する")
    a.add_argument("inputs", nargs="+", type=Path)
    a.add_argument("--join", action="store_true", help="入れたファイルを、分割された1本の動画として扱う")
    a.add_argument("--goal", choices=GOALS.keys(), default="finish",
                   help="rough=荒編集だけ / finish=完成動画まで / thumbs=サムネイルまで（初期値: finish）")
    a.add_argument("--mode", choices=["talk", "craft", "outdoor"], default="talk")
    a.add_argument("--llm", choices=["api", "none"], default="api")
    a.add_argument("--cam2", nargs="+", type=Path, help="2台目カメラの素材（--join のとき、または1本だけのとき）")
    a.add_argument("--template", type=Path, help="見た目のテンプレートフォルダ")
    a.add_argument("--glossary", help="固有名詞（「、」区切り）")
    a.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    a.add_argument("--caption-chars", type=int, default=24)
    a.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    a.add_argument("--out", type=Path, help="出力フォルダ（1本だけのとき。初期値: 素材と同じ場所に「<名前>_roughcut」）")

    r = sub.add_parser("run", help="順番待ちを上から順に処理する")
    r.add_argument("--shutdown", action="store_true", help="全部終わったらPCの電源を切る（Windows）")
    sub.add_parser("list", help="一覧と結果を表示する")
    rt = sub.add_parser("retry", help="エラーになったもの・終わったものを、もう一度順番待ちにする")
    rt.add_argument("ids", nargs="+")
    rt.add_argument("--from-start", action="store_true", help="荒編集からやり直す")
    rm = sub.add_parser("remove", help="一覧から消す（できたファイルは消しません）")
    rm.add_argument("ids", nargs="+")
    sub.add_parser("clear", help="終わったものを一覧から片付ける")
    args = ap.parse_args()

    q = Queue(args.queue)
    lk = lock_path(q.path)

    if args.cmd == "add":
        groups = [args.inputs] if args.join else [[p] for p in args.inputs]
        if args.out and len(groups) > 1:
            sys.exit("[エラー] --out は動画1本のときだけ使えます（--join を付けると1本として扱います）。")
        if args.cam2 and len(groups) > 1:
            sys.exit("[エラー] --cam2 は動画1本のときだけ使えます。")
        words = [w.strip() for w in (args.glossary or "").replace(",", "、").split("、") if w.strip()]
        for g in groups:
            try:
                it = q.add([str(p) for p in g], goal=args.goal, mode=args.mode, llm=args.llm,
                           cam2=[str(p) for p in args.cam2 or []], template=str(args.template.resolve()) if args.template else None,
                           glossary=words, effort=args.effort, caption_chars=args.caption_chars, device=args.device,
                           out=str(args.out) if args.out else None)
            except ValueError as e:
                sys.exit(f"[エラー] {e}")
            log(f"  追加しました: {Path(it['inputs'][0]).name}（{GOALS[it['goal']]}・id {it['id']}）")
        log(f"  順番待ち: {len(q.pending())}本。`python batch.py run` で処理を始めます。")
        return

    if args.cmd == "list":
        items = q.load()
        if not items:
            log("順番待ちの一覧は空です。")
            return
        for ln in summary_lines(items, rc_ops.is_locked(lk)):
            log(ln)
        return

    if args.cmd in ("retry", "remove", "clear"):
        if rc_ops.is_locked(lk) and args.cmd != "retry":
            sys.exit("[エラー] まとめて処理の実行中は、一覧から消せません。終わるか中止してからにしてください。")
        if args.cmd == "retry":
            for i in args.ids:
                q.retry(i, args.from_start)
            log("  順番待ちに戻しました。")
        elif args.cmd == "remove":
            q.remove(args.ids)
            log("  一覧から消しました（できたファイルはそのままです）。")
        else:
            log(f"  終わったもの {q.clear_done()}本を片付けました。")
        return

    # ---- run ----
    with rc_ops.SingleRun(lk) as ok:
        if not ok:
            sys.exit("[エラー] まとめて処理は、すでにほかで実行中です（画面とコマンドで同時には動かせません）。")
        pend = q.pending()
        if not pend:
            log("■ 順番待ちの動画はありません。")
            return
        rc_ops.keep_awake(True)
        log(f"■ まとめて処理を始めます（{len(pend)}本）。処理中はPCがスリープしません。")
        done, failed = run_all(q, log)
        log(f"■ まとめて処理が終わりました：完了 {done}本 / エラー {failed}本")
        for ln in summary_lines([it for it in q.load() if it["status"] in ("done", "error")]):
            log(ln)
        rc_ops.keep_awake(False)
    if args.shutdown:
        shutdown_pc(log)


if __name__ == "__main__":
    main()
