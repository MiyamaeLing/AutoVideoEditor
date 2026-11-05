"""最新版への更新：GitHub（MiyamaeLing/AutoVideoEditor の main）から zip を取ってきて上書きする。

守るもの（消さない・上書きしない）
- .venv（ライブラリ）・素材の出力フォルダ・ログ・自分で足した拡張（gui_ext_○○.py）など、zip に入っていないもの
  （更新では、ファイルを消すことはしません）
- templates の中で、自分で変えたファイル（template.toml・BGM・効果音・フォント）。新しいファイルは追加します
- 画面の設定（Windows のユーザー設定）・順番待ちの一覧・YouTube連携（%APPDATA% 側にあるので触りません）

上書きしたファイルは _update_backup/日時/ に残すので、`python update.py --rollback` で元に戻せます。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

APP_DIR = Path(__file__).resolve().parent
REPO = os.environ.get("AVE_UPDATE_REPO", "MiyamaeLing/AutoVideoEditor")
BRANCH = os.environ.get("AVE_UPDATE_BRANCH", "main")
API_URL = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"

VERSION_FILE = ".installed_version.json"     # 更新したときの版（コミット）
MANIFEST_FILE = ".installed_manifest.json"   # 更新で入れたファイルの中身の印（自分で変えたかの判定に使う）
BACKUP_DIR = "_update_backup"
TMP_DIR = "_update_tmp"
# zip に入っていても、手元のものを上書きしない場所
NEVER_TOUCH = (".venv/", ".git/", BACKUP_DIR + "/", TMP_DIR + "/", "tests/_fixtures/")
USER_OWNED = ("templates/",)                 # 自分で変えていたら残す場所
KEEP_BACKUPS = 3
NEW_COPY = "_新しい版（残したファイルの更新後の中身）"


class UpdateError(Exception):
    pass


# ============================================================
#  版の確認
# ============================================================
def _parse_date(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _git_head(app_dir: Path) -> str | None:
    g = app_dir / ".git"
    if not g.exists():
        return None
    try:
        top = subprocess.run(["git", "-C", str(app_dir), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != app_dir.resolve():
            return None
        p = subprocess.run(["git", "-C", str(app_dir), "log", "-1", "--format=%H %cI"], capture_output=True,
                           text=True, timeout=10)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def local_version(app_dir: Path = APP_DIR) -> dict:
    """手元の版。{"sha", "date", "how"}。how = update（更新で記録）/ git / files（ファイルの日付から推定）"""
    vf = app_dir / VERSION_FILE
    if vf.exists():
        try:
            d = json.loads(vf.read_text(encoding="utf-8"))
            if d.get("sha"):
                return {"sha": d["sha"], "date": d.get("date", ""), "how": "update"}
        except (OSError, ValueError):
            pass
    g = _git_head(app_dir)
    if g:
        sha, date = (g.split(" ", 1) + [""])[:2]
        return {"sha": sha, "date": date, "how": "git"}
    # zip を展開しただけのときは、ファイルの日付（zip の中の日付＝その版の日付）から推定する
    newest = 0.0
    for p in app_dir.glob("*.py"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    date = _dt.datetime.fromtimestamp(newest, _dt.timezone.utc).isoformat() if newest else ""
    return {"sha": "", "date": date, "how": "files"}


def version_text(v: dict) -> str:
    d = _parse_date(v.get("date", ""))
    ds = d.astimezone().strftime("%Y-%m-%d %H:%M") if d else "日付不明"
    if v.get("sha"):
        return f"{v['sha'][:7]}（{ds}）"
    return f"{ds}ごろの版（ファイルの日付から推定）" if d else "不明"


def _http_get(url: str, timeout: float = 15, progress=None) -> bytes:
    """URL の中身を取ってくる（テストではこの関数を差し替える）"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "AutoVideoEditor-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        last = -10
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            buf.write(chunk)
            if progress and total:
                pct = int(buf.tell() * 100 / total)
                if pct >= last + 10:
                    last = pct
                    progress(pct)
        return buf.getvalue()


