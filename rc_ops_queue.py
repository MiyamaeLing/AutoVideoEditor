"""まとめて処理の「順番待ちの一覧」（queue.json）と、1本ずつ順番に処理する仕組み。

一覧の場所: %APPDATA%\\AutoVideoEditor\\queue.json（アプリを更新しても消えない場所）
1本の処理は roughcut.py → finish.py を別プロセスで順に動かし、ログは各動画の出力フォルダの batch_log.txt に残す。
途中で止まっても（PCの電源が落ちても）、次に実行したときに続きから進める
（文字起こしの途中保存・書き出し済みの本編パーツも使い回す）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import rc_ops

APP_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = APP_DIR / "templates" / "default"

GOALS = {"rough": "荒編集だけ", "finish": "完成動画まで", "thumbs": "サムネイルまで"}
STATUS_JA = {"waiting": "順番待ち", "running": "処理中", "done": "完了", "error": "エラー"}
MODES_JA = {"talk": "レビュー・解説", "craft": "工作", "outdoor": "屋外企画"}
STEP_JA = {"rough": "荒編集", "finish": "仕上げ（完成動画）"}


def queue_path() -> Path:
    return rc_ops.user_dir() / "queue.json"


def lock_path(qpath: Path) -> Path:
    return Path(qpath).with_suffix(".lock")


def default_out(inputs: list[str], base: str | None = None) -> Path:
    """結果フォルダ。base（「作成」タブで選んだ保存先）が無ければ、素材と同じ場所の「<名前>_roughcut」"""
    import rc_media
    return rc_media.result_dir(inputs, base)


class Queue:
    """順番待ちの一覧。読み書きのたびにファイルを読み直すので、画面とコマンドの両方から触っても大丈夫"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else queue_path()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        import json
        for _ in range(5):
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                return list(data.get("items", []))
            except (OSError, ValueError):
                time.sleep(0.1)   # ちょうど書き換え中だったとき
        return []

    def save(self, items: list[dict]) -> None:
        """書き込む。一時ファイルの名前を毎回変えて、画面とまとめて処理が同時に書いてもぶつからないようにする"""
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        for _ in range(40):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:   # Windows でほかのプロセスが一瞬開いているとき
                time.sleep(0.05)
        try:
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _edit_lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".edit.lock")

    def _lock(self):
        """「読む → 書き換える → 保存」の間、ほかのプロセスが同時に書き換えないようにする（相手の更新が消えないように）"""
        import contextlib

        @contextlib.contextmanager
        def held():
            lp = self._edit_lock_path()
            lp.parent.mkdir(parents=True, exist_ok=True)
            fd = None
            for _ in range(200):   # 長くても10秒ほど待つ
                try:
                    fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    try:
                        if time.time() - lp.stat().st_mtime > 10:   # 落ちたプロセスが残したもの
                            lp.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    time.sleep(0.05)
                except PermissionError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                if fd is not None:
                    os.close(fd)
                    try:
                        lp.unlink(missing_ok=True)
                    except OSError:
                        pass
        return held()

    def add(self, inputs: list[str], goal: str = "finish", mode: str = "talk", llm: str = "api",
            cam2: list[str] | None = None, template: str | None = None, glossary: list[str] | None = None,
            effort: str = "medium", caption_chars: int = 24, device: str = "auto", out: str | None = None,
            out_base: str | None = None) -> dict:
        """out_base: 結果フォルダを作る場所（out が無いとき。空なら素材と同じ場所）"""
        if goal not in GOALS:
            raise ValueError(f"goal は {', '.join(GOALS)} のどれかです")
        if llm == "manual":
            raise ValueError("まとめて処理では「claude.ai に貼り付けて使う」は使えません（貼り付ける人がいないため）。"
                             "「Claude APIで自動」か「AIを使わない」を選んでください。")
        inputs = [str(Path(p).resolve()) for p in inputs]
        for p in inputs + list(cam2 or []):
            if not Path(p).exists():
                raise ValueError(f"ファイルが見つかりません: {p}")
        item = {
            "id": uuid.uuid4().hex[:8], "inputs": inputs, "cam2": [str(Path(p).resolve()) for p in cam2 or []],
            "goal": goal, "mode": mode, "llm": llm, "template": str(template or DEFAULT_TEMPLATE),
            "glossary": list(glossary or []), "effort": effort, "caption_chars": int(caption_chars), "device": device,
            "out": str(Path(out).resolve()) if out else str(default_out(inputs, out_base)),
            "status": "waiting", "steps_done": [], "message": "", "added": rc_ops.now_text(),
            "started": "", "finished": "", "elapsed": 0, "result": {},
        }
        with self._lock():
            items = self.load()
            items.append(item)
            self.save(items)
        return item

    def get(self, item_id: str) -> dict | None:
        return next((it for it in self.load() if it["id"] == item_id), None)

    def update(self, item_id: str, **fields) -> dict | None:
        with self._lock():
            items = self.load()
            for it in items:
                if it["id"] == item_id:
                    it.update(fields)
                    self.save(items)
                    return it
        return None

    def remove(self, ids) -> None:
        ids = set(ids)
        with self._lock():
            self.save([it for it in self.load() if it["id"] not in ids])

    def move(self, item_id: str, delta: int) -> None:
        with self._lock():
            items = self.load()
            i = next((k for k, it in enumerate(items) if it["id"] == item_id), None)
            if i is None:
                return
            j = max(0, min(len(items) - 1, i + delta))
            items.insert(j, items.pop(i))
            self.save(items)

    def retry(self, item_id: str, from_start: bool = False) -> None:
        fields = {"status": "waiting", "message": "", "finished": ""}
        if from_start:
            fields["steps_done"] = []
        self.update(item_id, **fields)

    def clear_done(self) -> int:
        with self._lock():
            items = self.load()
            keep = [it for it in items if it["status"] != "done"]
            self.save(keep)
        return len(items) - len(keep)

    def pending(self) -> list[dict]:
        """これから処理するもの（途中で止まった「処理中」のものも、続きから）"""
        return [it for it in self.load() if it["status"] in ("waiting", "running")]


