"""保存先（結果の ○○_roughcut フォルダを作る場所）を選べることのテスト。

- 選んでいなければ、今まで通り素材と同じ場所
- 選んだフォルダの中に作り、次回も同じ場所を使う
- カメラのファイル名（C0001.MP4）は撮影ごとに同じなので、別の素材の結果は番号を付けて分け、上書きしない
- まとめて処理でも同じ保存先を使い、まだ動かしていない同じ名前の動画どうしがぶつからない
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PY = sys.executable


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


# ---------------------------------------------------------------- 結果フォルダの決め方
def t_units(check, d: Path):
    import rc_media as M
    a = _touch(d / "撮影 1" / "C0001.MP4")
    b = _touch(d / "撮影 2" / "C0001.MP4")
    c = _touch(d / "撮影 3" / "C0001.MP4")
    a2 = _touch(d / "撮影 1" / "C0002.MP4")
    a3 = _touch(d / "撮影 1" / "C0003.MP4")
    base = d / "保存 先"
    base.mkdir()

    r0 = M.result_dir([a])
    check("保存先: 選んでいなければ今まで通り素材と同じ場所", r0 == a.parent / "C0001_roughcut" and not r0.exists(), str(r0))
    r1 = M.result_dir([a], base)
    check("保存先: 選んだフォルダの中に作る", r1 == (base / "C0001_roughcut").resolve() and (r1 / "sources.json").is_file(),
          str(r1))
    check("保存先: 同じ素材なら同じフォルダ（作り直し・途中からの再開）", M.result_dir([a], base) == r1)
    r2 = M.result_dir([b], base)
    check("保存先: 別の撮影の同じ名前（C0001.MP4）は番号を付けて分ける", r2.name == "C0001_2_roughcut", r2.name)
    peek = M.result_dir([c], base, reserve=False)
    check("保存先: 画面の表示用は決めるだけ（フォルダを作らない）", peek.name == "C0001_3_roughcut" and not peek.exists(),
          peek.name)

    # 前の版で作った結果（sources.json が無く session.json だけ）も、同じ素材なら使い回す
    old = base / "C0002_roughcut"
    old.mkdir()
    (old / "session.json").write_text(json.dumps({"inputs": [str(a2.resolve())]}), encoding="utf-8")
    check("保存先: 前の版の結果（session.json だけ）も同じ素材なら使い回す", M.result_dir([a2], base).name == "C0002_roughcut")
    # どの素材のものか分からないフォルダ（ほかのアプリのものなど）には書かない
    other = base / "C0003_roughcut"
    other.mkdir()
    (other / "メモ.txt").write_text("x", encoding="utf-8")
    r3 = M.result_dir([a3], base)
    check("保存先: どの素材のものか分からないフォルダには書かない",
          r3.name == "C0003_2_roughcut" and (other / "メモ.txt").exists() and not (other / "sources.json").exists(), r3.name)
    # 空のフォルダ（前に作りかけて止まったなど）は使う
    (base / "C0004_roughcut").mkdir()
    a4 = _touch(d / "撮影 1" / "C0004.MP4")
    check("保存先: 空のフォルダはそのまま使う", M.result_dir([a4], base).name == "C0004_roughcut")
    # 分割ファイルをつないだ結果（C0001+C0002）と、C0001 だけの結果は別
    rj = M.result_dir([a, a2], base)
    check("保存先: 分割ファイルをつないだ結果は、1本だけの結果と分ける", rj not in (r1, r2) and rj.name.endswith("_roughcut"),
          rj.name)

    # まとめて処理: 追加した時点で場所を押さえるので、まだ動かしていない同じ名前の動画どうしがぶつからない
    import rc_ops_queue as Q
    qbase = d / "まとめて 保存先"
    qbase.mkdir()
    q = Q.Queue(d / "queue.json")
    i1 = q.add([str(a)], goal="rough", llm="none", out_base=str(qbase))
    i2 = q.add([str(b)], goal="rough", llm="none", out_base=str(qbase))
    check("まとめて処理: 保存先の中に作り、同じ名前の動画どうしを分ける",
          Path(i1["out"]).parent == qbase.resolve() and Path(i1["out"]).name == "C0001_roughcut"
          and Path(i2["out"]).name == "C0001_2_roughcut", f'{i1["out"]} / {i2["out"]}')
    i3 = q.add([str(c)], goal="rough", llm="none")
    check("まとめて処理: 保存先を選んでいなければ素材と同じ場所", Path(i3["out"]) == c.parent / "C0001_roughcut", i3["out"])
    try:
        q.add([str(d / "無い.MP4")], goal="rough", llm="none", out_base=str(qbase))
        bad = False
    except ValueError:
        bad = True
    check("まとめて処理: 追加できなかった動画のフォルダは作らない", bad and not (qbase / "無い_roughcut").exists())


# ---------------------------------------------------------------- 画面（画面なしで別プロセス）
GUI_CODE = r'''
import os, sys, json
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
import gui
app = QApplication([])
shown = []
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[1] if len(a) > 1 else ""))
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[1] if len(a) > 1 else ""))
res = {}
base, A, B = Path(BASE), Path(SRC_A), Path(SRC_B)
w = gui.MainWindow(); w.show()
c = w.create
c.llm.setCurrentIndex(c.llm.findData("none"))
res["first_empty"] = c.out_edit.text() == "" and c.out_reset.isHidden()
c.files.add([A])
res["first_out"] = str(c.out_dir(reserve=False))

# 選ぶと設定に残り、結果のフォルダが表示に出る
c.set_out_base(str(base))
res["saved"] = w.settings.value("out_base")
res["hint"] = c.out_hint.text()
res["reset_shown"] = not c.out_reset.isHidden()
w2 = gui.MainWindow()
res["reopen"] = w2.create.out_edit.text()
w2.close()

# 荒編集を始めると、保存先の中に作る
calls = []
def fake_start(script, args, title, on_done=None, handle_error=None):
    calls.append((script, list(args)))
    return True
w.runner.start = fake_start
c.start(full=False)
args = calls[-1][1] if calls else []
res["start_out"] = args[args.index("--out") + 1] if "--out" in args else ""

# 別の撮影の同じ名前（C0001.MP4）は番号付きになることが、始める前に表示で分かる
c.files.clear()
res["hint_cleared"] = c.out_hint.text()
c.files.add([B])
res["hint2"] = c.out_hint.text()

# まとめて処理（「作成」タブから順番待ちに入れる）も同じ保存先
qp = w.queue_page
res["queued"] = bool(qp.add_from_create("rough"))
items = qp.q.load()
res["queue_out"] = items[-1]["out"] if items else ""

# 保存先が見つからない（外付けドライブを外したなど）ときは、知らせて止める
c.set_out_base(str(base / "ない フォルダ"))
res["missing_role"] = c.out_hint.property("role")
calls.clear(); shown.clear()
c.files.add([A])
c.start(full=False)
res["missing_blocked"] = (not calls) and any("保存先" in s for s in shown)
res["missing_not_made"] = not (base / "ない フォルダ").exists()

# 素材と同じ場所に戻す
c.out_reset.click()
res["reset_text"] = c.out_edit.text()
res["reset_saved"] = w.settings.value("out_base")
res["reset_hidden"] = c.out_reset.isHidden()
res["reset_out"] = str(c.out_dir(reserve=False))
w.close()
print("RESULT " + json.dumps(res, ensure_ascii=False))
'''


def t_gui(check, d: Path):
    a = _touch(d / "撮影 A" / "C0001.MP4")
    b = _touch(d / "撮影 B" / "C0001.MP4")
    base = d / "画面の 保存先"
    base.mkdir()
    cfg = d / "appdata"
    code = f"ROOT = {str(ROOT)!r}\nBASE = {str(base)!r}\nSRC_A = {str(a)!r}\nSRC_B = {str(b)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(cfg), XDG_CONFIG_HOME=str(cfg), AVE_SETTINGS_DIR=str(cfg),
               PYTHONIOENCODING="utf-8", AVE_UPDATE_CHECK="0")
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300)
    except subprocess.TimeoutExpired:
        check("保存先（画面）: テスト", False, "時間切れ")
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("保存先（画面）: テスト", False, p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    first = str(base.resolve() / "C0001_roughcut")
    check("保存先（画面）: はじめは素材と同じ場所（今まで通り）",
          r["first_empty"] and r["first_out"] == str(a.parent / "C0001_roughcut"), r["first_out"])
    check("保存先（画面）: 選んだ場所を覚える（開き直しても同じ）",
          r["saved"] == str(base) and r["reopen"] == str(base) and r["reset_shown"], f'{r["saved"]} / {r["reopen"]}')
    check("保存先（画面）: 結果を作るフォルダを表示する", str(base / "C0001_roughcut") in r["hint"], r["hint"])
    check("保存先（画面）: 荒編集は保存先の中に作る", r["start_out"] == first, r["start_out"])
    check("保存先（画面）: 素材を消すと表示も消える", "C0001_roughcut" not in r["hint_cleared"], r["hint_cleared"])
    check("保存先（画面）: 同じ名前の別の撮影は番号付きと先に分かる", "C0001_2_roughcut" in r["hint2"], r["hint2"])
    check("保存先（画面）: まとめて処理も同じ保存先", r["queued"] and r["queue_out"] == str(base.resolve() / "C0001_2_roughcut"),
          r["queue_out"])
    check("保存先（画面）: 見つからない保存先は知らせて止める",
          r["missing_role"] == "notice" and r["missing_blocked"] and r["missing_not_made"])
    check("保存先（画面）: 素材と同じ場所に戻せる",
          r["reset_text"] == "" and r["reset_saved"] == "" and r["reset_hidden"]
          and r["reset_out"] == str(a.parent / "C0001_roughcut"), r["reset_out"])


def run_all(check, out: Path, fx: Path):
    d = fx / "outdir"
    shutil.rmtree(d, ignore_errors=True)
    t_units(check, d / "units")
    t_gui(check, d / "gui")


if __name__ == "__main__":
    import make_fixtures
    res = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    print(f"\n{sum(res)}/{len(res)} 件 成功")
    sys.exit(0 if all(res) else 1)
