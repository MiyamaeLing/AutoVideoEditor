"""YouTube への設定・アップロード。

おすすめの流れ（審査なしで使える）:
  1. 動画ファイルは YouTube Studio にドラッグ＆ドロップしてアップロードする
  2. このツールがその動画を見つけて、タイトル・概要欄（チャプター付き）・タグ・カテゴリ・サムネイル・字幕を設定する

APIで動画ファイルごとアップロードすることもできるが、Google の審査（audit）を通していない
プロジェクトからアップロードした動画は「非公開」にロックされる（YouTube の仕様）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
CATEGORIES = {"28": "科学と技術", "26": "ハウツーとスタイル", "22": "ブログ", "19": "旅行とイベント",
              "24": "エンターテイメント", "27": "教育", "10": "音楽", "2": "自動車と乗り物"}
MODE_CATEGORY = {"talk": "28", "craft": "26", "outdoor": "19"}


def config_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / ".config")
    d = base / "ai-roughcut"
    d.mkdir(parents=True, exist_ok=True)
    return d


def client_secret_path() -> Path:
    return config_dir() / "client_secret.json"


def token_path() -> Path:
    return config_dir() / "youtube_token.json"


# ---------------- 投稿情報 ----------------

def fmt_chapter(t: float) -> str:
    t = int(round(t))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def valid_chapters(chapters: list[dict], duration: float | None) -> list[dict]:
    """YouTube のチャプターの条件（最初が0:00・3つ以上・それぞれ10秒以上）に合わせる"""
    out = []
    for c in sorted(chapters, key=lambda c: c["t"]):
        t = 0.0 if not out else c["t"]
        if out and t - out[-1]["t"] < 10:
            continue
        if duration and t > duration - 10:
            continue
        out.append({"t": t, "title": c["title"].strip()})
    return out if len(out) >= 3 else []


def build_description(desc: str, chapters: list[dict], tags: list[str]) -> str:
    parts = [desc.strip()]
    if chapters:
        parts.append("▼ チャプター\n" + "\n".join(f"{fmt_chapter(c['t'])} {c['title']}" for c in chapters))
    hashtags = [("#" + re.sub(r"\s+", "", t)) for t in tags[:3] if t.strip()]
    if hashtags:
        parts.append(" ".join(hashtags))
    return "\n\n".join(p for p in parts if p)


CHAPTER_HEAD = "▼ チャプター"
# 画面やファイルで人が直す欄（完成動画を作り直しても引き継ぐ）
USER_FIELDS = ("title", "tags", "category_id", "privacy", "upload_captions", "thumbnail")


def source_stamp(out_dir: Path) -> str:
    """完成動画（final_info.json）とチャプターの中身から作る目印。作り直すと変わる"""
    import hashlib
    final = out_dir / "final_info.json"
    src = final if final.exists() else out_dir / "session.json"
    try:
        d = json.loads(src.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    key = {k: d.get(k) for k in ("video", "duration", "chapters", "srt")}
    return hashlib.sha1(json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def chapter_block(chapters: list[dict]) -> str:
    return (CHAPTER_HEAD + "\n" + "\n".join(f"{fmt_chapter(c['t'])} {c['title']}" for c in chapters)) if chapters else ""


def replace_chapters(desc: str, chapters: list[dict]) -> str:
    """概要欄の「▼ チャプター」の部分だけを、新しいチャプターに入れ替える（ほかの文はそのまま）"""
    parts = desc.split("\n\n")
    new = chapter_block(chapters)
    idx = next((i for i, p in enumerate(parts) if p.lstrip().startswith(CHAPTER_HEAD)), None)
    if idx is not None:
        if new:
            parts[idx] = new
        else:
            del parts[idx]
    elif new:
        # チャプターの欄がなければ、ハッシュタグの行の前（なければ最後）に入れる
        tag = next((i for i, p in enumerate(parts) if p.strip().startswith("#")), len(parts))
        parts.insert(tag, new)
    return "\n\n".join(p for p in parts if p.strip())


def default_meta(out_dir: Path, use_saved: bool = True) -> dict:
    """荒編集・仕上げの結果から、YouTube に設定する内容を作る。

    youtube_meta.json（画面で直した内容）があれば、それを使う。ただし完成動画が作り直されていたら、
    タイトル・タグなど人が直す欄だけを引き継ぎ、概要欄のチャプター・動画ファイル・字幕は作り直す。
    動画IDも、前の動画を指したままにならないように空にする（previous_video_id に残す）。
    use_saved=False なら、保存した内容を使わずに AI の案から作り直す。
    """
    fresh = _fresh_meta(out_dir)
    saved_p = out_dir / "youtube_meta.json"
    if not use_saved or not saved_p.exists():
        return fresh
    try:
        saved = json.loads(saved_p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return fresh
    saved.pop("notice", None)
    if saved.get("source_stamp"):
        changed = saved["source_stamp"] != fresh["source_stamp"]
    else:   # 目印のない古い保存ファイル：完成動画の情報の方が新しければ、作り直されたとみなす
        fi = out_dir / "final_info.json"
        changed = fi.exists() and fi.stat().st_mtime > saved_p.stat().st_mtime
    if not changed:
        return {**fresh, **saved}
    meta = dict(fresh)
    for k in USER_FIELDS:
        if k in saved:
            meta[k] = saved[k]
    if meta.get("thumbnail") and not Path(meta["thumbnail"]).exists():
        meta["thumbnail"] = fresh["thumbnail"]
    if "description" in saved:
        meta["description"] = replace_chapters(saved["description"], fresh["chapters"])
    if saved.get("video_id"):
        meta["previous_video_id"] = saved["video_id"]
    meta["video_id"] = ""
    meta["notice"] = ("完成動画が作り直されていたので、概要欄のチャプターを今の動画に合わせて更新しました。"
                      + ("前に設定した動画IDは空にしました（新しくアップした動画を探します）。" if saved.get("video_id") else ""))
    return meta


def _fresh_meta(out_dir: Path) -> dict:
    sess = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
    final = json.loads((out_dir / "final_info.json").read_text(encoding="utf-8")) \
        if (out_dir / "final_info.json").exists() else {}
    meta = sess.get("meta") or {}
    chapters = valid_chapters(final.get("chapters") or sess.get("chapters", []), final.get("duration"))
    tags = [t for t in meta.get("tags", []) if t.strip()]
    thumbs = sorted((out_dir / "thumbnails").glob("thumb_*.jpg"))
    stem = sess["stem"]
    video = final.get("video") or str(out_dir / f"{stem}_完成.mp4")
    return {
        "title": (meta.get("titles") or [stem])[0][:100],
        "title_candidates": meta.get("titles", []),
        "description": build_description(meta.get("description", ""), chapters, tags),
        "tags": tags,
        "category_id": MODE_CATEGORY.get(sess.get("mode"), "22"),
        "video": video,
        "thumbnail": str(next((t for t in thumbs if t.stem.endswith("_bottom")), thumbs[0])) if thumbs else "",
        "captions": final.get("srt", ""),
        "upload_captions": False,
        "privacy": "private",
        "video_id": "",
        "chapters": chapters,
        "source_stamp": source_stamp(out_dir),
    }


def save_meta(out_dir: Path, meta: dict) -> None:
    meta = {k: v for k, v in meta.items() if k != "notice"}
    meta.setdefault("source_stamp", source_stamp(out_dir))
    (out_dir / "youtube_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def check_meta(meta: dict) -> list[str]:
    """YouTube の制限に引っかかる所を日本語で返す"""
    errs = []
    if not meta["title"].strip():
        errs.append("タイトルが空です")
    if len(meta["title"]) > 100:
        errs.append(f"タイトルが長すぎます（{len(meta['title'])}文字 / 上限100文字）")
    if "<" in meta["title"] or ">" in meta["title"] or "<" in meta["description"] or ">" in meta["description"]:
        errs.append("タイトル・概要欄に「<」「>」は使えません")
    if len(meta["description"].encode("utf-8")) > 5000:
        errs.append("概要欄が長すぎます（上限5000バイト）")
    if sum(len(t) + 2 for t in meta["tags"]) > 500:
        errs.append("タグの合計が長すぎます（上限 約500文字）")
    th = meta.get("thumbnail")
    if th and Path(th).exists() and Path(th).stat().st_size > 2 * 1024 * 1024:
        errs.append("サムネイルが2MBを超えています")
    return errs


# ---------------- Google への接続 ----------------

def get_service(interactive: bool = True):
    """YouTube API に接続する。初回はブラウザで Google アカウントの許可を求める。"""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    tp = token_path()
    if tp.exists():
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None   # 期限切れ（テスト中のアプリは7日で切れる）→ もう一度ログイン
    if not creds or not creds.valid:
        if not interactive:
            raise SystemExit("[エラー] Googleアカウントとの連携が必要です。")
        cs = client_secret_path()
        if not cs.exists():
            raise SystemExit(f"[エラー] {cs} がありません。README の「YouTube連携の準備」を参照してください。")
        print("  ブラウザが開くので、Googleアカウントでログインして許可してください…", flush=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(cs), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent",
                                      success_message="連携できました。このタブは閉じて、ツールに戻ってください。")
        tp.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, static_discovery=True)


def channel_info(yt) -> dict:
    r = yt.channels().list(part="snippet,contentDetails", mine=True).execute()
    items = r.get("items", [])
    if not items:
        raise SystemExit("[エラー] このGoogleアカウントにはYouTubeチャンネルがありません。")
    ch = items[0]
    return {"id": ch["id"], "title": ch["snippet"]["title"],
            "uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"]}


def recent_uploads(yt, n: int = 10) -> list[dict]:
    """最近アップロードした動画（非公開・下書きも含む）"""
    ch = channel_info(yt)
    r = yt.playlistItems().list(part="snippet,contentDetails", playlistId=ch["uploads"], maxResults=n).execute()
    ids = [it["contentDetails"]["videoId"] for it in r.get("items", [])]
    if not ids:
        return []
    v = yt.videos().list(part="snippet,status,processingDetails", id=",".join(ids)).execute()
    out = []
    for it in v.get("items", []):
        out.append({"id": it["id"], "title": it["snippet"]["title"],
                    "published": it["snippet"].get("publishedAt", ""),
                    "privacy": it["status"].get("privacyStatus", ""),
                    "upload_status": it["status"].get("uploadStatus", "")})
    order = {vid: i for i, vid in enumerate(ids)}
    return sorted(out, key=lambda x: order.get(x["id"], 99))


def guess_video(uploads: list[dict], video_path: str) -> dict | None:
    """Studio でアップロードした直後は、タイトルがファイル名（拡張子なし）になっている"""
    stem = Path(video_path).stem
    for u in uploads:
        if u["title"].strip() == stem:
            return u
    return None


def apply_meta(yt, video_id: str, meta: dict, log=print) -> None:
    """アップロード済みの動画に、タイトル・概要欄・タグ・カテゴリ・サムネイル・字幕を設定する"""
    from googleapiclient.http import MediaFileUpload

    errs = check_meta(meta)
    if errs:
        raise SystemExit("[エラー] " + " / ".join(errs))
    log("  タイトル・概要欄・タグを設定しています…")
    yt.videos().update(part="snippet", body={
        "id": video_id,
        "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                    "categoryId": meta["category_id"], "defaultLanguage": "ja", "defaultAudioLanguage": "ja"},
    }).execute()
    th = meta.get("thumbnail")
    if th and Path(th).exists():
        log("  サムネイルを設定しています…")
        try:
            yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(th, mimetype="image/jpeg")).execute()
        except Exception as e:  # noqa: BLE001
            log(f"  [注意] サムネイルを設定できませんでした（チャンネルの電話番号確認が必要な場合があります）: {e}")
    cap = meta.get("captions")
    if meta.get("upload_captions") and cap and Path(cap).exists():
        log("  字幕を追加しています…")
        yt.captions().insert(part="snippet", body={
            "snippet": {"videoId": video_id, "language": "ja", "name": "日本語", "isDraft": False}},
            media_body=MediaFileUpload(cap, mimetype="application/octet-stream")).execute()
    log(f"  完了: https://studio.youtube.com/video/{video_id}/edit")


def upload_video(yt, meta: dict, log=print) -> str:
    """APIで動画ファイルごとアップロードする（審査前のプロジェクトでは非公開にロックされる）"""
    from googleapiclient.http import MediaFileUpload

    errs = check_meta(meta)
    if errs:
        raise SystemExit("[エラー] " + " / ".join(errs))
    path = meta["video"]
    if not Path(path).exists():
        raise SystemExit(f"[エラー] 動画が見つかりません: {path}")
    body = {
        "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                    "categoryId": meta["category_id"], "defaultLanguage": "ja", "defaultAudioLanguage": "ja"},
        "status": {"privacyStatus": meta.get("privacy", "private"), "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(path, chunksize=16 * 1024 * 1024, resumable=True, mimetype="video/*")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    log(f"  アップロード中: {Path(path).name}", )
    resp, last = None, -10
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct >= last + 5:
                last = pct
                log(f"      {pct}%")
    vid = resp["id"]
    log(f"  アップロード完了: {vid}")
    return vid


if __name__ == "__main__":
    sys.exit("youtube.py から使ってください。")
