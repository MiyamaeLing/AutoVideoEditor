"""サムネイルの強化：人物の切り抜きデザイン（person）と、顔を見た候補選び。

- 顔の検出: OpenCV に入っている顔・目の検出器（Haar）を使う。追加のダウンロードなし・GPUなしで動く
    * 顔が大きく写っている・目が開いている・表情がほかの場面と違う（笑顔など）画像を優先する
    * 同じ場面の少しずらした3枚からは「目が開いていて、くっきりした」1枚を選ぶ
- 人物の切り抜き:
    1) 人物の形を見分けるデータ（PP-HumanSeg、約6MB。OpenCV だけで動く）を初回に自動でダウンロードして使う
    2) ダウンロードできないときは、顔の位置から体の範囲を推定して切り抜く（GrabCut）
    3) OpenCV が使えない・顔が見つからない画像では、人物のデザインを作らない（ほかのデザインだけになる）
- 背景のバリエーション: blur=ぼかし / vivid=鮮やか / burst=集中線 / dark=暗く / color=単色

設定はテンプレートの [thumbnail.pro]（template.toml の末尾）。
"""
from __future__ import annotations

import math
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

try:
    import cv2
    cv2.setNumThreads(max(1, (os.cpu_count() or 2) // 2))
except Exception:  # noqa: BLE001  # OpenCV が入っていない・壊れている
    cv2 = None

APP_DIR = Path(__file__).resolve().parent
MODEL_NAME = "human_segmentation_pphumanseg_2023mar.onnx"
MODEL_URLS = [
    "https://github.com/opencv/opencv_zoo/raw/main/models/human_segmentation_pphumanseg/" + MODEL_NAME,
    "https://huggingface.co/opencv/human_segmentation_pphumanseg/resolve/main/" + MODEL_NAME,
]
MODEL_INPUT = 192
BACKGROUNDS = ("blur", "vivid", "burst", "dark", "color")
MIN_MODEL_BYTES = 100_000       # これより小さいファイルは、ダウンロードの失敗とみなす
SURE_FACE_WEIGHT = 4.5        # 目が見つからなくても、これ以上なら顔とみなして切り抜く
MIN_FACE_WEIGHT = 2.0         # 顔の検出の自信の下限（低いと服の模様なども顔と間違える）

DEFAULTS = {
    "person": True,               # 人物の切り抜きデザインを作る
    "face_pick": True,            # 顔を見て候補を選ぶ
    "model": "auto",              # auto=初回に自動ダウンロード / off=使わない / ファイルのパス
    "backgrounds": ["blur", "vivid", "burst"],
    "side": "auto",               # 人物を置く側 auto / left / right
    "person_height": 0.96,        # 人物の高さ（画面の高さに対する割合）
    "max_zoom": 2.2,              # 人物を拡大する上限（大きくしすぎてぼやけないように）
    "outline": 12,                # 白いフチの太さ（px）
    "outline_color": "#FFFFFF",
    "shadow": True,               # 人物の後ろに影を付ける
    "burst_color": "",            # 集中線・単色の背景の色（空なら accent_color）
    "other_background": "none",   # ほかのデザイン（bottom/band/side）の背景: none / blur / vivid
    "save_cutout": True,          # 切り抜いた人物を透過PNG（cutout_*.png）でも保存する
}

_NOTICE: set[str] = set()


def _note(msg: str):
    if msg not in _NOTICE:
        _NOTICE.add(msg)
        print(f"  {msg}", flush=True)


def available() -> bool:
    return cv2 is not None


def settings(tpl) -> dict:
    s = dict(DEFAULTS)
    s.update((tpl.get("thumbnail", {}) or {}).get("pro", {}) or {})
    bgs = s.get("backgrounds") or ["blur"]
    if isinstance(bgs, str):
        bgs = [bgs]
    s["backgrounds"] = [b for b in bgs if b in BACKGROUNDS] or ["blur"]
    return s


def _hex(c: str, default=(43, 108, 176)):
    try:
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return default


# ================================================================ 顔の検出・候補の点数

_cascades: dict[str, object] = {}


def _cascade(name: str):
    """日本語を含むフォルダでも読めるように、ファイルは Python で読んで中身を渡す
    （Windows の OpenCV は、ASCII 以外の文字を含むパスを開けないため。rc_shorts.face_detector と同じ方法）"""
    if name not in _cascades:
        c = None
        try:
            xml = (Path(cv2.data.haarcascades) / name).read_text(encoding="utf-8")
            fs = cv2.FileStorage(xml, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
            c = cv2.CascadeClassifier()
            if not c.read(fs.getFirstTopLevelNode()) or c.empty():
                c = None
        except Exception:  # noqa: BLE001
            c = None
        if c is None:
            _note("[注意] 顔の検出に使うデータを読み込めませんでした。顔で候補を選ぶ処理と、人物の切り抜きは使いません。")
        _cascades[name] = c
    return _cascades[name]


def _read_onnx(path) -> object:
    """ONNX のモデルを、ファイルの中身（バイト列）から読む（日本語を含むパスでも読めるように）"""
    return cv2.dnn.readNetFromONNX(np.frombuffer(Path(path).read_bytes(), np.uint8))


def detect_faces(img: Image.Image) -> list[dict]:
    """顔を主役らしい順（大きくて確かな順）に返す。box は元画像の座標 (x, y, w, h)、eyes は見つかった目の数（0〜2）。"""
    if cv2 is None:
        return []
    fc = _cascade("haarcascade_frontalface_default.xml")
    if fc is None:
        return []
    W0, H0 = img.size
    s = 960 / W0 if W0 > 960 else 1.0
    g = np.asarray(img.convert("L").resize((max(1, int(W0 * s)), max(1, int(H0 * s)))))
    g = cv2.createCLAHE(2.0, (8, 8)).apply(g)     # 明るさのムラをならす（逆光・暗い部屋でも見つけやすく）
    mn = max(16, int(g.shape[0] * 0.035))
    try:
        boxes, _levels, weights = fc.detectMultiScale3(g, 1.1, 4, minSize=(mn, mn), outputRejectLevels=True)
    except cv2.error:
        return []
    # 自信のない検出（服の模様などの誤検出）を除き、重なった検出は自信の高い方だけ残す
    cand = sorted(((float(wt), [int(v) for v in b]) for b, wt in zip(boxes, np.ravel(weights))
                   if wt >= MIN_FACE_WEIGHT), reverse=True)
    kept: list[tuple[float, list[int]]] = []
    for wt, b in cand:
        if all(_overlap(b, k) < 0.25 for _, k in kept):
            kept.append((wt, b))
    ec = _cascade("haarcascade_eye.xml")
    sc = _cascade("haarcascade_smile.xml")
    faces = []
    for wt, (x, y, w, h) in kept:
        roi = g[y:y + int(h * 0.58), x:x + w]
        eyes = 0
        if ec is not None and roi.size:
            es = ec.detectMultiScale(roi, 1.1, 5, minSize=(max(6, w // 9), max(6, w // 9)), maxSize=(w // 2, w // 2))
            eyes = min(2, len(es))
        smile = False
        low = g[y + int(h * 0.55):y + h, x:x + w]
        if sc is not None and low.size:
            smile = len(sc.detectMultiScale(low, 1.1, 20, minSize=(w // 4, h // 10))) > 0
        crop = cv2.resize(g[y:y + h, x:x + w], (48, 48))
        faces.append({"box": [int(x / s), int(y / s), int(w / s), int(h / s)], "eyes": eyes, "smile": smile,
                      "weight": wt, "size": float(h / g.shape[0]), "crop": crop})
    # 大きくて自信の高い顔を先に（主役の顔）
    faces.sort(key=lambda f: -f["box"][2] * f["box"][3] * min(f["weight"], 6.0))
    return faces


def _overlap(a, b) -> float:
    """2つの枠の重なり（小さい方の面積に対する割合）"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter / max(1, min(a[2] * a[3], b[2] * b[3]))


def frame_key(img: Image.Image, sharp: float) -> float:
    """同じ場面の少しずらした数枚から1枚を選ぶときの点数。目が開いている顔を強く優先する。"""
    faces = detect_faces(img)
    if not faces:
        return sharp
    f = faces[0]
    return sharp * (1.3 + 0.6 * f["eyes"] + (0.3 if f["smile"] else 0.0))


def annotate(cands: list[dict]) -> list[dict]:
    """候補に顔の情報と点数（score）を付ける。点数が高いほどサムネイル向き。"""
    if cv2 is None or not cands:
        return cands
    med = float(np.median([c["sharp"] for c in cands])) or 1.0
    crops = []
    for c in cands:
        faces = detect_faces(Image.open(c["path"]))
        c["face"] = None
        if faces:
            f = faces[0]
            c["face"] = {k: f[k] for k in ("box", "eyes", "smile", "size", "weight")}
            crops.append((c, f["crop"].astype(np.float32)))
    # 表情の変化: 顔の下半分（口・ほお）が、ほかの場面の平均からどれだけ違うか
    if len(crops) >= 2:
        mean = np.mean([cr for _, cr in crops], axis=0)
        devs = [float(np.abs(cr[22:, :] - mean[22:, :]).mean()) for _, cr in crops]
        top = max(devs) or 1.0
        for (c, _), d in zip(crops, devs):
            c["face"]["expression"] = round(d / top, 3)
    for c in cands:
        f = c.get("face")
        s = min(c["sharp"] / med, 1.5) * 0.5
        if f:
            s += 1.0 + min(f["size"], 0.45) * 4.0 + 0.45 * f["eyes"] + 0.6 * f.get("expression", 0.0) \
                + (0.4 if f["smile"] else 0.0)
        if c.get("why", "全体から") != "全体から":
            s += 0.8     # 見どころ・テロップの場面
        c["score"] = round(s, 3)
    return cands


def order(cands: list[dict]) -> list[dict]:
    """AIなしで選ぶときの順番（点数の高い順）"""
    return sorted(cands, key=lambda c: -c.get("score", 0.0))


def describe(c: dict) -> str:
    """AIに候補を見せるときの補足"""
    f = c.get("face")
    if f is None:
        return "顔: 見つからない" if "face" in c else ""
    parts = ["顔: 大きく写っている" if f["size"] >= 0.25 else "顔: 写っている"]
    parts.append({2: "目が開いている", 1: "目が片方だけ見える"}.get(f["eyes"], "目が見えない"))
    if f["smile"]:
        parts.append("笑顔")
    if f.get("expression", 0) >= 0.8:
        parts.append("表情の変化が大きい")
    return "・".join(parts)


# ================================================================ 人物の切り抜き

def model_dir() -> Path:
    if os.environ.get("AVE_MODEL_DIR"):
        return Path(os.environ["AVE_MODEL_DIR"])
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "AutoVideoEditor" / "models"


def _valid_model(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > MIN_MODEL_BYTES
    except OSError:
        return False


def download_model(force: bool = False, timeout: float = 30.0) -> Path | None:
    """人物の形を見分けるデータを、初回だけダウンロードする。失敗したら None（1日は再試行しない）。"""
    d = model_dir()
    dst = d / MODEL_NAME
    if _valid_model(dst):
        return dst
    fail = d / "download_failed.txt"
    if not force and fail.exists() and time.time() - fail.stat().st_mtime < 86400:
        return None
    d.mkdir(parents=True, exist_ok=True)
    print("  人物の切り抜きに使うデータ（約6MB）を初回だけダウンロードしています…", flush=True)
    err = ""
    for url in MODEL_URLS:
        part = dst.with_suffix(".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AutoVideoEditor"})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as fp:
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    fp.write(b)
            if not _valid_model(part):
                raise OSError("ダウンロードしたファイルが小さすぎます")
            if cv2 is not None:
                _read_onnx(part)     # 読めることを確かめる
            os.replace(part, dst)
            fail.unlink(missing_ok=True)
            print(f"  ダウンロードしました: {dst}")
            return dst
        except Exception as e:  # noqa: BLE001
            err = str(e)
            part.unlink(missing_ok=True)
    fail.write_text(err, encoding="utf-8")
    print(f"  [注意] ダウンロードできませんでした（{err}）。顔の位置から切り抜く簡易な方法を使います。")
    return None


def find_model(cfg: dict, allow_download: bool = True) -> Path | None:
    m = str(cfg.get("model", "auto") or "off")
    if m == "off":
        return None
    if m != "auto":
        p = Path(m)
        if not p.is_absolute():
            p = APP_DIR / p
        if _valid_model(p):
            return p
        _note(f"[注意] 人物の切り抜きデータが見つかりません: {p}")
        return None
    for p in (APP_DIR / "models" / MODEL_NAME, model_dir() / MODEL_NAME):
        if _valid_model(p):
            return p
    return download_model() if allow_download else None


_nets: dict[str, object] = {}


def _model_prob(rgb: np.ndarray, model: Path) -> np.ndarray | None:
    """人物らしさ（0〜1）の地図を返す"""
    key = str(model)
    try:
        if key not in _nets:
            _nets[key] = _read_onnx(key)
        net = _nets[key]
        x = cv2.resize(rgb, (MODEL_INPUT, MODEL_INPUT), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        x = (x - 0.5) / 0.5
        net.setInput(cv2.dnn.blobFromImage(x))
        out = np.asarray(net.forward())[0].astype(np.float32)      # (2, 192, 192)
    except Exception as e:  # noqa: BLE001
        _note(f"[注意] 人物の切り抜きデータを使えませんでした（{e}）。簡易な方法で切り抜きます。")
        _nets[key] = None
        return None
    if out.ndim != 3 or out.shape[0] < 2:
        return None
    if out.min() < -1e-3 or out.max() > 1 + 1e-3 or not np.allclose(out[:2].sum(0), 1, atol=0.05):
        e = np.exp(out[:2] - out[:2].max(0, keepdims=True))
        out = e / e.sum(0, keepdims=True)
    return cv2.resize(out[1], (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def _face_prior(shape, face) -> np.ndarray:
    """顔の位置から、GrabCut の初期の印（前景・背景）を作る"""
    h, w = shape[:2]
    fx, fy, fw, fh = face
    cx = fx + fw / 2
    m = np.full((h, w), cv2.GC_BGD, np.uint8)
    # 人物がいそうな広めの範囲（頭の上から下の端まで、下に行くほど広い）
    top = max(0, int(fy - fh * 0.8))
    wide = np.array([[cx - fw * 1.2, top], [cx + fw * 1.2, top], [cx + fw * 1.7, fy + fh * 1.2],
                     [cx + fw * 2.7, h], [cx - fw * 2.7, h], [cx - fw * 1.7, fy + fh * 1.2]], np.int32)
    cv2.fillPoly(m, [wide], cv2.GC_PR_BGD)
    # たぶん人物（頭と、肩から下）
    cv2.ellipse(m, (int(cx), int(fy + fh * 0.4)), (int(fw * 0.75), int(fh * 0.98)), 0, 0, 360, cv2.GC_PR_FGD, -1)
    body = np.array([[cx - fw * 0.55, fy + fh * 0.95], [cx + fw * 0.55, fy + fh * 0.95],
                     [cx + fw * 1.5, fy + fh * 1.45], [cx + fw * 2.0, h], [cx - fw * 2.0, h],
                     [cx - fw * 1.5, fy + fh * 1.45]], np.int32)
    cv2.fillPoly(m, [body], cv2.GC_PR_FGD)
    # 確実に人物（髪の毛を含めた頭と、首の下）。髪が背景と似た色でも切れないように、頭は広めに決めておく
    cv2.ellipse(m, (int(cx), int(fy + fh * 0.36)), (int(fw * 0.46), int(fh * 0.74)), 0, 0, 360, cv2.GC_FGD, -1)
    cv2.rectangle(m, (int(cx - fw * 0.35), int(fy + fh * 1.25)), (int(cx + fw * 0.35), int(min(h, fy + fh * 2.4))),
                  cv2.GC_FGD, -1)
    return m


def _clean(fg: np.ndarray, face_pt) -> np.ndarray:
    """顔につながった一番大きなかたまりだけを残し、穴を埋めて、ふちを滑らかにする"""
    fg = fg.astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n <= 1:
        return fg
    keep = 0
    if face_pt is not None:
        x, y = int(face_pt[0]), int(face_pt[1])
        if 0 <= y < lab.shape[0] and 0 <= x < lab.shape[1]:
            keep = lab[y, x]
    if keep == 0:
        keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    fg = (lab == keep).astype(np.uint8)
    # 穴埋め（外側から塗りつぶせない所は人物の内側）
    pad = cv2.copyMakeBorder(fg, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    flood = pad.copy()
    cv2.floodFill(flood, None, (0, 0), 1)
    holes = (flood == 0)[1:-1, 1:-1]
    fg[holes] = 1
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    return fg


def person_mask(img: Image.Image, face: dict | None, cfg: dict, model: Path | None) -> np.ndarray | None:
    """人物の透明度（0〜1, 画像と同じ大きさ）。うまく切り抜けなければ None。"""
    if cv2 is None:
        return None
    rgb = np.asarray(img.convert("RGB"))
    H0, W0 = rgb.shape[:2]
    if face is None:
        return None     # 顔の位置を手がかりにするので、顔が見つからない画像では切り抜かない
    if face.get("eyes", 0) == 0 and face.get("weight", 99) < SURE_FACE_WEIGHT:
        return None     # 目が見えず、顔かどうか自信がない（服の模様などの誤検出で変に切り抜かないように）
    prob = _model_prob(rgb, model) if model else None
    s = 540 / H0 if H0 > 540 else 1.0
    sw, sh = int(W0 * s), int(H0 * s)
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    fbox = [v * s for v in face["box"]]
    if prob is not None:
        p = cv2.resize(prob, (sw, sh))
        m = np.full((sh, sw), cv2.GC_PR_BGD, np.uint8)
        m[p > 0.5] = cv2.GC_PR_FGD
        m[p > 0.93] = cv2.GC_FGD
        m[p < 0.04] = cv2.GC_BGD
        fx, fy, fw, fh = fbox
        cv2.ellipse(m, (int(fx + fw / 2), int(fy + fh * 0.5)), (int(fw * 0.3), int(fh * 0.38)), 0, 0, 360,
                    cv2.GC_FGD, -1)
    else:
        m = _face_prior((sh, sw), fbox)
    if not ((m == cv2.GC_FGD) | (m == cv2.GC_PR_FGD)).any() or not ((m == cv2.GC_BGD) | (m == cv2.GC_PR_BGD)).any():
        return None
    room = int((m != cv2.GC_BGD).sum())     # 人物がいてもよい範囲の広さ
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), m, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        if prob is None:
            return None
        m = np.where(cv2.resize(prob, (sw, sh)) > 0.5, cv2.GC_FGD, cv2.GC_BGD).astype(np.uint8)
    fg = (m == cv2.GC_FGD) | (m == cv2.GC_PR_FGD)
    face_pt = (fbox[0] + fbox[2] / 2, fbox[1] + fbox[3] / 2)
    fg = _clean(fg, face_pt)
    area = float(fg.mean())
    if not 0.03 <= area <= 0.85:
        return None
    if not fg[min(sh - 1, int(face_pt[1])), min(sw - 1, int(face_pt[0]))]:
        return None
    # 人物の形をとらえられなかった（範囲いっぱい・ほぼ四角に切れた。画面の中の画面など）ものは使わない
    ys, xs = np.nonzero(fg)
    if fg.sum() / max(1, (np.ptp(xs) + 1) * (np.ptp(ys) + 1)) > 0.93 or (prob is None and fg.sum() > room * 0.88):
        return None
    a = cv2.resize(fg.astype(np.float32), (W0, H0), interpolation=cv2.INTER_LINEAR)
    a = cv2.GaussianBlur(a, (0, 0), 1.2)
    return np.clip((a - 0.5) * 2.2 + 0.5, 0, 1)


# ================================================================ 合成

def background(frame: Image.Image, style: str, accent, focus=(0.7, 0.45)) -> Image.Image:
    """背景のバリエーション。frame は 1280x720 の画像。"""
    W, H = frame.size
    if style == "blur":
        sm = frame.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(5))
        img = sm.resize((W, H), Image.BICUBIC)
        img = ImageEnhance.Color(img).enhance(1.35)
        return ImageEnhance.Brightness(img).enhance(0.82)
    if style == "vivid":
        img = ImageEnhance.Color(frame).enhance(1.75)
        img = ImageEnhance.Contrast(img).enhance(1.18)
        return _vignette(img, 0.35)
    if style == "dark":
        img = ImageEnhance.Color(frame).enhance(0.25)
        img = ImageEnhance.Brightness(img).enhance(0.45)
        return _vignette(img, 0.4)
    # burst（集中線）/ color（単色のグラデーション）
    cx, cy = focus[0] * W, focus[1] * H
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / math.hypot(W, H)
    base = np.array(accent, np.float32)
    light = base + (255 - base) * 0.45
    t = np.clip(r * 1.6, 0, 1)[..., None]
    arr = light * (1 - t) + base * 0.55 * t
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    if style == "burst":
        d = ImageDraw.Draw(img, "RGBA")
        rng = np.random.default_rng(7)
        R = math.hypot(W, H)
        ang = 0.0
        while ang < 2 * math.pi:
            step = rng.uniform(0.035, 0.09)
            w = step * rng.uniform(0.25, 0.5)
            r0 = R * rng.uniform(0.18, 0.3)
            pts = [(cx + r0 * math.cos(ang), cy + r0 * math.sin(ang)),
                   (cx + R * math.cos(ang - w), cy + R * math.sin(ang - w)),
                   (cx + R * math.cos(ang + w), cy + R * math.sin(ang + w))]
            d.polygon(pts, fill=(255, 255, 255, int(rng.uniform(60, 120))))
            ang += step
    return img


def _vignette(img: Image.Image, strength: float) -> Image.Image:
    W, H = img.size
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
    k = 1 - strength * np.clip(r, 0, 1) ** 2
    return Image.fromarray((np.asarray(img, np.float32) * k[..., None]).clip(0, 255).astype(np.uint8))


def portrait(frame: Image.Image, alpha: np.ndarray, style: str) -> Image.Image:
    """人物はそのままで、背景だけをぼかす・鮮やかにする（ほかのデザイン用）"""
    if style not in ("blur", "vivid", "dark"):
        return frame
    bg = background(frame.convert("RGB"), style, (0, 0, 0))
    a = Image.fromarray((alpha * 255).astype(np.uint8))
    return Image.composite(frame.convert("RGB"), bg, a)


def cutout(frame: Image.Image, alpha: np.ndarray) -> Image.Image:
    """切り抜いた人物（透過PNG用、人物の範囲だけ）"""
    rgba = frame.convert("RGBA")
    rgba.putalpha(Image.fromarray((alpha * 255).astype(np.uint8)))
    box = Image.fromarray((alpha > 0.05).astype(np.uint8) * 255).getbbox()
    return rgba.crop(box) if box else rgba


def compose_person(frame: Image.Image, alpha: np.ndarray, text: str, style: str, tpl, font,
                   cfg: dict | None = None) -> Image.Image:
    """人物を大きく配置し、白いフチを付けて、反対側に大きな文字を置く"""
    import rc_thumb as T
    cfg = cfg or settings(tpl)
    W, H = T.W, T.H
    frame = frame.convert("RGB").resize((W, H))
    alpha = cv2.resize(alpha.astype(np.float32), (W, H)) if alpha.shape != (H, W) else alpha
    st = T.text_style(tpl)   # 文字の見せ方（上品・ポップ・シンプル）は、ほかのサムネイル案と同じにする
    accent = _hex(cfg.get("burst_color")) if cfg.get("burst_color") else T._accent(frame, st)

    ys, xs = np.nonzero(alpha > 0.5)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    side = cfg.get("side", "auto")
    if side not in ("left", "right"):
        side = "left" if (x0 + x1) / 2 < W * 0.45 else "right"
    # 人物の大きさ: 高さを画面の person_height に（横幅は画面の62%まで、拡大は max_zoom 倍まで）
    zoom = min(H * float(cfg.get("person_height", 0.96)) / (y1 - y0), W * 0.62 / (x1 - x0),
               float(cfg.get("max_zoom", 2.2)))
    zoom = max(zoom, 0.5)
    pw, ph = max(1, int((x1 - x0) * zoom)), max(1, int((y1 - y0) * zoom))
    person = frame.crop((x0, y0, x1, y1)).resize((pw, ph), Image.LANCZOS)
    pa = cv2.resize(alpha[y0:y1, x0:x1], (pw, ph), interpolation=cv2.INTER_LINEAR)
    # 画面の下の端で切れている人物は、下の端にそろえる
    py = H - ph if y1 >= H - 4 or ph >= H * 0.85 else int(H - ph - H * 0.02)
    margin = int(W * 0.03)
    px = W - pw - margin if side == "right" else margin
    px = max(-pw // 4, min(W - pw * 3 // 4, px))

    # 背景の映像も人物と同じように拡大・移動して、元の人物が切り抜きの真後ろに隠れるようにする
    bgsrc = _moved(frame, zoom, px - x0 * zoom, py - y0 * zoom) if style in ("blur", "vivid", "dark") else frame
    canvas = background(bgsrc, style, accent, focus=((px + pw / 2) / W, max(0.2, (py + ph * 0.3) / H)))
    canvas = canvas.convert("RGBA")
    full_a = np.zeros((H, W), np.float32)
    _paste_arr(full_a, pa, px, py)
    ol = int(cfg.get("outline", 12))
    if ol > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * ol + 1, 2 * ol + 1))
        edge = cv2.dilate((full_a > 0.5).astype(np.uint8), k).astype(np.float32)
        edge = cv2.GaussianBlur(edge, (0, 0), 1.0)
    else:
        edge = full_a
    if cfg.get("shadow", True):
        sh = cv2.GaussianBlur(edge, (0, 0), 10)
        sh = _shift(sh, 8 if side == "left" else -8, 10)
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow.putalpha(Image.fromarray((sh * 150).clip(0, 255).astype(np.uint8)))
        canvas = Image.alpha_composite(canvas, shadow)
    if ol > 0:
        white = Image.new("RGBA", (W, H), _hex(cfg.get("outline_color", "#FFFFFF"), (255, 255, 255)) + (255,))
        white.putalpha(Image.fromarray((edge * 255).clip(0, 255).astype(np.uint8)))
        canvas = Image.alpha_composite(canvas, white)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.paste(ImageEnhance.Color(ImageEnhance.Contrast(person).enhance(1.08)).enhance(1.15), (px, py))
    layer.putalpha(Image.fromarray((full_a * 255).clip(0, 255).astype(np.uint8)))
    canvas = Image.alpha_composite(canvas, layer).convert("RGB")

    # 文字（人物の反対側。人物と少し重なってもよい）
    lines = T._lines(text, one_line_max=4)
    fp, idx = font
    free_w = int(W - pw - margin * 2 + pw * 0.12) if px > W * 0.2 or px < 0 else int(W * 0.5)
    free_w = max(int(W * 0.42), min(int(W * 0.62), free_w))
    # 文字が右なら、右下の再生時間の表示にかからない高さに収める
    max_h = H - 120 if side == "right" else T.TIME_BOX[1] - T.TIME_GAP - 40
    f, size = T._fit(fp, idx, lines, free_w - 60, max_h, start=210, pad=T._pad(st))
    total_h = int(size * 1.15) * len(lines)
    tx = 40 if side == "right" else W - free_w + 20
    ty = (H - total_h) / 2 - size * 0.05
    ty = max(24.0, ty - T._time_lift(T._line_pos(lines, f, size, tx, ty, "left"), f, size * T._ink(st)))
    T._draw_styled(canvas, lines, f, size, tx, ty, "left", {**st, "_accent": accent})
    return canvas.filter(ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=2))


def _moved(frame: Image.Image, zoom: float, dx: float, dy: float) -> Image.Image:
    """frame を zoom 倍して (dx, dy) に置いた画像。はみ出して空いた所は、ぼかした元の画像で埋める"""
    W, H = frame.size
    fill = frame.resize((W // 8, H // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3)).resize((W, H))
    a = np.array([[zoom, 0, dx], [0, zoom, dy]], np.float32)
    big = cv2.warpAffine(np.asarray(frame), a, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    cover = cv2.warpAffine(np.full((H, W), 255, np.uint8), a, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)
    return Image.composite(Image.fromarray(big), fill, Image.fromarray(cover))


def _shift(a: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """端が反対側に回り込まないようにずらす"""
    out = np.zeros_like(a)
    _paste_arr(out, a, dx, dy)
    return out


def _paste_arr(dst: np.ndarray, src: np.ndarray, x: int, y: int):
    H, W = dst.shape
    h, w = src.shape
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    dx1, dy1 = min(W, x + w), min(H, y + h)
    if dx1 > dx0 and dy1 > dy0:
        dst[dy0:dy1, dx0:dx1] = src[sy0:sy0 + dy1 - dy0, sx0:sx0 + dx1 - dx0]


# ================================================================ rc_thumb から呼ぶ入口

class Designer:
    """1回のサムネイル作成の間、設定と切り抜きデータを持っておく（rc_thumb.generate から使う）"""

    def __init__(self, tpl, allow_download: bool = True):
        self.tpl = tpl
        self.cfg = settings(tpl)
        layouts = (tpl.get("thumbnail", {}) or {}).get("layouts", ["bottom", "band", "side"])
        self.want_person = bool(self.cfg.get("person")) or "person" in layouts
        self.other_bg = str(self.cfg.get("other_background", "none"))
        self.face_pick = bool(self.cfg.get("face_pick", True))
        self.allow_download = allow_download
        self._model: Path | None | bool = False      # False = まだ探していない
        self.ok = available()
        if (self.want_person or self.face_pick) and not self.ok:
            _note("[注意] OpenCV が使えないので、顔を見た場面選び・人物の切り抜きデザインは使いません"
                  "（setup.bat をもう一度実行すると入ります）。")

    @property
    def active(self) -> bool:
        return self.ok and (self.want_person or self.face_pick or self.other_bg != "none")

    @property
    def model(self) -> Path | None:
        if self._model is False:
            self._model = find_model(self.cfg, self.allow_download)
        return self._model

    def frame_key(self, img: Image.Image, sharp: float) -> float:
        if not self.face_pick:
            return sharp
        try:
            return frame_key(img, sharp)
        except Exception as e:  # noqa: BLE001  # 顔の検出に失敗しても、くっきりさで選ぶ
            _note(f"[注意] 顔の検出に失敗したので、くっきりさだけで選びます: {e}")
            return sharp

    def annotate(self, cands: list[dict]) -> list[dict]:
        if not self.face_pick:
            return cands
        try:
            cands = annotate(cands)
        except Exception as e:  # noqa: BLE001
            _note(f"[注意] 顔の検出に失敗したので、くっきりさだけで選びます: {e}")
            for c in cands:
                c.pop("score", None)
                c.pop("face", None)
            return cands
        for c in cands:
            c["note"] = describe(c)
        return cands

    def mask(self, frame: Image.Image, cand: dict | None = None):
        if cand is not None and "face" in cand:     # annotate() で調べ済み
            face = cand["face"]
        else:
            fs = detect_faces(frame)
            face = fs[0] if fs else None
        if face is None:
            return None     # 顔が見つからない場面では作らない（後ろ姿・物だけの場面など）
        return person_mask(frame, face, self.cfg, self.model)

    def make(self, frame: Image.Image, text: str, no: int, font, tdir: Path, cand: dict | None = None):
        """案 no の人物デザインを作る。返り値: (書き出したファイル, ほかのデザインに使う画像)"""
        if not self.ok or not (self.want_person or self.other_bg != "none"):
            return [], frame
        try:
            alpha = self.mask(frame, cand)
        except Exception as e:  # noqa: BLE001  # 切り抜きに失敗しても、ほかのデザインは作る
            print(f"  [注意] 案{no}: 人物を切り抜けませんでした: {e}")
            alpha = None
        if alpha is None:
            print(f"  案{no}: 人物が見つからないので、人物の切り抜きデザインは作りません。")
            return [], frame
        outs = []
        if self.want_person:
            import rc_thumb as T
            style = self.cfg["backgrounds"][(no - 1) % len(self.cfg["backgrounds"])]
            path = tdir / f"thumb_{no}_person.jpg"
            T.save_jpeg(compose_person(frame, alpha, text, style, self.tpl, font, self.cfg), path)
            outs.append(path)
            if self.cfg.get("save_cutout", True):
                cutout(frame, alpha).save(tdir / f"cutout_{no}.png")
        return outs, portrait(frame, alpha, self.other_bg)


def try_image(image: Path, tpl, text: str, out_dir: Path, allow_download: bool = True) -> list[Path]:
    """好きな画像で人物デザインを試す（背景をすべての種類で作る）"""
    import rc_thumb as T
    font = T.find_font(tpl)
    d = Designer(tpl, allow_download)
    if not d.ok:
        raise SystemExit("[エラー] OpenCV が使えないので、人物の切り抜きはできません。setup.bat をもう一度実行してください。")
    src = Image.open(image).convert("RGB")
    frame = src if src.size == (T.W, T.H) else _contain(src, T.W, T.H)
    faces = detect_faces(frame)
    alpha = person_mask(frame, faces[0] if faces else None, d.cfg, d.model)
    if alpha is None:
        raise SystemExit("[エラー] この画像からは人物を切り抜けませんでした。" +
                         ("顔が見つかりません（正面を向いた顔が写った画像で試してください）。" if not faces else
                          "人物と背景の区別がつきませんでした（背景がすっきりした画像で試してください）。"))
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = []
    for style in BACKGROUNDS:
        p = out_dir / f"person_{style}.jpg"
        T.save_jpeg(compose_person(frame, alpha, text, style, tpl, font, d.cfg), p)
        outs.append(p)
    cutout(frame, alpha).save(out_dir / "cutout.png")
    print(f"  顔: {describe({'face': ({k: faces[0][k] for k in ('box', 'eyes', 'smile', 'size', 'weight')} if faces else None)})}")
    print(f"  切り抜き方: {'人物の形のデータ＋GrabCut' if d.model else '顔の位置から推定（GrabCut）'}")
    return outs


def _contain(img: Image.Image, W: int, H: int) -> Image.Image:
    """画像の全体が入るように縮小し、空いた所は同じ画像をぼかして埋める（顔が切れないように）"""
    s = min(W / img.width, H / img.height)
    fit = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    k = max(W / img.width, H / img.height)
    fill = img.resize((int(img.width * k) + 1, int(img.height * k) + 1)).crop((0, 0, W, H))
    out = fill.filter(ImageFilter.GaussianBlur(24))
    out.paste(fit, ((W - fit.width) // 2, (H - fit.height) // 2))
    return out


if __name__ == "__main__":
    print("OpenCV:", "使えます" if available() else "使えません", file=sys.stderr)
