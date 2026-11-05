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
    env = dict(os.environ, APPDATA=str(fx / "appdata"))
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

    # テンプレートの編集画面とデザイン案（tests/test_design.py）
    import test_design
    test_design.run_all(check, out, fx)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
