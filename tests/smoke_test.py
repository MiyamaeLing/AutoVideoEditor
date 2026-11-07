"""全工程の通しテスト（GPU・ネット・APIキーなしで動く）。

    python tests/smoke_test.py

荒編集 → 仕上げ → サムネイル → YouTube設定の表示 → GUI（画面なしモード）を順に実行して確認する。
新しい機能を足したら、ここにも確認を追加してください。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import make_fixtures  # noqa: E402

PY = sys.executable
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def run(args: list[str], name: str) -> bool:
    p = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    ok = p.returncode == 0
    check(name, ok, "" if ok else "\n" + "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:]))
    return ok


def duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def main():
    fx = make_fixtures.make()
    out = fx / "out"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    shutil.copy(fx / "transcript.json", out / "transcript.json")
    tpl = str(fx / "tpl")

    if run(["roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--mode", "talk", "--llm", "none",
            "--decisions", str(fx / "claude_cuts.json"), "--meta", str(fx / "claude_meta.json"),
            "--cam2", str(fx / "phone.mp4"), "--out", str(out)], "荒編集（roughcut.py）"):
        sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
        check("カット判定の反映", set(sess["cut_ids"]) == {3, 4, 5, 7}, str(sess["cut_ids"]))
        xml = out / "C0001_荒編集.xml"
        try:
            ET.parse(xml)
            check("Premiere用XMLが正しいXML", True)
        except Exception as e:  # noqa: BLE001
            check("Premiere用XMLが正しいXML", False, str(e))
        sync = json.loads((out / "sync.json").read_text())
        check("2台目カメラの同期", abs(sync["offset_sec"] + 3.5) < 0.05, f"{sync['offset_sec']:+.2f}s")

    if run(["finish.py", str(out), "--template", tpl, "--plan", str(fx / "plan.json"), "--llm", "none"],
           "仕上げ（finish.py）"):
        mp4 = out / "C0001_完成.mp4"
        d = duration(mp4)
        check("完成動画ができた", mp4.exists() and d > 5, f"{d:.2f}s")
        check("完成用の字幕とチャプター", (out / "C0001_完成.srt").exists() and (out / "final_info.json").exists())
        check("サムネイル案（書き出し後に自動）", len(list((out / "thumbnails").glob("thumb_*.jpg"))) == 9)

    run(["finish.py", str(out), "--template", tpl, "--plan", str(fx / "plan.json"), "--preview", "8", "--no-thumbs"],
        "試し書き出し（--preview）")
    run(["thumbs.py", str(out), "--template", tpl, "--llm", "none", "--text", "テスト\\n二行目"], "サムネイル（thumbs.py）")
    run(["youtube.py", str(out), "--show"], "YouTube設定の表示（youtube.py）")
    run(["finish.py", "--template-preview", "--template", tpl], "テンプレートの見本")

    # GUI（画面なしモード）
    code = f"""
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from PySide6.QtWidgets import QApplication
import gui
app = QApplication([])
w = gui.MainWindow(); w.show()
assert Path({str(out)!r}, 'session.json').exists(), 'session.json がありません（荒編集が失敗しています）'
w.review.load(Path({str(out)!r}))
assert w.review.table.rowCount() > 0
w.grab().save({str(out / 'gui_smoke.png')!r})
print('gui ok')
"""
    env = dict(os.environ, APPDATA=str(fx / "appdata"), AVE_SETTINGS_DIR=str(fx / "appdata"))
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
        check("GUIの起動と結果の読み込み", p.returncode == 0 and "gui ok" in p.stdout,
              "" if p.returncode == 0 else p.stderr[-800:])
    except subprocess.TimeoutExpired:
        check("GUIの起動と結果の読み込み", False, "時間切れ（ダイアログが出たまま止まった可能性があります）")

    # 追加・修正した機能のテスト（tests/test_*.py）
    import test_fixes
    test_fixes.run_all(check, out, fx)
    import test_enhance   # 声と映像の補正
    test_enhance.run_all(check, out, fx)
    import test_speedup   # 工作の作業シーンの自動早送り
    test_speedup.run_all(check, out, fx)
    import test_shorts   # 縦型ショート
    test_shorts.run_all(check, out, fx)
    import test_multicam   # 2台カメラの自動切り替え
    test_multicam.run_all(check, out, fx)
    import test_broll   # Bロール（差し込み素材）の自動配置
    test_broll.run_all(check, out, fx)
    import test_i18n   # 英語字幕と多言語対応
    test_i18n.run_all(check, out, fx)
    import test_speakers   # 話者の区別（字幕の色分け）
    test_speakers.run_all(check, out, fx)
    import test_mv   # MVモード（ビートに合わせたカット）
    test_mv.run_all(check, out, fx)
    import test_design   # テンプレートの編集画面とデザイン案
    test_design.run_all(check, out, fx)
    import test_graphics   # オープニング・エンディング・テロップの種類・章の進み具合
    test_graphics.run_all(check, out, fx)
    import test_thumbspro   # 人物の切り抜きサムネイル・顔を見た候補選び
    test_thumbspro.run_all(check, out, fx)
    import test_timeline   # タイムライン表示と画面のデザイン
    test_timeline.run_all(check, out, fx)
    import test_ops   # 途中からの再開・まとめて処理・困ったときの情報・自動更新
    test_ops.run_all(check, out, fx)
    import test_analytics   # 公開後の分析・コメント要約・告知文
    test_analytics.run_all(check, out, fx)
    import test_preprod   # 企画・台本支援と全動画の発言検索
    test_preprod.run_all(check, out, fx)
    import test_integration   # 最後に、機能どうしの組み合わせ（統合）
    test_integration.run_all(check, out, fx)
    import test_review_fixes   # 統合後の確認で見つかった問題の直し
    test_review_fixes.run_all(check, out, fx)
    import test_claude_code   # サブスク（Claude Code）での連携と「AIへの指示」
    test_claude_code.run_all(check, out, fx)
    import test_transcribe_numpy   # 文字起こしの途中保存（numpy の値）
    test_transcribe_numpy.run_all(check, out, fx)
    import test_captions   # 字幕の分け方（単語の途中で切らない）
    test_captions.run_all(check, out, fx)
    import test_outdir   # 保存先（結果フォルダを作る場所）を選ぶ
    test_outdir.run_all(check, out, fx)
    import test_clip   # 完成動画の区間を書き出す
    test_clip.run_all(check, out, fx)
    import test_thumbstyle   # サムネイルの見せ方（上品・ポップ・シンプル）と文字の置き方
    test_thumbstyle.run_all(check, out, fx)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
