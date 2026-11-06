"""字幕の分け方のテスト（単語の途中で切らない・句読点や文節の切れ目で分ける・長さをそろえる）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = [
    ("このペンの機能の種類の他に、ペンの素材の種類、木の種類として、野原工芸さんのペンを紹介します。",
     ["野原工芸", "素材", "種類"]),
    ("このペンはシャーペンなんですけれども他にもボールペンのスタンダードスリムロータリーがあります",
     ["シャーペン", "ボールペン", "スタンダード", "ロータリー"]),
    ("オイルメンテナンスについては後で詳しく説明していきたいと思いますのでよろしくお願いします",
     ["オイルメンテナンス", "詳しく", "説明", "お願い"]),
    ("インパクトドライバーTD173DRGXの重さは1.5キログラムです", ["インパクトドライバー", "TD173DRGX", "1.5", "キログラム"]),
]


def _check_all(check, label: str):
    import rc_export
    for text, words in CASES:
        parts = rc_export.split_caption(text, 24)
        joined = "".join(parts)
        whole = all(any(w in p for p in parts) for w in words)
        check(f"字幕{label}: 単語の途中で切らない（{text[:12]}…）", whole and all(len(p) <= 24 for p in parts)
              and joined == text.rstrip("。"), str(parts))
    parts = rc_export.split_caption("あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめも", 24)
    check(f"字幕{label}: 区切れない長い文でも上限は守る", all(len(p) <= 24 for p in parts), str(parts))
    parts = rc_export.split_caption("これは、短い。", 24)
    check(f"字幕{label}: 短い文はそのまま", parts == ["これは、短い"], str(parts))
    parts = rc_export.split_caption("今日はマキタのインパクトドライバーをレビューしていきます、よろしくお願いします", 24)
    check(f"字幕{label}: 句読点を次の字幕の頭に回さない", not any(p.startswith("、") for p in parts), str(parts))
    lens = [len(p) for p in rc_export.split_caption("このペンの機能の種類の他に、ペンの素材の種類、木の種類として、野原工芸さんのペンを紹介します", 24)]
    check(f"字幕{label}: 2枚の長さがそろう", max(lens) - min(lens) <= 6, str(lens))


def run_all(check, out: Path = None, fx: Path = None):
    import rc_export
    _check_all(check, "")
    saved = rc_export._PARSER
    rc_export._PARSER = False   # BudouX が入っていないとき（文字の種類の変わり目で分ける）
    try:
        _check_all(check, "（BudouXなし）")
    finally:
        rc_export._PARSER = saved


if __name__ == "__main__":
    res = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    run_all(check)
    print(f"\n{sum(res)}/{len(res)} 件 成功")
    sys.exit(0 if all(res) else 1)
