"""faster-whisper による文字起こしと、AIが判定しやすい「短いかたまり（チャンク）」への分割。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from rc_media import SourceSet


def _setup_windows_cuda_dlls() -> None:
    """pip で入れた nvidia-cublas-cu12 / nvidia-cudnn-cu12 の DLL を読み込めるようにする。"""
    if os.name != "nt":
        return
    import site
    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    for root in roots:
        nv = Path(root) / "nvidia"
        if not nv.is_dir():
            continue
        for bin_dir in nv.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def load_model(name: str, device: str):
    """GPU(CUDA)で読み込み、動かなければCPUに切り替える。"""
    _setup_windows_cuda_dlls()
    import numpy as np
    from faster_whisper import WhisperModel

    if device in ("auto", "cuda"):
        try:
            model = WhisperModel(name, device="cuda", compute_type="float16")
            # 読み込めても実行時に落ちる環境（ドライバ/ライブラリ不一致）があるので、ここで試運転する
            segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="ja")
            list(segs)
            print(f"  文字起こしモデル: {name} (GPU / CUDA)")
            return model, "cuda"
        except Exception as e:  # noqa: BLE001
            if device == "cuda":
                raise
            print(f"  [注意] GPUで動かせなかったのでCPUに切り替えます（遅くなります）。理由: {e}")
    model = WhisperModel(name, device="cpu", compute_type="int8")
    print(f"  文字起こしモデル: {name} (CPU)")
    return model, "cpu"


def _cache_key(sources: SourceSet, model_name: str, glossary: str) -> str:
    h = hashlib.sha1()
    for f in sources.files:
        st = f.path.stat()
        h.update(f"{f.path}|{st.st_size}|{int(st.st_mtime)}".encode())
    h.update(f"{model_name}|{glossary}|v1".encode())
    return h.hexdigest()


# 長い素材は、この長さずつ区切って文字起こしし、区切りごとに途中保存する（止まっても続きから再開できる）。
# 一度に読み込む音声もこの長さだけなので、3時間の素材でもメモリをあまり使わない
WINDOW_SEC = 600.0
OVERLAP_SEC = 30.0      # 区切りの境目で発言が切れないように、少し先まで読んでおく
SR = 16000


def partial_path(cache_path: Path) -> Path:
    return Path(cache_path).with_name("transcript_partial.jsonl")


def clear_partial(cache_path: Path) -> None:
    """途中保存を消す（--retranscribe で最初からやり直すとき）"""
    partial_path(cache_path).unlink(missing_ok=True)


def _file_key(f, model_name: str, glossary: str) -> str:
    st = f.path.stat()
    return hashlib.sha1(f"{f.path}|{st.st_size}|{int(st.st_mtime)}|{model_name}|{glossary}|w1".encode()).hexdigest()


def _load_partial(path: Path, keys: set[str]) -> dict[str, dict]:
    """途中保存（1行に1区切り）を読む。最後の行が書きかけで壊れていても、そこまでを使う"""
    state: dict[str, dict] = {}
    if not path.exists():
        return state
    stale = False
    with open(path, encoding="utf-8", errors="replace") as fp:
        for ln in fp:
            try:
                rec = json.loads(ln)
            except ValueError:
                stale = True
                continue
            if rec.get("fkey") not in keys:
                stale = True
                continue
            st = state.setdefault(rec["fkey"], {"until": 0.0, "done": False, "segments": []})
            if abs(rec["from"] - st["until"]) < 1e-3 and not st["done"]:
                st["segments"].extend(rec["segments"])
                st["until"] = rec["to"]
                st["done"] = bool(rec["done"])
    if stale:   # 別の素材・設定のときの途中保存や壊れた行は捨てて、書き直す
        with open(path, "w", encoding="utf-8") as fp:
            for k, st in state.items():
                fp.write(json.dumps({"fkey": k, "from": 0.0, "to": st["until"], "done": st["done"],
                                     "segments": st["segments"]}, ensure_ascii=False) + "\n")
    return state


def _append_partial(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fp.flush()
        try:
            os.fsync(fp.fileno())
        except OSError:
            pass


def read_audio(path: Path, start: float, dur: float):
    """素材の start 秒から dur 秒だけの音声を、16kHz・モノラルで読み込む（faster-whisper に渡す形）"""
    import subprocess

    import numpy as np
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True)
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        sys.exit(f"[エラー] {path.name} の音声を読み込めませんでした:\n" + "\n".join(tail))
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _seg_dict(s, off: float) -> dict:
    return {
        "start": round(s.start + off, 3), "end": round(s.end + off, 3), "text": s.text.strip(),
        "words": [{"start": round(w.start + off, 3), "end": round(w.end + off, 3), "word": w.word}
                  for w in (s.words or [])],
    }


def _shift(segs: list[dict], off: float) -> list[dict]:
    if not off:
        return segs
    return [{"start": round(s["start"] + off, 3), "end": round(s["end"] + off, 3), "text": s["text"],
             "words": [{"start": round(w["start"] + off, 3), "end": round(w["end"] + off, 3), "word": w["word"]}
                       for w in s["words"]]} for s in segs]


def transcribe(sources: SourceSet, cache_path: Path, model_name: str, device: str,
               glossary_words: list[str], batch_size: int) -> list[dict]:
    """全ファイルを文字起こしして、全体時刻のセグメント一覧を返す（結果はキャッシュする）。

    ファイルごと・WINDOW_SEC ごとに transcript_partial.jsonl へ途中保存し、止まっても続きから再開する。
    """
    glossary = "、".join(glossary_words)
    key = _cache_key(sources, model_name, glossary)
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("key") == key:
            print("  文字起こし: 前回の結果を再利用します（transcript.json）")
            return data["segments"]

    ppath = partial_path(cache_path)
    fkeys = [_file_key(f, model_name, glossary) for f in sources.files]
    state = _load_partial(ppath, set(fkeys))
    model = dev = pipe = None
    prompt = f"以下は日本語の解説動画の音声です。{glossary}" if glossary else "以下は日本語の解説動画の音声です。"
    kwargs = dict(
        language="ja", word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
        condition_on_previous_text=False, initial_prompt=prompt,
        hotwords=glossary or None,
    )
    all_segs: list[dict] = []
    t0 = time.time()
    for f, off, fk in zip(sources.files, sources.offsets, fkeys):
        st = state.get(fk, {"until": 0.0, "done": False, "segments": []})
        if st["done"]:
            print(f"  文字起こし: {f.path.name} は途中保存した結果を使います")
            all_segs.extend(_shift(st["segments"], off))
            continue
        local = list(st["segments"])   # ファイル内の時刻
        pos = st["until"]
        print(f"  文字起こし中: {f.path.name}（{f.duration/60:.1f}分）", flush=True)
        if pos > 0:
            print(f"    途中から再開します（{pos/60:.1f}分まで終わっています）", flush=True)
        if model is None:
            from faster_whisper import BatchedInferencePipeline
            model, dev = load_model(model_name, device)
            pipe = BatchedInferencePipeline(model=model) if dev == "cuda" else None
        last_report = pos
        while True:
            if f.duration - pos < 0.5:   # 最後の発言が終わりぎりぎりまであった
                _append_partial(ppath, {"fkey": fk, "from": pos, "to": pos, "done": True, "segments": []})
                break
            last = pos + WINDOW_SEC >= f.duration - 1.0
            end = f.duration if last else pos + WINDOW_SEC
            audio = read_audio(f.path, pos, (f.duration - pos + 1.0) if last else (WINDOW_SEC + OVERLAP_SEC))
            if pipe is not None:
                segs, _ = pipe.transcribe(audio, batch_size=batch_size, **kwargs)
            else:
                segs, _ = model.transcribe(audio, **kwargs)
            new, nxt = [], end
            for s in segs:
                if not last and s.start + pos >= end:
                    break   # ここから先は次の区切りで（続きを読まないので、その分の時間もかからない）
                nxt = max(nxt, s.end + pos)
                # 無音部分で出やすい「幻聴」（ありもしない文が出る）を除外
                if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                    continue
                if s.compression_ratio > 2.6:
                    continue
                new.append(_seg_dict(s, pos))
                if s.end + pos - last_report > 120:
                    last_report = s.end + pos
                    pct = 100 * (s.end + pos) / max(f.duration, 1)
                    print(f"    … {(s.end + pos)/60:5.1f}分 / {f.duration/60:.1f}分 ({pct:.0f}%)", flush=True)
            if last:
                nxt = max(nxt, f.duration)
            local.extend(new)
            _append_partial(ppath, {"fkey": fk, "from": pos, "to": round(nxt, 3), "done": last, "segments": new})
            pos = round(nxt, 3)
            if last:
                break
        all_segs.extend(_shift(local, off))
    print(f"  文字起こし完了: {len(all_segs)}セグメント, {time.time()-t0:.0f}秒")
    cache_path.write_text(json.dumps({"key": key, "segments": all_segs}, ensure_ascii=False, indent=1), encoding="utf-8")
    ppath.unlink(missing_ok=True)
    return all_segs


@dataclass
class Chunk:
    id: int
    start: float
    end: float
    text: str


_SENT_END = re.compile(r"[。！？!?]$")


def make_chunks(segments: list[dict], pause: float = 0.35, max_len: float = 8.0) -> list[Chunk]:
    """単語の間の「間」や句点で区切り、言い直し単位で判定できる細かさにする。"""
    chunks: list[Chunk] = []

    def flush(words):
        if not words:
            return
        text = "".join(w["word"] for w in words).strip()
        if text:
            chunks.append(Chunk(len(chunks) + 1, words[0]["start"], words[-1]["end"], text))

    for seg in segments:
        words = seg["words"] or [{"start": seg["start"], "end": seg["end"], "word": seg["text"]}]
        cur: list[dict] = []
        for w in words:
            if cur:
                gap = w["start"] - cur[-1]["end"]
                too_long = w["end"] - cur[0]["start"] > max_len
                if gap > pause or too_long or _SENT_END.search(cur[-1]["word"].strip()):
                    flush(cur)
                    cur = []
            cur.append(w)
        flush(cur)
    return chunks


def save_chunks(chunks: list[Chunk], path: Path) -> None:
    path.write_text(json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=1), encoding="utf-8")


FILLER_ONLY = re.compile(r"^(?:[、。,.!?！？…・\s]|えー+と?|えっと|ええ+と?|あー+|あの+ー*|あのー+|うー+ん|うーん|んー+|まあ+|その+ー*|ええ+|おー+)+$")


def is_filler_only(text: str) -> bool:
    return bool(FILLER_ONLY.match(text.strip()))


def fmt_ts(t: float, ms: bool = False) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if ms:
        return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m):02d}:{s:04.1f}"
    return f"{int(h)}:{int(m):02d}:{int(s):02d}" if h else f"{int(m):02d}:{int(s):02d}"


if __name__ == "__main__":
    sys.exit("roughcut.py から使ってください。")
