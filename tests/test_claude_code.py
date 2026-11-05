"""Claude Code（サブスク）経由で Claude を使う機能と、「AIへの指示」のテスト。

    python tests/test_claude_code.py   # 単独で実行（先に tests/smoke_test.py を実行しておくと全部できます）
    python tests/smoke_test.py         # 通しテストの最後からも呼ばれます

本物の claude コマンドの代わりに、偽の claude（Python のスクリプト）を ROUGHCUT_CLAUDE_BIN で使う。
偽の claude は、受け取ったオプション・依頼文・環境変数を記録し、JSON Schema の形に合わせた答えを返す。
"""
from __future__ import annotations

import base64
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

FAKE = r'''
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
log = os.environ["FAKE_CC_LOG"]
mode = os.environ.get("FAKE_CC_MODE", "ok")
stdin = sys.stdin.buffer.read().decode("utf-8")
rec = {"argv": argv, "stdin": stdin, "has_key": "ANTHROPIC_API_KEY" in os.environ,
       "cwd_files": sorted(p.name for p in Path.cwd().iterdir())}
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
if mode == "old" and "--effort" in argv:
    sys.stderr.write("error: unknown option '--effort'\n"); sys.exit(1)
if mode == "login":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Invalid API key · Please run /login"})); sys.exit(1)
if mode == "limit":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached|1793900000"})); sys.exit(1)
schema = json.loads(argv[argv.index("--json-schema") + 1]) if "--json-schema" in argv else {}
props = schema.get("properties", {})
ans_dir = Path(os.environ["FAKE_CC_ANSWERS"])
if "cuts" in props:
    ans = json.loads((ans_dir / "claude_cuts.json").read_text(encoding="utf-8"))
elif "titles" in props:
    ans = json.loads((ans_dir / "claude_meta.json").read_text(encoding="utf-8"))
elif "telops" in props:
    ans = json.loads((ans_dir / "plan.json").read_text(encoding="utf-8"))
else:
    ans = {"ok": "ok", "fruits": ["りんご"]}
out = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(ans, ensure_ascii=False),
       "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}
if mode != "text":
    out["structured_output"] = ans
else:
    out["result"] = "```json\n" + json.dumps(ans, ensure_ascii=False) + "\n```"
print(json.dumps(out, ensure_ascii=False))
'''


