"""演出のディレクター（rc_direct.py）と、テロップの見せ方（rc_ass.py）・ズームの中心（rc_render.py）のテスト。

    python tests/test_direct.py            # 単独で実行（先に素材を作ります）
    python tests/smoke_test.py             # 通しテストの最後からも呼ばれます

確認すること:
  - 見せ方の値を整える（知らない場所・文字に無い強調・長すぎる強調は使わない。AIなしでは数字だけ強調）
  - テロップの描き方: 置く場所（8か所）・強調する言葉の色と大きさ・大きさ・出方（6種類）・二重の縁取り・長い文字の2行分け
  - 見せ方の無いテロップ（前の版のプラン・テンプレートの見本）は、今までと同じ形で出る
  - 字幕のキーワードに色を付ける。演出プランの caption_keywords を整える
  - ズームの中心（画面の外にはみ出さない）。区間の分け方・早送りのタイムラインにも中心を渡す
  - カード型のテロップも、決めた場所に置く
  - AIに場面の静止画を見せて、答えをプランに書き足す（偽のAI）。寄れないズームはやめる。設定・失敗で止まらない
  - 見せ方つきのプランで書き出せる（ASS に位置・強調が入る）
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def _dialogues(ass: str, style: str = "") -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue") and (not style or f",{style}," in ln)]


# ---------------------------------------------------------------- 値・描き方
def t_units(check):
    import rc_ass
    import rc_direct as D
    import rc_graphics
    import rc_plan
    import rc_render
    from rc_template import Template
    tpl = Template(ROOT / "templates" / "default")

    ok = D.telop_opts({"text": "重さはわずか30g", "position": "top-right", "emphasis": "30g", "size": "large", "animation": "drop"})
    bad = D.telop_opts({"text": "重さはわずか30g", "position": "どこか", "emphasis": "40g", "size": "huge", "animation": "spin"})
    whole = D.telop_opts({"text": "30g", "emphasis": "30g"})
    check("見せ方: AIの答えをそのまま使い、知らない場所・文字に無い強調・知らない大きさと出方は使わない",
          ok == {"position": "top-right", "emphasis": "30g", "size": "large", "animation": "drop"}
          and bad == {"position": "", "emphasis": "", "size": "normal", "animation": ""} and whole["emphasis"] == "",
          f"{ok} {bad}")
    auto = [D.telop_opts({"text": t})["emphasis"] for t in ("重さ 1.2kg", "年2回の受注生産", "軽すぎて笑った")]
    check("見せ方: AIが決めていないテロップは、数字（単位つき）だけ強調する", auto == ["1.2kg", "2回", ""], str(auto))
    card = D.telop_opts({"text": "A／B", "position": "bottom-left", "emphasis": "A", "animation": "shake"}, card=True)
    card2 = D.telop_opts({"text": "A／B", "position": "left", "animation": "fade"}, card=True)
    check("見せ方: カード型は、置ける場所と出方だけ", card == {"position": "", "emphasis": "", "size": "normal", "animation": ""}
          and card2["position"] == "left" and card2["animation"] == "fade", f"{card} {card2}")
    check("見せ方: ズームの中心（%を0〜1に。範囲の外は端に、決まっていなければ真ん中）",
          D.zoom_focus({"focus": [25, 80]}) == (0.25, 0.8) and D.zoom_focus({"focus": [-5, 140]}) == (0.0, 1.0)
          and D.zoom_focus({}) is None and D.zoom_focus({"focus": "x"}) is None)
    desc = D.describe({"text": "重さはわずか30g", "position": "top-right", "emphasis": "30g", "size": "large", "animation": "drop"})
    check("見せ方: 画面の表に出す説明", desc == "右上・強調「30g」・大きめ・上から落ちる" and D.describe({"text": "あ"}) == "", desc)

    sp = [rc_ass.split_two(t, e) for t, e in (("年2回の受注生産のみ", "年2回"), ("正式名アフリカンパドーク", ""),
                                             ("7,700円→今は9,900円", ""), ("短い", ""))]
    check("テロップ: 長い文字は自然な所で2行に（助詞のあと。強調する言葉・カタカナ語・数字の途中では切らない）",
          sp == [["年2回の", "受注生産のみ"], ["正式名", "アフリカンパドーク"], ["7,700円→", "今は9,900円"], ["短い"]], str(sp))
    cap, c = tpl["caption"], tpl["telop"]["emphasis"]
    places = {p: rc_ass.telop_place(p, 1920, 1080, c, cap) for p in D.POSITIONS}
    check("テロップ: 置く場所は8か所（左右は端に寄せ、下は字幕の上）",
          places["top"][:2] == (8, 960) and places["top-left"][0] == 7 and places["top-right"][:2] == (9, 1850)
          and places["left"][0] == 4 and places["right"][0] == 6 and places["center"][0] == 5
          and places["bottom-left"][0] == 1 and places["bottom-right"][0] == 3
          and 700 < places["bottom-left"][2] < 1080 - cap["margin_bottom"] - cap["size"] * 2, str(places))

    o = D.telop_opts({"text": "重さはわずか30gしかない", "position": "top-right", "emphasis": "30g", "size": "large", "animation": "drop"})
    ass = rc_ass.build_ass(tpl, 1920, 1080, [(0, 2, "このペンは30gです")], [(1, 4, "重さはわずか30gしかない", "emphasis", o)], [],
                           keywords=["30g", "ペン"])
    tel = _dialogues(ass, "T_emphasis")
    front = tel[-1] if tel else ""
    check("テロップ: 置く場所・出方・大きさ・強調する言葉（別の色で大きく）が ASS に入る",
          "\\an9" in front and "\\move(1850,0,1850,70,0,180)" in front and "\\fscx122" in front and "\\fscx134" in front
          and "\\N" in front and front.count("\\1c&H597AFF&") == 1, front[:260])
    check("テロップ: 二重の縁取り（後ろに太い縁をもう1枚）", len(tel) == 2 and tel[0].startswith("Dialogue: 1,")
          and "\\bord15" in tel[0] and "\\3c&HFFFFFF&" in tel[0] and tel[1].startswith("Dialogue: 2,"), str(len(tel)))
    anims = {}
    for a in D.ANIMATIONS:
        oo = D.telop_opts({"text": "ここ注意", "position": "left", "animation": a})
        anims[a] = _dialogues(rc_ass.build_ass(tpl, 1920, 1080, [], [(1, 4, "ここ注意", "warning", oo)], []), "T_warning")[-1]
    check("テロップ: 出方は6種類（弾む・横から・ふわっ・落ちる・ドン・揺れる）",
          "\\t(0,110," in anims["pop"] and "\\move(0,475,70,475,0,220)" in anims["slide"] and "\\fad(220,220)" in anims["fade"]
          and "\\move(70,405,70,475" in anims["drop"] and "\\fscx190" in anims["zoom"] and "\\frz-4" in anims["shake"]
          and len(set(anims.values())) == 6, str({k: v[60:150] for k, v in anims.items()}))
    old = _dialogues(rc_ass.build_ass(tpl, 1920, 1080, [], [(1, 4, "重さ わずか1.2kg！", "emphasis")], []), "T_emphasis")
    check("テロップ: 見せ方の無いテロップ（前の版のプラン・見本）は、今までと同じ形で出る",
          len(old) == 1 and "\\pos" not in old[0] and "\\an" not in old[0] and old[0].endswith("重さ わずか1.2kg！"), str(old))
    caps = _dialogues(ass, "Caption")
    plain = _dialogues(rc_ass.build_ass(tpl, 1920, 1080, [(0, 2, "このペンは30gです")], [], []), "Caption")
    check("字幕: キーワードに色を付ける（無ければ今までどおり）",
          caps[0].endswith("この{\\1c&H4DE1FF&}ペン{\\1c&HFFFFFF&}は{\\1c&H4DE1FF&}30g{\\1c&HFFFFFF&}です")
          and plain[0].endswith(",このペンは30gです"), caps[0][-90:])
    plan = rc_plan.sanitize({"telops": [], "zooms": [], "bgm": [],
                             "caption_keywords": ["紅紫檀", "紅紫檀", "a", " 30g ", "とても長いキーワードは使わない言葉"]}, {1}, [])
    check("演出プラン: 字幕のキーワードを整える（重なり・短すぎ・長すぎを除く）。無ければ項目を作らない",
          plan.get("caption_keywords") == ["紅紫檀", "30g"]
          and "caption_keywords" not in rc_plan.sanitize({"telops": [], "zooms": [], "bgm": []}, {1}, [])
          and "caption_keywords" in rc_plan.PLAN_SCHEMA["required"], str(plan.get("caption_keywords")))

    seg = rc_render.Seg(Path("a.mp4"), 0.0, 30, 1.2, focus=(0.25, 0.9))
    f1 = rc_render._pre_chain(seg, 1920, 1080)
    f0 = rc_render._pre_chain(rc_render.Seg(Path("a.mp4"), 0.0, 30, 1.2), 1920, 1080)
    check("ズーム: 中心を決めた所に寄せる（はみ出さない式）。決めていなければ真ん中のまま",
          "x='max(0,min(iw-ow,iw*0.2500-ow/2))'" in f1 and "ih*0.9000" in f1 and f0.startswith("crop=iw/1.200:ih/1.200,scale"),
          f1[:120])
    box = [e for e in rc_graphics.card_events(tpl, 1920, 1080, [(1, 4, "なにこれ！？", "bubble", {"position": "left"})])]
    box0 = [e for e in rc_graphics.card_events(tpl, 1920, 1080, [(1, 4, "なにこれ！？", "bubble")])]
    check("カード型: 決めた場所に置く（決めていなければテンプレートの場所）", box and box0 and box != box0)


def t_segments(check, out: Path):
    """区間の分け方・早送りのタイムラインに、ズームの中心が渡る"""
    import finish
    import rc_speedup
    from rc_media import SourceSet, probe
    from rc_timeline import Timeline
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    tl = Timeline(src, [tuple(k) for k in sess["keep"]], src.main.fps)
    s0 = tl.pieces[0].g_in
    zooms = [(s0 + 0.5, s0 + 1.5, 1.2, (0.3, 0.7)), (s0 + 2.0, s0 + 2.5, 1.12, None)]
    segs = finish.split_by_zoom(tl, zooms)
    hit = [s for s in segs if s.zoom > 1.001]
    check("ズーム: 区間ごとの映像に、倍率と中心が入る", [(s.zoom, s.focus) for s in hit[:2]] == [(1.2, (0.3, 0.7)), (1.12, None)]
          and all(s.focus is None for s in segs if s.zoom <= 1.001), str([(s.zoom, s.focus) for s in hit]))
    old = finish.split_by_zoom(tl, [(s0 + 0.5, s0 + 1.5, 1.2)])
    check("ズーム: 中心なしの書き方（前の版）もそのまま使える", any(s.zoom == 1.2 and s.focus is None for s in old))
    sp = rc_speedup.SpeedTimeline(tl, [], zooms)
    check("ズーム: 早送りのタイムラインでも中心が入る", any(s.zoom == 1.2 and s.focus == (0.3, 0.7) for s in sp.segs))


# ---------------------------------------------------------------- AIに見せる
def t_direct(check, out: Path, fx: Path):
    import rc_direct as D
    import rc_graphics
    import rc_llm
    from rc_media import SourceSet, probe
    from rc_template import Template
    from rc_transcribe import Chunk
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    src = SourceSet([probe(Path(p)) for p in sess["inputs"]])
    by_id = {c["id"]: Chunk(**c) for c in json.loads((out / "chunks.json").read_text(encoding="utf-8"))}
    work = fx / "direct"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    base = json.loads((fx / "plan.json").read_text(encoding="utf-8"))
    base["zooms"].append({"id": base["telops"][0]["start_id"], "strength": "small"})
    seen = {}

    def fake_call(client, anthropic, system, content, schema, effort, usage):
        seen.update(system=system, content=content, schema=schema, effort=effort)
        usage["in"] = usage.get("in", 0) + 100
        return {"telops": [{"no": 1, "position": "top-right", "emphasis": "ドリル", "size": "large", "animation": "zoom"},
                           {"no": 2, "position": "left", "emphasis": "存在しない", "size": "small", "animation": "slide"},
                           {"no": 3, "position": "なにか", "emphasis": "", "size": "normal", "animation": "drop"},
                           {"no": 9, "position": "top", "emphasis": "", "size": "normal", "animation": "pop"}],
                "zooms": [{"no": 1, "x": 30, "y": 140, "keep": True}, {"no": 2, "x": 50, "y": 50, "keep": False}]}
    orig = rc_llm._client, rc_llm._call
    rc_llm._client, rc_llm._call = (lambda: (None, None)), fake_call
    tpl = Template(fx / "tpl")
    try:
        plan = json.loads(json.dumps(base))
        usage = {"in": 0, "out": 0}
        done = D.direct(work, plan, by_id, src, tpl, "api", "medium", usage, rc_graphics.CARD_STYLES)
        texts = [b["text"] for b in seen.get("content", []) if b.get("type") == "text"]
        imgs = [b for b in seen.get("content", []) if b.get("type") == "image"]
        check("ディレクター: テロップとズームの場面の静止画を、番号つきでAIに見せる",
              done and len(texts) == len(imgs) == 5 and texts[0].startswith("テロップ1（emphasis）: 「ドリルをレビュー！」")
              and texts[3].startswith("ズーム1（発言:") and "position" in seen["system"]
              and seen["schema"]["properties"]["telops"]["items"]["required"][0] == "no", str(texts))
        t = plan["telops"]
        check("ディレクター: 答えをプランに書き足す（おかしな値は使わない。番号の無いものは無視）",
              (t[0]["position"], t[0]["emphasis"], t[0]["size"], t[0]["animation"]) == ("top-right", "ドリル", "large", "zoom")
              and (t[1]["position"], t[1]["emphasis"], t[1]["size"]) == ("left", "", "small")
              and (t[2]["position"], t[2]["animation"]) == ("", "drop") and t[0]["se"] == "pop", str(t))
        check("ディレクター: ズームの中心を決め、寄れないズームはやめる",
              plan["zooms"] == [{"id": 10, "strength": "medium", "focus": [30, 100]}] and usage["in"] == 100, str(plan["zooms"]))
        check("ディレクター: 作業用の静止画は残さない", not (work / "work_direct").exists())

        # 設定でやめる・AIを使わない・テロップもズームも無い
        tdir = work / "tpl_off"
        shutil.copytree(fx / "tpl", tdir, ignore=shutil.ignore_patterns("bgm", "_design_work"))
        s = (tdir / "template.toml").read_text(encoding="utf-8")
        (tdir / "template.toml").write_text(s.replace("\nvision = true", "\nvision = false"), encoding="utf-8")
        seen.clear()
        p2 = json.loads(json.dumps(base))
        off = [D.direct(work, p2, by_id, src, Template(tdir), "api", "medium", {}, ()),
               D.direct(work, p2, by_id, src, tpl, "none", "medium", {}, ()),
               D.direct(work, {"telops": [], "zooms": []}, by_id, src, tpl, "api", "medium", {}, ())]
        check("ディレクター: vision = false・AIなし・テロップもズームも無いときは、何もしない",
              off == [False, False, False] and not seen and p2 == base and "\nvision = false" in
              (tdir / "template.toml").read_text(encoding="utf-8"), str(off))

        def broken(*a, **k):
            raise RuntimeError("通信エラー")
        rc_llm._call = broken
        p3 = json.loads(json.dumps(base))
        check("ディレクター: AIが失敗しても止まらず、プランはそのまま", D.direct(work, p3, by_id, src, tpl, "api", "medium", {}, ()) is False
              and p3 == base)
    except BaseException:  # noqa: BLE001
        import traceback
        check("ディレクター: 偽のAI", False, traceback.format_exc()[-800:])
    finally:
        rc_llm._client, rc_llm._call = orig


def t_render(check, out: Path, fx: Path):
    """見せ方つきのプランで書き出せる（ASS に入る）"""
    import finish
    d = fx / "direct" / "render"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(out, d, ignore=shutil.ignore_patterns("work", "thumbnails", "shorts", "*.mp4", "*.png"))
    plan = json.loads((fx / "plan.json").read_text(encoding="utf-8"))
    plan["telops"][0].update(position="bottom-right", emphasis="ドリル", size="large", animation="zoom")
    plan["zooms"][0]["focus"] = [20, 80]
    plan["caption_keywords"] = ["ドリル"]
    pf = d / "plan_look.json"
    pf.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        finish.run(d, fx / "tpl", "none", pf, preview=12.0, keep_work=True, thumbs=False)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        check("書き出し: 見せ方つきのプラン", False, repr(e))
        return
    ass = (d / "work" / "subs.ass").read_text(encoding="utf-8")
    tel = _dialogues(ass, "T_emphasis")
    used = json.loads((d / "plan_used.json").read_text(encoding="utf-8"))
    check("書き出し: 見せ方つきのプランで書き出せる（位置・強調・キーワードの色が入る）",
          any("\\an3" in x and "\\fscx232" in x for x in tel) and "\\1c&H4DE1FF&}ドリル" in ass
          and list(d.glob("*_プレビュー.mp4")), str(tel)[:300])
    check("書き出し: 使ったプランに、見せ方とキーワードが残る",
          used["telops"][0].get("position") == "bottom-right" and used["zooms"][0].get("focus") == [20, 80]
          and used.get("caption_keywords") == ["ドリル"], str(used["telops"][0]))


# ---------------------------------------------------------------- まとめ
def run_all(check, out: Path, fx: Path):
    t_units(check)
    if not (out / "session.json").exists():
        check("演出のディレクター（荒編集の結果が必要）", False, "先に tests/smoke_test.py を実行してください")
        return
    t_segments(check, out)
    t_direct(check, out, fx)
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
