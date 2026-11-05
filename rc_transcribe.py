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


def transcribe(sources: SourceSet, cache_path: Path, model_name: str, device: str,
               glossary_words: list[str], batch_size: int) -> list[dict]:
    """全ファイルを文字起こしして、全体時刻のセグメント一覧を返す（結果はキャッシュする）。"""
    glossary = "、".join(glossary_words)
    key = _cache_key(sources, model_name, glossary)
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("key") == key:
            print("  文字起こし: 前回の結果を再利用します（transcript.json）")
            return data["segments"]

    from faster_whisper import BatchedInferencePipeline

    model, dev = load_model(model_name, device)
    prompt = f"以下は日本語の解説動画の音声です。{glossary}" if glossary else "以下は日本語の解説動画の音声です。"
    all_segs: list[dict] = []
    t0 = time.time()
    for f, off in zip(sources.files, sources.offsets):
        print(f"  文字起こし中: {f.path.name}（{f.duration/60:.1f}分）")
        kwargs = dict(
            language="ja", word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
            condition_on_previous_text=False, initial_prompt=prompt,
            hotwords=glossary or None,
        )
        if dev == "cuda":
            segs, _ = BatchedInferencePipeline(model=model).transcribe(str(f.path), batch_size=batch_size, **kwargs)
        else:
            segs, _ = model.transcribe(str(f.path), **kwargs)
        last_report = 0.0
        for s in segs:
            # 無音部分で出やすい「幻聴」（ありもしない文が出る）を除外
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            if s.compression_ratio > 2.6:
                continue
            words = [
                {"start": round(w.start + off, 3), "end": round(w.end + off, 3), "word": w.word}
                for w in (s.words or [])
            ]
            all_segs.append({
                "start": round(s.start + off, 3), "end": round(s.end + off, 3),
                "text": s.text.strip(), "words": words,
            })
            if s.end - last_report > 120:
                last_report = s.end
                pct = 100 * s.end / max(f.duration, 1)
                print(f"    … {s.end/60:5.1f}分 / {f.duration/60:.1f}分 ({pct:.0f}%)", flush=True)
    print(f"  文字起こし完了: {len(all_segs)}セグメント, {time.time()-t0:.0f}秒")
    cache_path.write_text(json.dumps({"key": key, "segments": all_segs}, ensure_ascii=False, indent=1), encoding="utf-8")
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