def _fake(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        script = base / "fake_claude.py"
        script.write_text(FAKE, encoding="utf-8")
        exe = base / "claude.cmd"
        exe.write_text(f'@"{PY}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = base / "claude"
        exe.write_text(f"#!{PY}\n" + FAKE, encoding="utf-8")
        exe.chmod(0o755)
    return exe


def _records(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _env(base: Path, fx: Path, mode: str = "ok", **extra) -> dict:
    log = base / "log.jsonl"
    e = dict(os.environ, ROUGHCUT_BACKEND="claude-code", ROUGHCUT_CLAUDE_BIN=str(base / ("claude.cmd" if os.name == "nt" else "claude")),
             FAKE_CC_LOG=str(log), FAKE_CC_MODE=mode, FAKE_CC_ANSWERS=str(fx), PYTHONIOENCODING="utf-8",
             ANTHROPIC_API_KEY="sk-ant-api03-SHOULD-NOT-BE-PASSED")
    e.update(extra)
    return e


# ---------------------------------------------------------------- 1. rc_cc 単体
def t_unit(check, base: Path, fx: Path):
    import importlib
    import rc_cc
    import rc_llm
    exe = _fake(base)
    log = base / "log.jsonl"
    saved = dict(os.environ)
    try:
        os.environ.update(_env(base, fx))
        importlib.reload(rc_cc)
        check("Claude Code: 偽の claude を見つける", rc_cc.find_claude() == str(exe), str(rc_cc.find_claude()))
        anthropic, client = rc_llm._client()
        check("Claude Code: ROUGHCUT_BACKEND=claude-code で Claude Code を使う", getattr(client, "is_claude_code", False))

        log.unlink(missing_ok=True)
        usage = {"in": 0, "out": 0}
        res = rc_llm._call(client, anthropic, "果物を抜き出す", "りんごと車",
                           {"type": "object", "properties": {"fruits": {"type": "array"}}}, "medium", usage)
        r = _records(log)[-1]
        check("Claude Code: structured_output を答えとして使う", res.get("fruits") == ["りんご"], str(res))
        check("Claude Code: API キーを claude に渡さない（サブスクで動かす）", r["has_key"] is False)
        check("Claude Code: -p・JSON出力・JSON Schema・ツールなしで呼ぶ",
              "-p" in r["argv"] and "--json-schema" in r["argv"] and r["argv"][r["argv"].index("--tools") + 1] == ""
              and r["argv"][r["argv"].index("--output-format") + 1] == "json", str(r["argv"]))
        check("Claude Code: 指示と依頼文は標準入力で渡す", "果物を抜き出す" in r["stdin"] and "りんごと車" in r["stdin"])
        check("Claude Code: 料金は「サブスク」として表示する", rc_llm.cost_usd(usage) == 0
              and "サブスク" in rc_llm.cost_text(usage) and usage["in"] == 110, f"{usage} {rc_llm.cost_text(usage)}")

        # 画像
        log.unlink(missing_ok=True)
        img = base64.b64encode((fx / "tpl" / "logo.png").read_bytes() if (fx / "tpl" / "logo.png").exists()
                               else b"\xff\xd8\xff\xd9").decode()
        content = [{"type": "text", "text": "どちらの写真がよい？"},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}}]
        rc_llm._call(client, anthropic, "サムネイル選び", content, {"type": "object", "properties": {}}, "low", {})
        r = _records(log)[-1]
        check("Claude Code: 画像はファイルにして Read ツールで見てもらう",
              r["cwd_files"] == ["image_01.jpg", "image_02.png"] and "image_01.jpg" in r["stdin"]
              and r["argv"][r["argv"].index("--tools") + 1] == "Read", f"{r['cwd_files']} {r['argv']}")

        # 指示（言葉で伝える）と、直すときの元の内容
        log.unlink(missing_ok=True)
        os.environ["ROUGHCUT_REQUEST"] = "工具の説明は全部残して"
        plan_file = base / "base_plan.json"
        plan_file.write_text('{"telops": [{"id": 1, "text": "前のテロップ"}]}', encoding="utf-8")
        os.environ["ROUGHCUT_REVISE_BASE"] = str(plan_file)
        import rc_plan
        rc_llm._call(client, anthropic, "カット判定", "#1 こんにちは", rc_llm.CUT_SCHEMA, "low", {})
        rc_llm._call(client, anthropic, "演出", "#1 こんにちは", rc_plan.PLAN_SCHEMA, "low", {})
        a, b = _records(log)[-2:]
        check("指示: 言葉で伝えた指示が、AIへの依頼文に入る", "工具の説明は全部残して" in a["stdin"]
              and "投稿者からの指示" in a["stdin"])
        check("指示: 今の演出は、演出プランの問い合わせにだけ付く",
              "前のテロップ" in b["stdin"] and "前のテロップ" not in a["stdin"])
        os.environ.pop("ROUGHCUT_REQUEST")
        os.environ.pop("ROUGHCUT_REVISE_BASE")

        # 古い Claude Code（知らないオプション）
        log.unlink(missing_ok=True)
        os.environ["FAKE_CC_MODE"] = "old"
        _, c2 = rc_llm._client()
        res = rc_llm._call(c2, anthropic, "x", "y", {"type": "object", "properties": {"fruits": {}}}, "high", {})
        rs = _records(log)
        check("Claude Code: 古い claude が知らないオプションは外してやり直す",
              len(rs) == 2 and "--effort" in rs[0]["argv"] and "--effort" not in rs[1]["argv"] and res.get("fruits"),
              str([r["argv"] for r in rs]))

        # JSON が文字で返ってきた場合
        os.environ["FAKE_CC_MODE"] = "text"
        res = rc_llm._call(c2, anthropic, "x", "y", {"type": "object", "properties": {"fruits": {}}}, "low", {})
        check("Claude Code: ```json で囲まれた文字の答えも読む", res.get("fruits") == ["りんご"], str(res))

        # エラーの案内
        for mode, word in (("login", "ログイン"), ("limit", "上限")):
            os.environ["FAKE_CC_MODE"] = mode
            msg = ""
            try:
                rc_llm._call(c2, anthropic, "x", "y", {"type": "object", "properties": {}}, "low", {})
            except SystemExit as e:
                msg = str(e)
            check(f"Claude Code: エラーの案内（{mode}）", word in msg and msg.startswith("[エラー]"), msg)

        # claude が無い
        os.environ["ROUGHCUT_CLAUDE_BIN"] = str(base / "nai")
        os.environ["PATH"] = str(base / "empty_path")
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(base / "empty_home")
        os.environ["APPDATA"] = str(base / "empty_home")
        msg = ""
        try:
            rc_llm._client()
        except SystemExit as e:
            msg = str(e)
        check("Claude Code: 入っていないときはインストール方法を案内する", "install" in msg and "ログイン" in msg, msg)
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---------------------------------------------------------------- 2. 荒編集〜演出プランを Claude Code で通す
def t_cli(check, base: Path, fx: Path):
    _fake(base)
    log = base / "log.jsonl"
    log.unlink(missing_ok=True)
    od = base / "結果 CC"
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True)
    shutil.copy(fx / "transcript.json", od / "transcript.json")   # 文字起こしは済んでいることにする
    env = _env(base, fx, ROUGHCUT_REQUEST="マキタの話は必ず残して")
    p = subprocess.run([PY, "roughcut.py", str(fx / "C0001.MP4"), str(fx / "C0002.MP4"), "--mode", "talk",
                        "--llm", "api", "--out", str(od)], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    rs = _records(log)
    ok = p.returncode == 0 and (od / "session.json").exists()
    check("Claude Code: 荒編集（カット判定・タイトル）が API キーなしで通る", ok,
          "" if ok else (p.stdout + p.stderr)[-1500:])
    if not ok:
        return
    sess = json.loads((od / "session.json").read_text(encoding="utf-8"))
    check("Claude Code: AIの判定がカットに使われる", 3 in sess.get("cut_ids", []) and 5 in sess.get("cut_ids", []),
          str(sess.get("cut_ids")))
    check("Claude Code: カット判定とタイトル作成の2回だけ呼ぶ", len(rs) == 2, str(len(rs)))
    check("Claude Code: 指示が全部の問い合わせに入る", all("マキタの話は必ず残して" in r["stdin"] for r in rs))
    check("Claude Code: ログの料金表示が「サブスク」", "サブスク" in p.stdout, p.stdout[-400:])

    p = subprocess.run([PY, "finish.py", str(od), "--template", str(fx / "tpl"), "--plan-only", "--llm", "api"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=600)
    check("Claude Code: 演出プランも作れる", p.returncode == 0 and (od / "plan_used.json").exists(),
          (p.stdout + p.stderr)[-1200:])


# ---------------------------------------------------------------- 3. 画面
GUI_CODE = r'''
import json, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ROOT)
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication([])
import gui
win = gui.MainWindow()
c = win.create
r = {}
keys = [c.llm.itemData(i, Qt.UserRole + 1) for i in range(c.llm.count())]
r["first_cc"] = keys[0] == "cc"
c.llm.setCurrentIndex(keys.index("cc"))
r["cc_data_is_api"] = c.llm.currentData() == "api"
r["backend_saved"] = win.settings.value("backend") == "claude-code"
r["has_key_cc"] = gui.has_api_key(win.settings)
r["check_ok"] = win.check_api_key("api")
r["cc_btn"] = c.cc_btn.isVisibleTo(c) and not c.key_btn.isVisibleTo(c)
gui.AI_STATE["request"] = "テロップ多め"
e = win.runner.env()
r["env_backend"] = e.value("ROUGHCUT_BACKEND") == "claude-code"
r["env_no_key"] = not e.contains("ANTHROPIC_API_KEY") or e.value("ANTHROPIC_API_KEY") == os.environ.get("ANTHROPIC_API_KEY", "")
r["env_request"] = e.value("ROUGHCUT_REQUEST") == "テロップ多め"
c.llm.setCurrentIndex(keys.index("api"))
r["api_backend"] = win.settings.value("backend") == "api" and win.runner.env().value("ROUGHCUT_BACKEND") == "api"
c.llm.setCurrentIndex(keys.index("cc"))
# 「確認・仕上げ」：指示でやり直す
win.review.load(Path(OUT))
r["redo_enabled"] = win.review.b_redo_cut.isEnabled() and win.review.b_redo_plan.isEnabled()
calls = []
win.review.make_plan = lambda then=None: calls.append(("plan", str(gui.AI_STATE["base"]), gui.AI_STATE["request"]))
win.review.ask.setText("ツッコミを増やして")
win.review.redo_plan()
r["redo_plan"] = bool(calls) and calls[0][1].endswith("plan_revise_base.json") and "ツッコミを増やして" in calls[0][2]
r["base_copied"] = Path(OUT, "plan_revise_base.json").exists()
# 貼り付け画面にも指示と元の内容が入る
p = Path(OUT) / "prompt_plan.md"
p.write_text("演出プランを作ってください", encoding="utf-8")
dlg = gui.ManualDialog(win, p, "演出プランを作る")
r["manual_has_request"] = "ツッコミを増やして" in dlg.prompt and "今の内容" in dlg.prompt
gui.AI_STATE["base"] = None
ran = []
win.run_roughcut = lambda out, args, llm, then_finish, template, **kw: ran.append((llm, args))
from PySide6.QtWidgets import QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
win.review.redo_cuts()
r["redo_cuts"] = bool(ran) and ran[0][0] == "api" and "--decisions" not in ran[0][1] and "--mode" in ran[0][1]
print("RESULT " + json.dumps(r))
'''


def t_gui(check, out: Path, fx: Path, base: Path):
    if not (out / "session.json").exists():
        check("Claude Code 画面: テスト（先に smoke_test.py を実行してください）", False)
        return
    d = base / "画面"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(out, d, ignore=shutil.ignore_patterns("work", "analytics", "shorts", "thumbnails", "*.mp4"))
    shutil.copy(fx / "plan.json", d / "plan_used.json")
    (d / "plan_edited.json").unlink(missing_ok=True)
    _fake(base)
    code = f"ROOT = {str(ROOT)!r}\nOUT = {str(d)!r}\n" + GUI_CODE
    env = dict(os.environ, APPDATA=str(base / "appdata"), XDG_CONFIG_HOME=str(base / "appdata"),
               PYTHONIOENCODING="utf-8", ROUGHCUT_CLAUDE_BIN=str(base / ("claude.cmd" if os.name == "nt" else "claude")))
    env.pop("ANTHROPIC_API_KEY", None)
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=300)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), "")
    if not line:
        check("Claude Code 画面: テスト", False, p.stdout[-800:] + p.stderr[-1500:])
        return
    r = json.loads(line[7:])
    names = {
        "first_cc": "AIの欄の最初が「Claude で自動（サブスク・Claude Code）」",
        "cc_data_is_api": "サブスクを選んでも、処理は「Claude で自動」と同じ（--llm api）",
        "backend_saved": "サブスクを選ぶと設定に残る",
        "has_key_cc": "サブスクなら APIキーがなくても使える",
        "check_ok": "Claude Code が入っていれば、キーの確認で止めない",
        "cc_btn": "サブスクのときは「接続を確認」、APIのときは「APIキーを設定…」",
        "env_backend": "処理に ROUGHCUT_BACKEND=claude-code を渡す",
        "env_no_key": "サブスクのときは保存した APIキーを渡さない",
        "env_request": "「AIへの指示」を処理に渡す",
        "api_backend": "APIに戻すと ROUGHCUT_BACKEND=api",
        "redo_enabled": "「この指示で〜」のボタンが押せる",
        "redo_plan": "「この指示で演出を直す」は、今の演出と指示を付けて作り直す",
        "base_copied": "直す元の演出は写しを使う",
        "manual_has_request": "貼り付け画面のプロンプトにも、指示と今の演出が入る",
        "redo_cuts": "「この指示でカットをやり直す」は文字起こしを使い回して判定し直す",
    }
    for k, name in names.items():
        check(f"Claude Code 画面: {name}", r.get(k), "" if r.get(k) else json.dumps(r, ensure_ascii=False))


def run_all(check, out: Path, fx: Path):
    base = fx / "claude_code"
    base.mkdir(parents=True, exist_ok=True)
    t_unit(check, base, fx)
    t_cli(check, base, fx)
    t_gui(check, out, fx, base)


def main():
    import make_fixtures
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    out = fx / "out"
    run_all(check, out, fx)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 件 成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
