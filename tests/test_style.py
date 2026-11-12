"""演出のスタイル（にぎやか / 落ち着いた）の切り替えの確認。AI は使わない。

  python tests/test_style.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def t_units(check):
    import rc_plan
    import rc_polish
    import rc_style as S
    from rc_template import Template
    from rc_timeline import PRESETS
    keep = os.environ.pop(S.ENV, None)
    try:
        tpl = Template(ROOT / "templates" / "default")
        check("スタイル: 何も選ばなければ「にぎやか」。プラン > 画面の選択 > テンプレートの順で決まる",
              S.current(tpl) == "lively" and S.current(tpl, {"look": "calm"}) == "calm" and S.current(None, {"look": "x"}) == "lively")
        lively = rc_plan._instructions("talk", ["calm"])
        S.activate("calm")
        calm = rc_plan._instructions("talk", ["calm"])
        check("スタイル: 画面で選んだもの（環境変数）が使われる", S.current(tpl) == "calm" and S.is_calm())
        check("演出プラン: 落ち着いたスタイルでは、小さなラベルだけを頼む（大きなテロップ・ズーム・キーワードは頼まない）",
              '"label"' in calm and "bubble" not in calm and "必ず空の配列" in calm and "- bgm:" in calm
              and calm.rstrip().endswith("出力はJSONのみです。") and "bubble" in lively and "label" not in lively, calm[-300:])
        check("演出プラン: ラベルの種類を受け取れる",
              "label" in rc_plan.PLAN_SCHEMA["properties"]["telops"]["items"]["properties"]["style"]["enum"])
        plan = S.clean_plan({"telops": [{"start_id": 1, "end_id": 1, "text": "重さ：30g", "style": "label", "se": "pop"},
                                        {"start_id": 2, "end_id": 2, "text": "すごい！", "style": "emphasis", "se": "ding"},
                                        {"start_id": 3, "end_id": 3, "text": "価格 9,900円", "style": "info", "se": "none"}],
                             "zooms": [{"id": 1, "strength": "medium"}], "bgm": [], "caption_keywords": ["30g"]}, "calm")
        check("演出プラン: 落ち着いたスタイルでは、ラベル以外のテロップ・ズーム・効果音・キーワードを使わない",
              [t["style"] for t in plan["telops"]] == ["label", "label"] and all(t["se"] == "none" for t in plan["telops"])
              and plan["zooms"] == [] and "caption_keywords" not in plan and plan["look"] == "calm", str(plan))
        p0 = S.clean_plan({"telops": [{"start_id": 2, "end_id": 2, "text": "すごい！", "style": "emphasis", "se": "ding"}],
                           "zooms": [{"id": 1}], "bgm": []}, "lively")
        check("演出プラン: にぎやかでは今までどおり", len(p0["telops"]) == 1 and len(p0["zooms"]) == 1 and p0["look"] == "lively")
        size = tpl["caption"]["size"]
        S.apply_template(tpl, "calm")
        check("字幕: 落ち着いたスタイルでは小さめ・細い縁取り・キーワードの色なし",
              tpl["caption"]["size"] < size * 0.8 and tpl["caption"]["outline"] <= 3 and tpl["caption"]["keyword_color"] == "",
              str(tpl["caption"]))
        pc, pl = S.cut_preset(PRESETS["talk"]), None
        os.environ[S.ENV] = "lively"
        pl = S.cut_preset(PRESETS["talk"])
        check("カット: 落ち着いたスタイルでは、言葉の前後の間を少し残す",
              pc["pad_after"] > pl["pad_after"] == PRESETS["talk"]["pad_after"] and pc["merge_gap"] > pl["merge_gap"], str(pc))
        cfg = S.config(None)
        sch = S.schedule_labels([(10.0, "太さ：10mm"), (20.0, "重さ：30g"), (30.0, "全長：138mm"), (100.0, "価格：9,900円")],
                                [0.0, 60.0, 150.0], 200.0, cfg)
        check("ラベル: 測るたびに下へ積み上がり、章が変わるといっしょに消える",
              [x[3] for x in sch] == [0, 1, 2, 0] and len({round(x[1], 1) for x in sch[:3]}) == 1 and sch[0][1] <= 60
              and sch[3][0] == 100.0, str(sch))
        many = S.schedule_labels([(float(k), f"項目{k}：{k}") for k in range(1, 8)], [], 300.0, cfg)
        live = [x for x in many if x[0] <= 7.0 < x[1]]
        check("ラベル: 同時に出すのは4つまで（古いものから消す）", len(live) <= 4 and len({x[3] for x in live}) == len(live), str(many))
        ev = S.label_events(sch[:1], 1920, 1080, tpl)
        st = S.label_styles(tpl)
        ass = S.add_events("[V4+ Styles]\nStyle: A\n\n[Events]\nDialogue: x\n", ev, st)
        check("ラベル: 帯は1枚（文字は透明）、その上に項目と値を色分けして重ねる。左上に置く",
              len(ev) == 2 and ",Label," in ev[0] and ev[0].count("{") == 1 and ",LabelTx," in ev[1] and ev[1].count("\\1c") == 2
              and "\\an7" in ev[0] and "太さ  10mm" in ev[0] and ass.index("Style: Label,") < ass.index("[Events]")
              and ass.rstrip().endswith(ev[1]), str(ev))
        items = [(1, "えー、野原工芸野原工芸は長野県の工房です"), (2, "重さが30グラム"), (3, "はい"), (4, "これはすごい")]
        res = {"captions": [{"id": 1, "text": "野原工芸は長野県の工房です。"}, {"id": 2, "text": "重さが30g"}, {"id": 3, "text": ""},
                            {"id": 4, "text": "これは本当にすごくて、みなさんにもぜひ使ってほしいと思います"}, {"id": 9, "text": "x"}]}
        out = rc_polish.apply(items, res)
        check("字幕の文: AIが整えた文を使う（言い直し・単位）。長くなりすぎた返事・知らないIDは使わない",
              out == {"1": "野原工芸は長野県の工房です", "2": "重さが30g", "3": ""}, str(out))
        tx = rc_polish.texts([(1, 0.0, 1.0, "a"), (2, 1.0, 2.0, "b")], {"2": "B"})
        check("字幕の文: 整えた文は発言IDで当てはめる（無ければ元の文）", [t for *_x, t in tx] == ["a", "B"]
              and not rc_polish.accept("とても長い大事な発言をしています", "") and rc_polish.accept("えーと", ""))
    finally:
        os.environ.pop(S.ENV, None)
        if keep is not None:
            os.environ[S.ENV] = keep


def t_render(check, out: Path, fx: Path):
    """落ち着いたスタイルのプランで書き出せる（ラベルが入り、章カード・ズーム・大きなテロップが入らない）"""
    import shutil
    d = fx / "style"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(out, d, ignore=shutil.ignore_patterns("work", "thumbnails", "*.mp4", "shorts"))
    sess = json.loads((d / "session.json").read_text(encoding="utf-8"))
    chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
    ids = [c["id"] for c in chunks if c["id"] not in set(sess["cut_ids"])]
    plan = {"look": "calm", "telops": [{"start_id": ids[0], "end_id": ids[0], "text": "重さ：1.2kg", "style": "label", "se": "pop"},
                                       {"start_id": ids[1], "end_id": ids[1], "text": "すごい！", "style": "emphasis", "se": "ding"}],
            "zooms": [{"id": ids[1], "strength": "medium"}], "bgm": [], "caption_text": {str(ids[0]): "整えた字幕です"}}
    pf = d / "plan_calm.json"
    pf.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "ROUGHCUT_LOOK"}
    p = subprocess.run([sys.executable, str(ROOT / "finish.py"), str(d), "--template", str(fx / "tpl"), "--plan", str(pf),
                        "--llm", "none", "--preview", "6", "--no-thumbs", "--keep-work"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    ass = (d / "work" / "subs.ass").read_text(encoding="utf-8") if (d / "work" / "subs.ass").exists() else ""
    used = json.loads((d / "plan_used.json").read_text(encoding="utf-8")) if (d / "plan_used.json").exists() else {}
    check("書き出し: 落ち着いたスタイルで書き出せる（左上のラベル・整えた字幕が入り、章カード・大きなテロップは入らない）",
          p.returncode == 0 and ",Label," in ass and "整えた字幕です" in ass and ",Chapter," not in ass.split("[Events]")[-1]
          and ",T_emphasis," not in ass.split("[Events]")[-1] and "落ち着いた" in p.stdout and list(d.glob("*_プレビュー.mp4")),
          (p.stdout + p.stderr)[-600:])
    check("書き出し: 使ったプランにスタイルが残り、ズームと大きなテロップは消えている",
          used.get("look") == "calm" and used.get("zooms") == [] and [t["style"] for t in used.get("telops", [])] == ["label"]
          and used.get("caption_text"), str(used)[:300])


def run_all(check, out: Path, fx: Path):
    t_units(check)
    if not (out / "session.json").exists():
        check("演出のスタイル（荒編集の結果が必要）", False, "先に tests/smoke_test.py を実行してください")
        return
    t_render(check, out, fx)


if __name__ == "__main__":
    import make_fixtures
    res = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {'' if ok else detail}", flush=True)
    fx = make_fixtures.make()
    run_all(check, fx / "out", fx)
    print(f"\n{sum(res)}/{len(res)} 件 成功")
    sys.exit(0 if all(res) else 1)