def status_text(it: dict, batch_running: bool) -> str:
    st = it.get("status", "waiting")
    if st == "running" and not batch_running:
        return "中断（次は続きから）"
    return STATUS_JA.get(st, st)


def item_name(it: dict) -> str:
    names = [Path(p).name for p in it["inputs"]]
    return names[0] + (f" ＋{len(names) - 1}ファイル" if len(names) > 1 else "")


def steps_for(goal: str) -> list[str]:
    return ["rough"] if goal == "rough" else ["rough", "finish"]


def step_args(it: dict, step: str) -> tuple[str, list[str]]:
    out = Path(it["out"])
    if step == "rough":
        args = [*it["inputs"], "--mode", it["mode"], "--out", str(out), "--llm", it["llm"],
                "--effort", it["effort"], "--caption-chars", str(it["caption_chars"]), "--device", it["device"]]
        if it.get("cam2"):
            args += ["--cam2", *it["cam2"]]
        if it.get("glossary"):
            gp = out / "glossary.txt"
            gp.write_text("\n".join(it["glossary"]), encoding="utf-8")
            args += ["--glossary", str(gp)]
        return "roughcut.py", args
    args = [str(out), "--template", it["template"], "--llm", it["llm"], "--effort", it["effort"]]
    edited = out / "plan_edited.json"
    if edited.exists():
        args += ["--plan", str(edited)]   # 画面で直した演出があれば、それで書き出す
    if it["goal"] != "thumbs":
        args.append("--no-thumbs")
    return "finish.py", args


_COST = re.compile(r"約\$([\d.]+)")


def _error_text(lines: list[str]) -> str:
    """ログの最後の「[エラー]」とその続き、なければ Traceback の最後の行"""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("[エラー]"):
            block = [lines[i].strip()]
            for ln in lines[i + 1:i + 6]:
                if ln.strip().startswith(("■", "[注意]", "[お知らせ]")):
                    break
                block.append(ln.strip())
            return "\n".join(b for b in block if b)
    tb = [ln for ln in lines if ln.strip() and not ln.startswith((" ", "\t"))]
    return tb[-1].strip() if tb else "原因が分かりませんでした（batch_log.txt を見てください）"


def run_step(script: str, args: list[str], log_fp, env: dict | None = None) -> tuple[int, list[str]]:
    """子のプロセスを動かし、出力をそのまま画面とログに流す"""
    e = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    e.update(env or {})
    p = subprocess.Popen([sys.executable, "-u", str(APP_DIR / script), *args], cwd=str(APP_DIR),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e)
    tail: list[str] = []
    try:
        for raw in p.stdout:
            ln = raw.decode("utf-8", "replace").rstrip("\r\n")
            print(ln, flush=True)
            log_fp.write(ln + "\n")
            log_fp.flush()
            tail.append(ln)
            if len(tail) > 400:
                del tail[:100]
        p.wait()
    finally:
        if p.poll() is None:   # Ctrl+C などで止めたとき、子だけ動き続けないように
            p.kill()
            p.wait()
    return p.returncode, tail


def _result(it: dict) -> dict:
    """出来上がったものの一覧（結果の確認用）"""
    import json
    out = Path(it["out"])
    stem = Path(it["inputs"][0]).stem
    res = {}
    xml = out / f"{stem}_荒編集.xml"
    if xml.exists():
        res["xml"] = str(xml)
    mp4 = out / f"{stem}_完成.mp4"
    if mp4.exists():
        res["video"] = str(mp4)
    thumbs = sorted((out / "thumbnails").glob("thumb_*.jpg")) if (out / "thumbnails").is_dir() else []
    if thumbs and it["goal"] == "thumbs":
        res["thumbnails"] = len(thumbs)
    try:
        sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
        keep = sum(e - s for s, e in sess.get("keep", []))
        res["edited_sec"] = round(keep, 1)
        titles = (sess.get("meta") or {}).get("titles") or []
        if titles:
            res["title"] = titles[0]
    except (OSError, ValueError):
        pass
    try:
        fi = json.loads((out / "final_info.json").read_text(encoding="utf-8"))
        if mp4.exists():
            res["final_sec"] = fi.get("duration")
    except (OSError, ValueError):
        pass
    return res