def remote_version(timeout: float = 15) -> dict:
    """GitHub の main の最新のコミット。{"sha", "date", "message"}"""
    try:
        data = json.loads(_http_get(API_URL, timeout=timeout).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"最新版の情報を取得できませんでした（ネットにつながっているか確認してください）: {e}") from e
    try:
        return {"sha": data["sha"], "date": data["commit"]["committer"]["date"],
                "message": data["commit"]["message"].splitlines()[0]}
    except (KeyError, TypeError, AttributeError) as e:
        raise UpdateError(f"最新版の情報の形が想定と違います: {e}") from e


def is_newer(local: dict, remote: dict) -> bool:
    if local.get("sha"):
        if local["sha"] == remote["sha"]:
            return False
        ld, rd = _parse_date(local.get("date", "")), _parse_date(remote.get("date", ""))
        return not (ld and rd and rd <= ld)
    # 版が分からないとき：ファイルの日付より1日以上新しい版があれば知らせる（zip の日付は時差でずれるため）
    ld, rd = _parse_date(local.get("date", "")), _parse_date(remote.get("date", ""))
    if not rd:
        return False
    return not ld or rd > ld + _dt.timedelta(days=1)


def check(app_dir: Path = APP_DIR, timeout: float = 15) -> tuple[bool, dict, dict]:
    """(新しい版があるか, 手元の版, 最新の版)"""
    loc = local_version(app_dir)
    rem = remote_version(timeout)
    return is_newer(loc, rem), loc, rem


# ============================================================
#  更新
# ============================================================
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(p: Path) -> str | None:
    try:
        return sha256(p.read_bytes())
    except OSError:
        return None


def read_zip(data: bytes) -> tuple[dict[str, bytes], str]:
    """GitHub の zip を {アプリのフォルダからの相対パス: 中身} にする。zip のコメントはコミットのID"""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UpdateError("ダウンロードしたファイルが zip ではありません（途中で切れた可能性があります）。") from e
    names = [n for n in z.namelist() if not n.endswith("/")]
    if not names:
        raise UpdateError("zip の中身が空です。")
    top = {n.split("/", 1)[0] for n in names}
    strip = len(top) == 1 and all("/" in n for n in names)
    out = {}
    for n in names:
        rel = n.split("/", 1)[1] if strip else n
        pp = PurePosixPath(rel)
        if pp.is_absolute() or ".." in pp.parts or ":" in rel:
            raise UpdateError(f"zip の中に危ないパスがあります: {n}")
        out[str(pp)] = z.read(n)
    for need in ("gui.py", "requirements.txt", "roughcut.py"):
        if need not in out:
            raise UpdateError(f"このツールの zip ではないようです（{need} がありません）。")
    sha = z.comment.decode("ascii", "replace").strip() if z.comment else ""
    return out, (sha if len(sha) == 40 else "")


def plan_update(files: dict[str, bytes], app_dir: Path) -> dict:
    """何を書き換えるかを決める。{"write": [...], "add": [...], "kept": [...], "same": n}"""
    manifest = {}
    mf = app_dir / MANIFEST_FILE
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    plan = {"write": [], "add": [], "kept": [], "same": 0}
    for rel, data in sorted(files.items()):
        if rel.startswith(NEVER_TOUCH) or rel in (VERSION_FILE, MANIFEST_FILE):
            continue
        dst = app_dir / rel
        if not dst.exists():
            plan["add"].append(rel)
            continue
        cur = _file_hash(dst)
        if cur == sha256(data):
            plan["same"] += 1
            continue
        if rel.startswith(USER_OWNED) and manifest.get(rel) != cur:
            # 前の更新のときから変わっている（＝自分で直した）か、いつのものか分からないので残す
            plan["kept"].append(rel)
            continue
        plan["write"].append(rel)
    return plan


