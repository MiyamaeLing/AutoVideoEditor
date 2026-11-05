"""2台カメラの切り替え（画面）：「② 演出の確認」に「カメラ」の表を足す。

- 手元カメラ（2台目）を使う場面の一覧。行をクリックすると、その場面を再生して両カメラの静止画を出す
- 開始・終了の時刻はダブルクリックで直せる。チェックを外すと正面カメラのまま
- 「自動で選び直す」「AIで選び直す」で multicam.py を実行する
- 表の内容は、ほかの演出と同じく plan_edited.json の "angles" に保存され、書き出しに使われる
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import rc_multicam

TAB_NAME = "カメラ"


def fmt_t(t: float) -> str:
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h >= 1 else f"{int(m):02d}:{s:04.1f}"


def parse_t(text: str) -> float | None:
    """「1:23.5」「01:02:03」「83.5」などを秒にする（読めなければ None）"""
    text = text.strip().replace("：", ":").replace("．", ".")
    if not re.fullmatch(r"\d+(:\d+){0,2}(\.\d+)?", text):
        return None
    v = 0.0
    for part in text.split(":"):
        v = v * 60 + float(part)
    return v


class AnglePanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rv = win.review
        self.had_key = False       # 今のプランに "angles" があったか（無ければ書き出しのときに自動で決める）
        self.cam2 = None
        self._cam2_for = None
        self._loading = False

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["使う", "開始（元の時刻）", "終了", "長さ（カット後）", "理由"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self.table.itemChanged.connect(self._changed)
        self.table.cellClicked.connect(lambda r, c: self._show_row(r, play=c != 0))

        self.img_main, self.img_cam2 = QLabel(), QLabel()
        imgs = QHBoxLayout()
        for lab, title in ((self.img_main, "正面カメラ"), (self.img_cam2, "手元カメラ（2台目）")):
            lab.setFixedSize(224, 126)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("background: #222; color: #ddd;")
            lab.setText(title)
            box = QVBoxLayout()
            box.addWidget(QLabel(title))
            box.addWidget(lab)
            imgs.addLayout(box)
        imgs.addStretch()

        self.b_add = QPushButton("＋ 追加")
        self.b_add.setToolTip("再生中の位置（または選んだ行のあと）から5秒の場面を、手元カメラにします")
        self.b_del = QPushButton("選んだ行を削除")
        self.b_auto = QPushButton("自動で選び直す")
        self.b_auto.setToolTip("長い無言の作業と、「ここを」「この部品」などの発言を手元カメラにします")
        self.b_ai = QPushButton("AIで選び直す")
        self.b_ai.setToolTip("Claude に両カメラの静止画と発言を見せて決めてもらいます（Claude APIを使います）")
        self.b_add.clicked.connect(self.add_row)
        self.b_del.clicked.connect(self.delete_rows)
        self.b_auto.clicked.connect(lambda: self.redo(False))
        self.b_ai.clicked.connect(lambda: self.redo(True))
        bl = QHBoxLayout()
        for b in (self.b_add, self.b_del):
            bl.addWidget(b)
        bl.addStretch()
        for b in (self.b_auto, self.b_ai):
            bl.addWidget(b)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.note)
        lay.addWidget(self.table, 1)
        lay.addLayout(imgs)
        lay.addLayout(bl)

    # ---------- 表 ----------
    def _cam2_info(self):
        out = self.rv.out
        if self._cam2_for != out:
            self._cam2_for = out
            try:
                self.cam2 = rc_multicam.load_cam2(out, self.rv.sess) if out else None
            except (Exception, SystemExit):  # noqa: BLE001
                self.cam2 = None
        return self.cam2

    def has_cam2(self) -> bool:
        return bool((self.rv.sess.get("args") or {}).get("cam2")) if self.rv.out else False

    def fill(self):
        self._loading = True
        self.table.setRowCount(0)
        src = self.rv._plan_source()
        try:
            plan = json.loads(src.read_text(encoding="utf-8-sig")) if src else {}
        except (OSError, ValueError):
            plan = {}
        self.had_key = "angles" in plan
        off = (plan.get("_off") or {}).get("angles", [])
        rows = [(a, True) for a in rc_multicam.sanitize_angles(plan.get("angles"))]
        rows += [(a, False) for a in rc_multicam.sanitize_angles(off)]
        for a, on in sorted(rows, key=lambda r: r[0]["start"]):
            if a.get("cam") == "main":
                continue
            self._insert(a["start"], a["end"], a.get("why", ""), on)
        self._loading = False
        self._update_note(plan)

    def _update_note(self, plan=None):
        n = self.table.rowCount()
        idx = self.rv.plan_tabs.indexOf(self)
        if idx >= 0:
            self.rv.plan_tabs.setTabText(idx, f"{TAB_NAME}（{n}）")
            self.rv.plan_tabs.setTabVisible(idx, self.has_cam2())
        cfg = rc_multicam.settings(self._template())
        if not self.rv.out or not (self.rv.out / "plan_used.json").exists():
            self.note.setText("「演出プランを作る」を押すと、手元カメラ（2台目）を使う場面がここに表示されます。")
        elif not self.had_key and n == 0:
            self.note.setText("まだ選んでいません。書き出しのときに自動で選びます（「自動で選び直す」で今すぐ選べます）。")
        else:
            self.note.setText(f"ここに並んだ場面は、映像だけを手元カメラ（2台目）に切り替えます（音声は正面カメラのまま）。"
                              f"行をクリックすると再生します。時刻はダブルクリックで直せます。"
                              f"{cfg['min_shot']:.0f}秒より短い切り替えは、書き出しのときにまとめます。")
        for b in (self.b_auto, self.b_ai, self.b_add):
            b.setEnabled(bool(self.rv.out) and (self.rv.out / "plan_used.json").exists())
        self.b_del.setEnabled(n > 0)

    def _template(self):
        try:
            from rc_template import Template
            return Template(Path(self.rv._template()))
        except (Exception, SystemExit):  # noqa: BLE001
            return None

    def _insert(self, s: float, e: float, why: str, on: bool = True, row: int | None = None):
        r = self.table.rowCount() if row is None else row
        self.table.insertRow(r)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        chk.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.table.setItem(r, 0, chk)
        for col, t in ((1, s), (2, e)):
            it = QTableWidgetItem(fmt_t(t))
            it.setData(Qt.UserRole, float(t))
            self.table.setItem(r, col, it)
        ln = QTableWidgetItem(self._len_text(s, e))
        ln.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(r, 3, ln)
        self.table.setItem(r, 4, QTableWidgetItem(why))
        return r

    def _len_text(self, s: float, e: float) -> str:
        """カットした所を除いた長さ（完成動画で映る長さ）"""
        keep = self.rv.sess.get("keep") or [(s, e)]
        d = sum(max(0.0, min(e, ke) - max(s, ks)) for ks, ke in keep)
        return f"{d:.1f}秒"

    def _times(self, r: int) -> tuple[float, float]:
        return self.table.item(r, 1).data(Qt.UserRole), self.table.item(r, 2).data(Qt.UserRole)

    def _changed(self, it: QTableWidgetItem):
        if self._loading:
            return
        if it.column() in (1, 2):
            self._loading = True
            v = parse_t(it.text())
            if v is None:
                v = it.data(Qt.UserRole)   # 読めない書き方なら元に戻す
            it.setData(Qt.UserRole, float(v))
            it.setText(fmt_t(v))
            s, e = self._times(it.row())
            if e <= s:   # 終了が開始より前なら、入れ替える
                self.table.item(it.row(), 1).setData(Qt.UserRole, e)
                self.table.item(it.row(), 1).setText(fmt_t(e))
                self.table.item(it.row(), 2).setData(Qt.UserRole, s)
                self.table.item(it.row(), 2).setText(fmt_t(s))
                s, e = e, s
            self.table.item(it.row(), 3).setText(self._len_text(s, e))
            self._loading = False
        self.had_key = True

    def collect_into(self, plan: dict) -> dict:
        """表の内容を演出プランに入れる（チェックを外したものは "_off" に）"""
        if not self.had_key and self.table.rowCount() == 0:
            return plan   # まだ何も選んでいない：書き出しのときに自動で決める
        on, off = [], []
        for r in range(self.table.rowCount()):
            s, e = self._times(r)
            a = {"start": round(s, 3), "end": round(e, 3), "cam": "cam2", "why": self.table.item(r, 4).text().strip()}
            (on if self.table.item(r, 0).checkState() == Qt.Checked else off).append(a)
        plan["angles"] = sorted(on, key=lambda a: a["start"])
        plan.setdefault("_off", {})["angles"] = sorted(off, key=lambda a: a["start"])
        return plan

    # ---------- 操作 ----------
    def _player_time(self) -> float | None:
        p = self.rv.player
        if not p or not self.rv.offsets:
            return None
        cur = p.source().toLocalFile()
        for start, _d, path in self.rv.offsets:
            if cur and Path(path) == Path(cur):
                return start + p.position() / 1000
        return None

    def add_row(self):
        t = self._player_time()
        r = self.table.currentRow()
        if t is None:
            t = self._times(r)[1] if r >= 0 else 0.0
        self.had_key = True
        self._loading = True
        row = self._insert(t, t + 5.0, "手動で追加")
        self._loading = False
        self.table.selectRow(row)
        self._update_note()

    def delete_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if rows:
            self.had_key = True
        self._update_note()

    def _show_row(self, r: int, play: bool = True):
        if not (0 <= r < self.table.rowCount()):
            return
        s, e = self._times(r)
        if play:
            self.rv.play_range(s, e)
        t = min(s + 1.0, (s + e) / 2)
        for lab, loc in ((self.img_main, self._main_loc(t)), (self.img_cam2, self._cam2_loc(t))):
            data = rc_multicam.grab_jpeg(Path(loc[0]), loc[1], 448) if loc else None
            pm = QPixmap()
            if data and pm.loadFromData(data):
                lab.setPixmap(pm.scaled(lab.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                lab.setText("（映像がありません）")

    def _main_loc(self, t: float):
        if not self.rv.offsets:
            return None
        path, local = self.rv._locate(t)
        return (path, local) if path else None

    def _cam2_loc(self, t: float):
        c2 = self._cam2_info()
        return c2.locate(t) if c2 else None

    def redo(self, ai: bool):
        rv = self.rv
        if not rv.out:
            return
        if not (rv.out / "plan_used.json").exists():
            QMessageBox.information(self, "演出プランがありません", "先に「演出プランを作る」を押してください。")
            return
        if ai and not self.win.check_api_key("api"):
            return
        rv._save_plan_edits()   # 表のほかの手直し（テロップなど）が消えないように
        args = [str(rv.out), "--template", rv._template(), "--llm", "api" if ai else "none",
                "--effort", self.win.create.effort.currentData()]

        def done(ok):
            if ok:
                rv._fill_plan()
                rv.plan_tabs.setCurrentWidget(self)
        self.win.runner.start("multicam.py", args, "AIでカメラを選び直し中" if ai else "カメラを選び直し中", done)


def register(win):
    rv = win.review
    panel = AnglePanel(win)
    rv.plan_tabs.addTab(panel, TAB_NAME)
    rv.plan_tabs.setTabVisible(rv.plan_tabs.indexOf(panel), False)
    rv.angle_panel = panel

    # 演出の表の読み込み・保存に、カメラの表も加える（gui.py の処理はそのまま使う）
    orig_fill, orig_collect, orig_rows = rv._fill_plan, rv._collect_plan, rv._plan_rows

    def fill():
        orig_fill()
        panel.fill()

    def collect():
        return panel.collect_into(orig_collect())

    rv._fill_plan = fill
    rv._collect_plan = collect
    rv._plan_rows = lambda: orig_rows() + panel.table.rowCount()
