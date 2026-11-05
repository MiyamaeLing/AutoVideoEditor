"""全動画の発言検索（コマンド）

使い方の例:
  python search.py index "D:\\YouTube"            （フォルダ以下の ○○_roughcut を全部、索引に入れる。2回目からは差分だけ）
  python search.py find ドリル                    （全動画から発言を探す）
  python search.py find "マキタ 重さ" --include-cut   （空白で区切ると「両方を含む」発言。カットした発言も含める）
  python search.py list                           （索引に入っている動画の一覧）

索引はツールのフォルダの search_index.sqlite に保存します（--db で変えられます）。
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

import rc_win  # noqa: F401,E402  (Windowsでコンソール画面を出さない)
import rc_search  # noqa: E402
from rc_transcribe import fmt_ts  # noqa: E402


def cmd_index(a, idx: rc_search.Index):
    root = a.folder
    if not root.is_dir():
        sys.exit(f"[エラー] フォルダが見つかりません: {root}")
    print(f"■ 索引を作っています: {root}")

    def prog(i, n, f):
        print(f"  {i}/{n}本 {f.name} ({int(i / max(n, 1) * 100)}%)", flush=True)
    st = idx.update(root, prog)
    s = idx.stats()
    print("■ 完了")
    print(f"  見つかった荒編集: {st['found']}本（新しく追加 {st['added']}・読み直し {st['updated']}・"
          f"変化なし {st['same']}・消えたので外した {st['removed']}）")
    if st["failed"]:
        print(f"  [注意] 読み込めなかったもの: {st['failed']}本")
    print(f"  索引の中身: {s['videos']}本・{s['chunks']:,}発言（{'FTS5' if s['fts'] else '部分一致'}で検索）")


def cmd_find(a, idx: rc_search.Index):
    rows = idx.search(" ".join(a.words), include_cut=a.include_cut, limit=a.limit)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if not rows:
        print("見つかりませんでした。")
        if not idx.stats()["videos"]:
            print("（索引が空です。先に python search.py index <フォルダ> を実行してください）")
        return
    for r in rows:
        mark = "［カット済み］" if r["cut"] else ""
        print(f"{r['stem']}\t{fmt_ts(r['start'])}\t{mark}{r['text']}\t{r['folder']}")
    print(f"\n{len(rows)}件" + ("（上限に達しました。--limit で増やせます）" if len(rows) >= a.limit else ""))


def cmd_list(a, idx: rc_search.Index):
    vs = idx.videos()
    for v in vs:
        print(f"{v['stem']}\t{fmt_ts(v['duration'] or 0)}\t{v['n']}発言\t{v['title']}\t{v['folder']}")
    print(f"{len(vs)}本")


def main(argv=None):
    ap = argparse.ArgumentParser(description="全動画の発言検索")
    ap.add_argument("--db", type=Path, help="索引ファイル（初期値: ツールのフォルダの search_index.sqlite）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("index", help="フォルダ以下の荒編集を索引に入れる")
    i.add_argument("folder", type=Path)
    i.set_defaults(func=cmd_index)
    f = sub.add_parser("find", help="発言を探す")
    f.add_argument("words", nargs="+", help="探す言葉（空白で区切ると、全部を含む発言）")
    f.add_argument("--include-cut", action="store_true", help="荒編集でカットした発言も探す")
    f.add_argument("--limit", type=int, default=200)
    f.add_argument("--json", action="store_true", help="結果をJSONで出す")
    f.set_defaults(func=cmd_find)
    li = sub.add_parser("list", help="索引に入っている動画の一覧")
    li.set_defaults(func=cmd_list)
    a = ap.parse_args(argv)
    idx = rc_search.Index(a.db)
    try:
        a.func(a, idx)
    finally:
        idx.close()


if __name__ == "__main__":
    main()