def process_item(q: Queue, it: dict, log=print) -> bool:
    """1本を処理する。終わっている工程は飛ばす。成功したら True"""
    out = Path(it["out"])
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    q.update(it["id"], status="running", started=it.get("started") or rc_ops.now_text(), message="")
    done = list(it.get("steps_done") or [])
    cost = float(it.get("result", {}).get("cost_usd", 0) or 0)
    with open(out / "batch_log.txt", "a", encoding="utf-8") as fp:
        fp.write(f"\n===== まとめて処理 {rc_ops.now_text()} / {item_name(it)}（{GOALS[it['goal']]}） =====\n")
        for step in steps_for(it["goal"]):
            if step in done:
                log(f"  {STEP_JA[step]}は前回終わっているので飛ばします")
                continue
            for f in it["inputs"] + it.get("cam2", []):
                if not Path(f).exists():
                    msg = f"[エラー] 素材が見つかりません: {f}"
                    fp.write(msg + "\n")
                    q.update(it["id"], status="error", message=msg, finished=rc_ops.now_text())
                    log(msg)
                    return False
            log(f"■ {item_name(it)}：{STEP_JA[step]}")
            script, args = step_args(it, step)
            fp.write(f"--- {STEP_JA[step]}: {script} {' '.join(args)}\n")
            fp.flush()
            code, tail = run_step(script, args, fp)
            cost += sum(float(m) for ln in tail for m in _COST.findall(ln) if "Claude API" in ln)
            if code != 0:
                msg = _error_text(tail)
                q.update(it["id"], status="error", message=msg, finished=rc_ops.now_text(),
                         elapsed=round(it.get("elapsed", 0) + time.time() - t0),
                         result={**_result(it), "cost_usd": round(cost, 3)})
                log("  → エラーで止まりました。次の動画に進みます。")
                return False
            done.append(step)
            q.update(it["id"], steps_done=done)
    res = _result(it)
    res["cost_usd"] = round(cost, 3)
    q.update(it["id"], status="done", finished=rc_ops.now_text(), message="",
             elapsed=max(1, round(it.get("elapsed", 0) + time.time() - t0)), result=res)
    return True


def run_all(q: Queue, log=print) -> tuple[int, int]:
    """順番待ちのものを上から順に全部処理する。(完了, エラー) の件数を返す。途中で足されたものも処理する"""
    ok = ng = 0
    tried: set[str] = set()
    while True:
        nxt = next((it for it in q.pending() if it["id"] not in tried), None)
        if nxt is None:
            break
        tried.add(nxt["id"])
        n_all = len(q.pending()) + ok + ng
        log(f"■ [{ok + ng + 1}/{n_all}] {item_name(nxt)}（{GOALS[nxt['goal']]}・{MODES_JA.get(nxt['mode'], nxt['mode'])}）")
        if process_item(q, nxt, log):
            ok += 1
        else:
            ng += 1
    return ok, ng


def fmt_sec(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def summary_lines(items: list[dict], batch_running: bool = False) -> list[str]:
    lines = []
    for i, it in enumerate(items, 1):
        res = it.get("result") or {}
        extra = []
        if res.get("video"):
            extra.append(f"完成 {Path(res['video']).name}（{fmt_sec(res.get('final_sec'))}）")
        elif res.get("xml"):
            extra.append(f"Premiere用 {Path(res['xml']).name}")
        if res.get("thumbnails"):
            extra.append(f"サムネイル案 {res['thumbnails']}枚")
        if res.get("cost_usd"):
            extra.append(f"Claude 約${res['cost_usd']:.2f}")
        if it.get("elapsed"):
            extra.append(f"かかった時間 {fmt_sec(it['elapsed'])}")
        msg = it.get("message", "").splitlines()[0] if it.get("message") else ""
        lines.append(f"{i:>2}. [{status_text(it, batch_running)}] {item_name(it)} "
                     f"（{GOALS.get(it['goal'], it['goal'])}・{MODES_JA.get(it['mode'], it['mode'])}・id {it['id']}）")
        if extra:
            lines.append("      " + " / ".join(extra))
        if msg:
            lines.append("      " + msg)
        if it.get("status") in ("done", "error"):
            lines.append(f"      出力フォルダ: {it['out']}")
    return lines


def shutdown_pc(log=print) -> None:
    """すべて終わったあとに PC の電源を切る（2分後。取り消すときは `shutdown /a`）"""
    if os.name != "nt":
        log("  [お知らせ] 電源を切るのは Windows だけです")
        return
    log("■ 2分後にPCの電源を切ります（取り消すときは、Windowsキー＋R で「shutdown /a」）")
    subprocess.run(["shutdown", "/s", "/t", "120", "/c", "AI動画編集ツールのまとめて処理が終わりました"])
