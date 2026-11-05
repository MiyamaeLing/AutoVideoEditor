"""文字起こしの途中保存に numpy の値（np.bool_ / np.float32）が混じっても落ちないことのテスト。

実機（Windows・GPU）で、最後の区切りの保存で「Object of type bool is not JSON serializable」が出た問題の確認。
本物の Whisper の代わりに、numpy の値を返す偽のモデルを使う。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_all(check, out: Path, fx: Path):
    import numpy as np
    import rc_transcribe as T
    from rc_media import SourceSet, probe
    base = fx / "transcribe_numpy"
    base.mkdir(parents=True, exist_ok=True)
    cache = base / "transcript.json"
    for p in base.glob("transcript*"):
        p.unlink()

    class FakeModel:
        def transcribe(self, audio, **kw):
            n = len(audio) / 16000
            return iter([NS(start=np.float32(t), end=np.float32(t + 2), text="テスト",
                            words=[NS(start=np.float32(t), end=np.float32(t + 1), word="テ")],
                            no_speech_prob=np.float32(0.1), avg_logprob=np.float32(-0.2),
                            compression_ratio=np.float32(1.0)) for t in np.arange(0, max(n - 2, 0), 5)]), None

    saved = (T.WINDOW_SEC, T.load_model)
    T.WINDOW_SEC = 20.0   # 区切りを短くして、最後の区切りまで通す
    T.load_model = lambda name, dev: (FakeModel(), "cpu")
    try:
        src = SourceSet([probe(fx / "C0001.MP4")])
        err = ""
        try:
            segs = T.transcribe(src, cache, "large-v3-turbo", "cpu", [], 8)
        except Exception as e:  # noqa: BLE001
            segs, err = [], repr(e)
        check("文字起こし: numpy の値が混じっても最後まで保存できる", bool(segs) and cache.exists(), err)
        if segs:
            check("文字起こし: 時刻は普通の数で返す", all(type(s["start"]) is float for s in segs))
            check("文字起こし: 保存した結果を読み直せる", json.loads(cache.read_text(encoding="utf-8"))["segments"])
        check("文字起こし: np.bool_ を JSON にできる",
              json.loads(json.dumps({"done": np.bool_(True)}, default=T._plain)) == {"done": True})
    finally:
        T.WINDOW_SEC, T.load_model = saved


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "tests"))
    import make_fixtures
    res = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    print(f"\n{sum(res)}/{len(res)} 件 成功")
    sys.exit(0 if all(res) else 1)
