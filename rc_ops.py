"""運用まわりの共通部品：設定の保存場所・スリープ防止・二重起動の防止・書き出し済みパーツの使い回し・
「困ったときの情報」（ログ・環境・設定をまとめる。APIキーなどは伏せる）。

まとめて処理は rc_ops_queue.py / batch.py、自動更新は rc_ops_update.py / update.py にあります。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


# ============================================================
#  場所・小物
# ============================================================
def user_dir() -> Path:
    """このツールの利用者ごとのデータ置き場（順番待ちの一覧・困ったときの情報など）。

    アプリのフォルダは「最新版に更新」で上書きされるので、消えて困るものはここに置く。
    Windows: %APPDATA%\\AutoVideoEditor / それ以外: ~/.config/AutoVideoEditor
    """
    base = Path(os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "AutoVideoEditor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json_atomic(path: Path, data) -> None:
    """途中で止まっても壊れたファイルが残らないように、別名で書いてから置き換える"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # Windows でほかのプロセスが一瞬開いているとき
            import time
            time.sleep(0.05)
    os.replace(tmp, path)


def keep_awake(on: bool = True) -> bool:
    """処理中に Windows がスリープしないようにする（このプロセスが終われば自動で元に戻る）"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:  # noqa: BLE001
        return False


class SingleRun:
    """同じ処理を2つ同時に動かさないためのロック。プロセスが落ちればロックも自動で外れる。

        with SingleRun(path) as ok:
            if not ok: ...（ほかで実行中）
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fp = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.close()
            return False
        self.fp = fp
        return True

    def release(self) -> None:
        if not self.fp:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fp.close()
        self.fp = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def is_locked(path: Path) -> bool:
    """ほかのプロセスがロックを持っているか（自分で取れたらすぐ外す）"""
    if not Path(path).exists():
        return False
    lk = SingleRun(path)
    if lk.acquire():
        lk.release()
        return False
    return True


# ============================================================
#  仕上げ：書き出し済みの本編パーツの使い回し
# ============================================================
PART_RE = re.compile(r"^part_\d{4}_[0-9a-f]{12}\.mkv(\.done)?$")


def part_signature(cmd: list[str], paths) -> str:
    """本編パーツの中身を決めるもの（FFmpeg の指示と、元の素材ファイルの大きさ・更新日時）から短い印を作る"""
    h = hashlib.sha1()
    h.update("\0".join(cmd).encode("utf-8"))
    for p in paths:
        try:
            st = Path(p).stat()
            h.update(f"|{p}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8"))
        except OSError:
            h.update(f"|{p}|?".encode("utf-8"))
    return h.hexdigest()[:12]


def mark_part_done(path: Path) -> None:
    """パーツを最後まで書き出せた印（大きさを記録）。印がないパーツや大きさが違うパーツは使わない"""
    Path(str(path) + ".done").write_text(str(path.stat().st_size), encoding="utf-8")


def part_ok(path: Path, expected_sec: float) -> bool:
    """書き出し済みのパーツが最後まで書けているか（書き終わりの印・大きさ・長さで確かめる）"""
    done = Path(str(path) + ".done")
    if not path.is_file() or not done.is_file():
        return False
    try:
        if int(done.read_text(encoding="utf-8").strip()) != path.stat().st_size:
            return False
    except (OSError, ValueError):
        return False
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return abs(float(p.stdout.strip()) - expected_sec) < 0.25
    except ValueError:
        return False


def clean_work(work: Path) -> None:
    """作業フォルダの中身を消す。ただし最後まで書けた本編パーツ（part_xxxx_印.mkv）は残す。

    使えるかどうかは、書き出しのときに書き終わりの印・大きさ・長さで確かめる。消せないファイルがあれば OSError。
    """
    if not work.exists():
        return
    keep = set()
    for p in work.iterdir():
        if p.is_file() and PART_RE.match(p.name):
            pair = p.with_name(p.name[:-5]) if p.name.endswith(".done") else Path(str(p) + ".done")
            if pair.exists():   # パーツと書き終わりの印がそろっているものだけ残す
                keep.add(p.name)
    if not keep:
        shutil.rmtree(work)    # 使い回すものがなければ、これまでどおりフォルダごと消す
        return
    for p in list(work.iterdir()):
        if p.name in keep:
            continue
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def remove_unused_parts(work: Path, used: set[str]) -> None:
    used = set(used) | {n + ".done" for n in used}
    for p in work.glob("part_*.mkv*"):
        if p.name not in used:
            try:
                p.unlink()
            except OSError:
                pass