def apply(files: dict[str, bytes], app_dir: Path, sha: str = "", date: str = "", log=print) -> dict:
    """ファイルを書き換える。上書きするものは先にバックアップする。結果の plan を返す"""
    plan = plan_update(files, app_dir)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = app_dir / BACKUP_DIR / stamp
    old_req = _file_hash(app_dir / "requirements.txt")
    old_setup = _file_hash(app_dir / "setup.ps1")
    if plan["write"] or plan["add"] or plan["kept"]:
        bdir.mkdir(parents=True, exist_ok=True)
        for rel in plan["write"] + [VERSION_FILE, MANIFEST_FILE]:
            if (app_dir / rel).exists():
                b = bdir / rel
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(app_dir / rel, b)
        (bdir / "_added.json").write_text(json.dumps(plan["add"], ensure_ascii=False, indent=1), encoding="utf-8")
    for rel in plan["kept"]:
        # 自分で直したので残したファイルの、新しい版の中身（見比べたいとき用）
        n = bdir / NEW_COPY / rel
        n.parent.mkdir(parents=True, exist_ok=True)
        n.write_bytes(files[rel])
    for rel in plan["write"] + plan["add"]:
        dst = app_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".updating")
        tmp.write_bytes(files[rel])
        os.replace(tmp, dst)
    # 今回入れた（または前から同じだった）ファイルの印を記録する。残したファイルは「新しい版の印」を記録するので、
    # 次の更新でも自分で直したものとして残る
    manifest = {}
    mf = app_dir / MANIFEST_FILE
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    for rel, data in files.items():
        if rel.startswith(NEVER_TOUCH):
            continue
        manifest[rel] = sha256(data)
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    if sha:
        (app_dir / VERSION_FILE).write_text(json.dumps(
            {"sha": sha, "date": date, "updated_at": _dt.datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=1), encoding="utf-8")
    plan["requirements_changed"] = old_req != _file_hash(app_dir / "requirements.txt")
    plan["setup_changed"] = old_setup is not None and old_setup != _file_hash(app_dir / "setup.ps1")
    plan["backup"] = str(bdir) if bdir.exists() else ""
    _prune_backups(app_dir)
    return plan


def _prune_backups(app_dir: Path) -> None:
    root = app_dir / BACKUP_DIR
    if not root.is_dir():
        return
    for d in sorted(p for p in root.iterdir() if p.is_dir())[:-KEEP_BACKUPS]:
        shutil.rmtree(d, ignore_errors=True)


def rollback(app_dir: Path = APP_DIR, log=print) -> Path:
    """いちばん新しいバックアップに戻す（そのとき追加されたファイルは消す）"""
    root = app_dir / BACKUP_DIR
    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not dirs:
        raise UpdateError("戻せるバックアップがありません。")
    b = dirs[-1]
    added = []
    if (b / "_added.json").exists():
        added = json.loads((b / "_added.json").read_text(encoding="utf-8"))
    n = 0
    for p in b.rglob("*"):
        if p.is_file() and p.name != "_added.json" and NEW_COPY not in p.relative_to(b).parts:
            rel = p.relative_to(b)
            dst = app_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            n += 1
    for rel in added:
        try:
            (app_dir / rel).unlink()
        except OSError:
            pass
    for f in (VERSION_FILE, MANIFEST_FILE):
        if not (b / f).exists():
            (app_dir / f).unlink(missing_ok=True)   # 更新の前は記録がなかった（次の確認はファイルの日付で）
    shutil.rmtree(b, ignore_errors=True)
    log(f"  {n}個のファイルを元に戻し、追加された{len(added)}個のファイルを消しました（{b.name} の状態）")
    return b


def record_manifest(app_dir: Path = APP_DIR) -> bool:
    """今のファイルの印を記録する（セットアップのときに1回だけ）。次の更新で「自分で直したか」が分かるようになる"""
    mf = app_dir / MANIFEST_FILE
    if mf.exists():
        return False
    manifest = {}
    for p in app_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(app_dir).as_posix()
        if rel.startswith(NEVER_TOUCH) or "__pycache__" in rel:
            continue
        if "/" in rel and not rel.startswith(("templates/", "tests/")):
            continue   # 直下のファイルと templates・tests だけ（素材の出力フォルダなどは見ない）
        h = _file_hash(p)
        if h:
            manifest[rel] = h
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    return True


def install_requirements(app_dir: Path, log=print) -> bool:
    """必要なライブラリを入れ直す（setup.bat の「ライブラリ」の段階と同じ）"""
    log("■ 必要なライブラリを確認・追加しています（数分かかることがあります）")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
           str(app_dir / "requirements.txt")]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", cwd=str(app_dir))
    for ln in p.stdout:
        ln = ln.rstrip()
        if ln and not ln.startswith("Requirement already satisfied"):
            log("    " + ln)
    return p.wait() == 0
