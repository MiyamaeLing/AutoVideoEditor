"""GUI の拡張：「② 演出の確認」に「早送り」の表を足す（工作の作業シーン）。

- 行ごとに「使う／使わない」と速さ（×2〜×8、タイムラプス）を選べる
- 行をクリックすると、その速さで再生する
- 表の内容は、テロップなどと同じく plan_edited.json に保存され、書き出しで使われる
- 「早送り入りXML」ボタンで、Premiere 用の XML（速度入り）を作る
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

import rc_speedup

CHOICES = [("fast", k) for k in (2, 3, 4, 6, 8)] + [("timelapse", k) for k in (10, 15, 20, 30)]


def _fmt(t: float) -> str:
    t = max(0, t)
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _label(style: str, k: float) -> str:
    return f"{'タイムラプス ' if style == 'timelapse' else ''}×{rc_speedup.fmt_speed(k)}"


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


class SpeedupTab(QWidget):
    COLS = ["使う", "元の時刻", "作業の長さ", "速さ", "完成での長さ", "メモ"]

    def __init__(self, rv):
        super().__init__()
        self.rv = rv
        self.note = QLabel("工作モードで残した「喋っていない作業の場面」です。チェックを外すと普通の速さのまま。"
                           "行をクリックすると、その速さで再生します。")
        self.note.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.cellClicked.connect(lambda r, c: c != 3 and self.play(r))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.note)
        lay.addWidget(self.table)

    # ---- 表 ----
    def _config(self) -> dict:
        try:
            from rc_template import Template
            return rc_speedup.config(Template(Path(self.rv._template())))
        except BaseException:  # noqa: BLE001  テンプレートが壊れていても表は出す（Template は sys.exit する）
            return rc_speedup.config(None)

    def _works(self, cfg) -> list:
        total = sum(d for _s, d, _p in self.rv.offsets) or None
        try:
            return rc_speedup.work_ranges(self.rv.sess, self.rv.chunks, total) if cfg.get("enabled", True) else []
        except Exception:  # noqa: BLE001
            return []

    def fill(self):
        self.table.setRowCount(0)
        src = self.rv._plan_source()
        plan = _read(src) if src else None
        if plan:
            cfg = self._config()
            works = self._works(cfg)
            by_id = {c["id"]: c for c in self.rv.chunks}
            on = rc_speedup.sanitize(plan, works, by_id, cfg)
            off = [x for x in ((plan.get("_off") or {}).get("speedups") or []) if isinstance(x, dict) and "start" in x]
            rows = [(x, True) for x in on] + [(x, False) for x in off]
            # 早送りにしていない作業も、あとから選べるように出しておく
            for n, (s, e) in enumerate(works, 1):
                if not any(x["start"] < e and x["end"] > s for x, _ in rows):
                    a = rc_speedup.auto_speed(e - s, {**cfg, "min_seconds": 0}) or (float(cfg["min_speed"]), "fast")
                    rows.append(({"start": s, "end": e, "speed": a[0], "style": a[1], "work": n,
                                  "_note": "短いので自動では早送りにしていません"}, False))
            rows.sort(key=lambda r: r[0]["start"])
            for x, checked in rows:
                self._add_row(x, checked)
        idx = self.rv.plan_tabs.indexOf(self)
        self.rv.plan_tabs.setTabText(idx, f"早送り（{self.table.rowCount()}）")
        # レビュー・屋外モードなど、早送りの場面がないときは表とボタンを出さない
        show = self.table.rowCount() > 0 or (self.rv.sess or {}).get("mode") == "craft"
        self.rv.plan_tabs.setTabVisible(idx, show)
        btn = getattr(self.rv, "speedup_xml_button", None)
        if btn is not None:
            btn.setVisible(show)

    def _add_row(self, x: dict, checked: bool):
        r = self.table.rowCount()
        self.table.insertRow(r)
        chk = self.rv._check(x.get("work", 0), checked)
        chk.setData(Qt.UserRole + 1, (float(x["start"]), float(x["end"])))
        self.table.setItem(r, 0, chk)
        self.table.setItem(r, 1, self.rv._ro(_fmt(x["start"])))
        dur = float(x["end"]) - float(x["start"])
        self.table.setItem(r, 2, self.rv._ro(f"{dur:.0f}秒"))
        cb = QComboBox()
        choices = list(CHOICES)
        cur = (x.get("style", "fast"), float(x["speed"]))
        if not any(st == cur[0] and abs(k - cur[1]) < 1e-6 for st, k in choices):
            choices.append(cur)
            choices.sort(key=lambda c: (c[0] == "timelapse", c[1]))
        for st, k in choices:
            cb.addItem(_label(st, k), (st, float(k)))
        cb.setCurrentIndex(max(0, next(i for i, (st, k) in enumerate(choices)
                                       if st == cur[0] and abs(k - cur[1]) < 1e-6)))
        cb.currentIndexChanged.connect(lambda _i, row=r: self._update_len(row))
        self.table.setCellWidget(r, 3, cb)
        self.table.setItem(r, 4, self.rv._ro(""))
        memo = x.get("_note") or (f"作業{x['work']}" if x.get("work") else "AIが追加した早送り（声は消えます）")
        self.table.setItem(r, 5, self.rv._ro(memo))
        self._update_len(r)

    def _update_len(self, r: int):
        s, e = self.table.item(r, 0).data(Qt.UserRole + 1)
        _st, k = self.table.cellWidget(r, 3).currentData()
        self.table.item(r, 4).setText(f"{(e - s) / k:.1f}秒")

    def collect(self) -> tuple[list[dict], list[dict]]:
        on, off = [], []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            s, e = it.data(Qt.UserRole + 1)
            st, k = self.table.cellWidget(r, 3).currentData()
            row = {"start": round(s, 3), "end": round(e, 3), "speed": k, "style": st,
                   "work": int(it.data(Qt.UserRole) or 0)}
            (on if it.checkState() == Qt.Checked else off).append(row)
        return on, off

    def play(self, r: int):
        it = self.table.item(r, 0)
        if not it or not self.rv.player:
            return
        s, e = it.data(Qt.UserRole + 1)
        _st, k = self.table.cellWidget(r, 3).currentData()
        self._orig_play(s, e)
        self.rv.player.setPlaybackRate(float(k))
        self.rv.play_info.setText(f"{_fmt(s)} 〜 {_fmt(e)}（{_label(_st, k)} で再生中）")


def register(win):
    rv = win.review
    tab = SpeedupTab(rv)
    rv.plan_tabs.addTab(tab, "早送り")
    rv.speedup_tab = tab

    orig_fill, orig_collect, orig_rows, orig_play = rv._fill_plan, rv._collect_plan, rv._plan_rows, rv.play_range
    tab._orig_play = orig_play

    def fill_plan():
        orig_fill()
        tab.fill()

    def collect_plan() -> dict:
        plan = orig_collect()
        on, off = tab.collect()
        plan["speedups"] = on
        plan["speedups_auto"] = False   # 画面で決めた通りにする（書いていない作業を自動で足さない）
        plan.setdefault("_off", {})["speedups"] = off
        return plan

    def plan_rows() -> int:
        return orig_rows() + tab.table.rowCount()

    def play_range(s, e):
        if rv.player:
            rv.player.setPlaybackRate(1.0)   # 早送りの表で速くしたあとも、ほかの行はふつうの速さで
        orig_play(s, e)

    rv._fill_plan, rv._collect_plan, rv._plan_rows, rv.play_range = fill_plan, collect_plan, plan_rows, play_range

    # 早送り入りの Premiere 用 XML
    b = QPushButton("早送り入りXML")
    b.setToolTip("「早送り」の表の速さを入れた、Premiere 用の XML を作ります（<名前>_早送り入り.xml）")

    def make_xml():
        if not rv.out:
            return
        if rv._plan_rows():
            p = rv.out / "plan_edited.json"
            p.write_text(json.dumps(rv._collect_plan(), ensure_ascii=False, indent=1), encoding="utf-8")

        def done(ok):
            xs = list(rv.out.glob("*_早送り入り.xml"))
            if ok and xs:
                QMessageBox.information(rv, "早送り入りXML", f"作りました。Premiere Pro の「ファイル → 読み込み」で"
                                        f"選んでください。\n\n{xs[0]}")
        win.runner.start("speedup.py", [str(rv.out), "--xml", "--template", rv._template()], "早送り入りXMLを作成中",
                         done)
    b.clicked.connect(make_xml)
    rv.ext_bar.insertWidget(0, b)
    rv.speedup_xml_button = b
    b.setVisible(False)   # 工作モードの結果を開いたときに出す

    orig_update = rv._update_buttons

    def update_buttons():
        orig_update()
        b.setEnabled(rv.out is not None and not win.runner.busy)
    rv._update_buttons = update_buttons
    update_buttons()