# ============================================================
#  困ったときの情報
# ============================================================
SECRET_WORDS = ("key", "token", "secret", "password", "passwd", "auth")
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),          # Claude の APIキー
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),            # Google の APIキー
    re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"),            # Google のアクセストークン
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),             # Google のリフレッシュトークン
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),             # Google のクライアントシークレット
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{12,}"),
]
HIDDEN = "（伏せています）"


def mask_text(text: str) -> str:
    """ログの中の APIキー・トークンと、Windowsのユーザー名（フォルダ名）を伏せる"""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + HIDDEN, text)
    # "api_key": "…" や ANTHROPIC_API_KEY=… の形も伏せる
    text = re.sub(r'(?i)("?[\w\-]*(?:api_key|apikey|token|secret|password)[\w\-]*"?\s*[:=]\s*)("?)[^\s",}]{6,}\2',
                  lambda m: m.group(1) + m.group(2) + HIDDEN + m.group(2), text)
    home = str(Path.home())
    if len(home) > 3:
        for h in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            text = text.replace(h, "%USERPROFILE%")
    return text


def mask_settings(settings: dict) -> dict:
    out = {}
    for k, v in (settings or {}).items():
        if any(w in str(k).lower() for w in SECRET_WORDS):
            out[k] = "設定済み" + HIDDEN if v else "未設定"
        else:
            out[k] = mask_text(str(v)) if isinstance(v, str) else v
    return out


def _cmd(args: list[str], timeout: float = 20) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"（確認できませんでした: {e.__class__.__name__}）"


def os_text() -> str:
    if os.name == "nt":
        rel, ver, _sp, _ = platform.win32_ver()
        try:
            build = int(ver.split(".")[2])
        except (IndexError, ValueError):
            build = 0
        name = "Windows 11" if build >= 22000 else f"Windows {rel}"
        ed = ""
        try:
            ed = platform.win32_edition() or ""
        except Exception:  # noqa: BLE001
            pass
        return f"{name} {ed}（ビルド {ver}）".replace("  ", " ")
    return platform.platform()


def gpu_text() -> str:
    if shutil.which("nvidia-smi"):
        s = _cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used",
                  "--format=csv,noheader"])
        if s and "確認できません" not in s:
            return "NVIDIA: " + s.replace("\n", " / ")
    if os.name == "nt":
        s = _cmd(["powershell", "-NoProfile", "-Command",
                  "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + ' (ドライバー ' + $_.DriverVersion + ')' }"])
        if s:
            return s.replace("\n", " / ") + "（nvidia-smi が見つかりません）"
    return "NVIDIAのGPUは見つかりませんでした（nvidia-smi がありません）"


def cuda_text() -> str:
    code = ("import ctranslate2 as c; print(c.__version__, c.get_cuda_device_count())")
    s = _cmd([sys.executable, "-c", code], timeout=60)
    parts = s.split()
    if len(parts) == 2 and parts[1].isdigit():
        n = int(parts[1])
        return f"ctranslate2 {parts[0]}：GPUを{n}台認識" if n else f"ctranslate2 {parts[0]}：GPUを使えません（CPUで文字起こし）"
    return "文字起こしのライブラリを読み込めませんでした: " + s.splitlines()[-1][:200] if s else "不明"


def ffmpeg_text() -> str:
    if not shutil.which("ffmpeg"):
        return "見つかりません（setup.bat を実行してください）"
    first = _cmd(["ffmpeg", "-hide_banner", "-version"]).splitlines()[:1]
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.2", "-c:v", "h264_nvenc",
                        "-f", "null", "-"], capture_output=True)
    nv = "使える" if p.returncode == 0 else "使えない（CPUで書き出し）"
    return f"{first[0] if first else '?'} / {shutil.which('ffmpeg')} / NVENC（GPUで書き出し）: {nv}"


def memory_text() -> str:
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            total, avail = m.ullTotalPhys, m.ullAvailPhys
        else:
            info = {}
            for ln in Path("/proc/meminfo").read_text().splitlines():
                k, v = ln.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
            total, avail = info["MemTotal"], info.get("MemAvailable", 0)
        return f"{total / 2**30:.1f}GB（空き {avail / 2**30:.1f}GB）"
    except Exception:  # noqa: BLE001
        return "不明"


def disk_text(p: Path) -> str:
    try:
        u = shutil.disk_usage(p)
        return f"{p.anchor or p}：空き {u.free / 2**30:.0f}GB / {u.total / 2**30:.0f}GB"
    except OSError:
        return "不明"


