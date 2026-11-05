"""全動画の発言検索。

指定したフォルダ以下にある荒編集の結果（○○_roughcut。session.json と chunks.json のあるフォルダ）を探し、
発言を SQLite の索引に入れて、動画をまたいで検索できるようにする。

- 日本語は単語の区切りがないので、SQLite の FTS5（trigram = 3文字ずつの索引）を使う。
  2文字以下の言葉や、FTS5 が使えない環境では LIKE（部分一致）で探す
- 全角・半角、カタカナ・ひらがなの違いは無視する（「ドリル」で「どりる」も見つかる）
- 2回目からは、変わったフォルダだけを読み直す（3時間の素材が何本あっても、1本ずつ読むのでメモリは少しで済む）
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from rc_preprod import fold, fold_char

APP_DIR = Path(__file__).resolve().parent
SKIP_DIRS = {"work", "thumbnails", "_preview_work", "node_modules", "$RECYCLE.BIN", "System Volume Information",
             "__pycache__", ".git", ".venv"}


def default_db() -> Path:
    env = os.environ.get("AVE_SEARCH_DB")
    return Path(env) if env else APP_DIR / "search_index.sqlite"


def _has_trigram(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._t USING fts5(x, tokenize='trigram')")
        con.execute("DROP TABLE temp._t")
        return True
    except sqlite3.Error:
        return False


class Index:
    def __init__(self, db: Path | str | None = None):
        self.path = Path(db) if db else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path))
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY, folder TEXT UNIQUE, stem TEXT, mode TEXT, title TEXT,
                inputs TEXT, stamp TEXT, n INTEGER, duration REAL, indexed_at TEXT);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, video_id INTEGER, cid INTEGER, start REAL, "end" REAL,
                text TEXT, norm TEXT, cut INTEGER);
            CREATE INDEX IF NOT EXISTS chunks_video ON chunks(video_id, cid);
        """)
        self.fts = _has_trigram(self.con)
        if self.fts:
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(norm, tokenize='trigram')")
        self.con.commit()

    def close(self):
        self.con.close()

    # ---------------------------------------------------------------- 索引づくり
    @staticmethod
    def find_folders(root: Path):
        """root 以下の荒編集フォルダを探す（見つけたフォルダの中までは探さない）"""
        root = Path(root)
        for cur, dirs, files in os.walk(root):
            if "session.json" in files and "chunks.json" in files:
                dirs[:] = []
                yield Path(cur)
                continue
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))

    @staticmethod
    def _stamp(folder: Path) -> str:
        parts = []
        for n in ("chunks.json", "session.json"):
            st = (folder / n).stat()
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        return "|".join(parts)

    def update(self, root: Path | str, progress=None) -> dict:
        """root 以下を索引に入れる。消えたフォルダは索引から外す。戻り値は件数のまとめ"""
        root = Path(root).resolve()
        found = list(self.find_folders(root))
        known = {r[0]: (r[1], r[2]) for r in self.con.execute("SELECT folder, id, stamp FROM videos")}
        stats = {"found": len(found), "added": 0, "updated": 0, "same": 0, "removed": 0, "failed": 0}
        seen = set()
        for i, f in enumerate(found, 1):
            key = str(f.resolve())
            seen.add(key)
            try:
                stamp = self._stamp(f)
                if key in known and known[key][1] == stamp:
                    stats["same"] += 1
                else:
                    self._index_folder(f, key, stamp, known.get(key, (None,))[0])
                    stats["updated" if key in known else "added"] += 1
            except Exception as e:  # noqa: BLE001  1本読めなくても、ほかは続ける
                stats["failed"] += 1
                print(f"  [注意] 読み込めませんでした: {f}（{e}）", flush=True)
            if progress:
                progress(i, len(found), f)
        prefix = str(root).rstrip("\\/") + os.sep
        for folder, (vid, _s) in known.items():
            if (folder.startswith(prefix) or folder == str(root)) and folder not in seen:
                self._delete_video(vid)
                stats["removed"] += 1
        self.con.commit()
        return stats

    def _delete_video(self, vid: int):
        if self.fts:
            self.con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE video_id=?)", (vid,))
        self.con.execute("DELETE FROM chunks WHERE video_id=?", (vid,))
        self.con.execute("DELETE FROM videos WHERE id=?", (vid,))

    def _index_folder(self, folder: Path, key: str, stamp: str, old_id: int | None):
        sess = json.loads((folder / "session.json").read_text(encoding="utf-8-sig"))
        chunks = json.loads((folder / "chunks.json").read_text(encoding="utf-8-sig"))
        corr = {int(k): v for k, v in (sess.get("corrections") or {}).items()}
        cut = set(sess.get("cut_ids", []))
        inputs = []
        for p in sess.get("inputs", []):
            inputs.append({"path": p, "duration": _duration(Path(p))})
        meta = sess.get("meta") or {}
        ym = _read(folder / "youtube_meta.json") or {}
        title = ym.get("title") or (meta.get("titles") or [""])[0]
        if old_id is not None:
            self._delete_video(old_id)
        cur = self.con.execute(
            "INSERT INTO videos(folder, stem, mode, title, inputs, stamp, n, duration, indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (key, sess.get("stem", folder.name), sess.get("mode", ""), title, json.dumps(inputs, ensure_ascii=False),
             stamp, len(chunks), chunks[-1]["end"] if chunks else 0, time.strftime("%Y-%m-%d %H:%M:%S")))
        vid = cur.lastrowid
        rows = []
        for c in chunks:
            text = corr.get(c["id"], c["text"])
            rows.append((vid, c["id"], c["start"], c["end"], text, fold(text), int(c["id"] in cut)))
        self.con.executemany('INSERT INTO chunks(video_id, cid, start, "end", text, norm, cut) VALUES(?,?,?,?,?,?,?)', rows)
        if self.fts:
            self.con.execute("INSERT INTO chunks_fts(rowid, norm) SELECT id, norm FROM chunks WHERE video_id=?", (vid,))

    # ---------------------------------------------------------------- 検索
    def stats(self) -> dict:
        v, n = self.con.execute("SELECT COUNT(*), COALESCE(SUM(n), 0) FROM videos").fetchone()
        return {"videos": v, "chunks": n, "fts": self.fts}

    def videos(self) -> list[dict]:
        cols = ["id", "folder", "stem", "mode", "title", "n", "duration", "indexed_at"]
        return [dict(zip(cols, r)) for r in self.con.execute(f"SELECT {', '.join(cols)} FROM videos ORDER BY folder")]

    def search(self, query: str, include_cut: bool = False, limit: int = 200, folder: str | None = None) -> list[dict]:
        terms = [fold(t) for t in query.split() if t.strip()]
        if not terms:
            return []
        where, params = [], []
        long_terms = [t for t in terms if len(t) >= 3]
        if self.fts and long_terms:
            match = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            where.append("c.id IN (SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?)")
            params.append(match)
            rest = [t for t in terms if len(t) < 3]
        else:
            rest = terms
        for t in rest:
            where.append("c.norm LIKE ? ESCAPE '\\'")
            params.append("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if not include_cut:
            where.append("c.cut = 0")
        if folder:
            where.append("v.folder = ?")
            params.append(str(folder))
        sql = (f'SELECT v.folder, v.stem, v.title, v.inputs, c.video_id, c.cid, c.start, c."end", c.text, c.cut '
               f"FROM chunks c JOIN videos v ON v.id = c.video_id WHERE {' AND '.join(where)} "
               f"ORDER BY v.folder, c.start LIMIT ?")
        params.append(int(limit))
        out = []
        for folder_, stem, title, inputs, vid, cid, s, e, text, cut in self.con.execute(sql, params):
            out.append({"folder": folder_, "stem": stem, "title": title or "", "inputs": json.loads(inputs or "[]"),
                        "video_id": vid, "id": cid, "start": s, "end": e, "text": text, "cut": bool(cut),
                        "spans": match_spans(text, terms)})
        self._context(out)
        return out

    def _context(self, rows: list[dict]):
        """前後の発言（話の流れが分かるように）"""
        for r in rows:
            got = dict(self.con.execute("SELECT cid, text FROM chunks WHERE video_id=? AND cid IN (?, ?)",
                                        (r["video_id"], r["id"] - 1, r["id"] + 1)).fetchall())
            r["before"], r["after"] = got.get(r["id"] - 1, ""), got.get(r["id"] + 1, "")


def match_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """元の文の中で、検索語に当たる部分（文字の位置）"""
    norm_chars = [fold_char(c) for c in text]
    pos, starts = 0, []
    for nc in norm_chars:
        starts.append(pos)
        pos += len(nc)
    norm = "".join(norm_chars)

    def to_orig(k: int) -> int:
        lo = 0
        for i, s in enumerate(starts):
            if s <= k:
                lo = i
            else:
                break
        return lo
    spans = []
    for t in terms:
        i = norm.find(t)
        while i >= 0 and t:
            a, b = to_orig(i), to_orig(i + len(t) - 1) + 1
            spans.append((a, b))
            i = norm.find(t, i + len(t))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def locate(inputs: list[dict], t: float) -> tuple[str | None, float]:
    """分割ファイルを1本として数えた時刻 t が、どのファイルの何秒目か"""
    start = 0.0
    for i, f in enumerate(inputs):
        d = f.get("duration")
        if d is None or i == len(inputs) - 1 or t < start + d:
            return f["path"], max(0.0, t - start)
        start += d
    return None, 0.0


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def _duration(p: Path) -> float | None:
    if not p.exists():
        return None
    try:
        from rc_media import probe
        return probe(p).duration
    except (Exception, SystemExit):  # noqa: BLE001  ffprobe が無い・壊れたファイルでも索引づくりは続ける
        return None