LIBS = ["faster-whisper", "ctranslate2", "anthropic", "PySide6", "numpy", "Pillow", "google-api-python-client",
        "google-auth-oauthlib", "nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def libs_text() -> str:
    from importlib import metadata
    out = []
    for name in LIBS:
        try:
            out.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            alt = {"PySide6": ("PySide6-Essentials", "PySide6")}.get(name)
            v = None
            if alt:
                try:
                    v = metadata.version(alt[0])
                except metadata.PackageNotFoundError:
                    try:
                        v = __import__(alt[1]).__version__
                    except Exception:  # noqa: BLE001
                        v = None
            out.append(f"{name} {v}" if v else f"{name} （入っていません）")
    return " / ".join(out)


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join((["…（前は省略。全部は zip に入っています）"] if len(lines) > n else []) + lines[-n:])


def collect(app_dir: Path = APP_DIR, gui_log: str = "", settings: dict | None = None,
            folder: Path | None = None, extra: dict | None = None) -> tuple[str, dict[str, str]]:
    """困ったときの情報をまとめる。(クリップボード用の文章, zipに入れる {名前: 中身}) を返す。

    gui_log: 画面の下の「ログ」の内容 / settings: 画面の設定 / folder: 今開いている ○○_roughcut フォルダ
    """
    import rc_ops_update
    ver = rc_ops_update.local_version(app_dir)
    env_lines = [
        f"- このツールの版: {rc_ops_update.version_text(ver)}",
        f"- 場所: {app_dir}",
        f"- OS: {os_text()}",
        f"- Python: {sys.version.split()[0]}（{sys.executable}）",
        f"- GPU: {gpu_text()}",
        f"- 文字起こしのGPU: {cuda_text()}",
        f"- FFmpeg: {ffmpeg_text()}",
        f"- メモリ: {memory_text()}",
        f"- ディスク: {disk_text(app_dir)}" + (f" / 素材側 {disk_text(folder)}" if folder else ""),
        f"- ライブラリ: {libs_text()}",
        f"- ANTHROPIC_API_KEY（環境変数）: {'設定済み' + HIDDEN if os.environ.get('ANTHROPIC_API_KEY') else '未設定'}",
    ]
    yt = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "ai-roughcut"
    env_lines.append(f"- YouTube連携: client_secret.json {'あり' if (yt / 'client_secret.json').exists() else 'なし'}"
                     f" / ログイン {'済み' if (yt / 'youtube_token.json').exists() else 'まだ'}")
    files: dict[str, str] = {}
    sections = ["# 困ったときの情報（AI動画編集ツール）", f"作成日時: {now_text()}", "",
                "## 環境", *env_lines]
    if settings is not None:
        ms = mask_settings(settings)
        sections += ["", "## 設定（APIキーなどは伏せています）",
                     *[f"- {k}: {v}" for k, v in sorted(ms.items(), key=lambda kv: str(kv[0]))]]
        files["設定.json"] = json.dumps(ms, ensure_ascii=False, indent=1)
    for k, v in (extra or {}).items():
        sections += ["", f"## {k}", mask_text(str(v))]
    err_log = app_dir / "gui_error.log"
    if err_log.exists():
        t = mask_text(err_log.read_text(encoding="utf-8", errors="replace"))
        files["ログ/gui_error.log"] = t
        sections += ["", "## gui_error.log（予期しないエラーの記録・最後の部分）", _tail(t, 40)]
    if gui_log.strip():
        t = mask_text(gui_log)
        files["ログ/画面のログ.txt"] = t
        sections += ["", "## 画面のログ（最後の部分）", _tail(t, 120)]
    if folder and Path(folder).is_dir():
        folder = Path(folder)
        listing = []
        for p in sorted(folder.iterdir()):
            if p.is_file():
                listing.append(f"{p.name}  {p.stat().st_size / 1e6:.1f}MB")
            elif p.is_dir():
                listing.append(f"{p.name}/")
        sections += ["", f"## 作業中のフォルダ: {mask_text(str(folder))}", *listing[:80]]
        bl = folder / "batch_log.txt"
        if bl.exists():
            t = mask_text(bl.read_text(encoding="utf-8", errors="replace"))
            files["ログ/batch_log.txt"] = t
            sections += ["", "## まとめて処理のログ（最後の部分）", _tail(t, 60)]
    q = user_dir() / "queue.json"
    if q.exists():
        files["queue.json"] = mask_text(q.read_text(encoding="utf-8", errors="replace"))
    text = "\n".join(sections) + "\n"
    files["困ったときの情報.txt"] = text
    return text, files


def save_zip(files: dict[str, str], dest_dir: Path | None = None) -> Path:
    dest_dir = Path(dest_dir) if dest_dir else user_dir() / "困ったときの情報"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = dest_dir / f"困ったときの情報_{stamp}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content.encode("utf-8"))
    # 古いものは10個まで残す
    old = sorted(dest_dir.glob("困ったときの情報_*.zip"))[:-10]
    for p in old:
        try:
            p.unlink()
        except OSError:
            pass
    return path
