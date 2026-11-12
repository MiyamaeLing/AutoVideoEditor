"""画面の拡張: Bロール（差し込み素材）の確認・変更。

「確認・仕上げ」→「② 演出の確認」に「Bロール」の表を足します。
  - 素材フォルダを選ぶ / AIで割り当てる（broll.py を実行）/ 素材を1つ足す
  - 行ごとに「使う」「出し方（全画面・ワイプ）」「長さ」を変えられる
  - 変更は、ほかの演出と同じく plan_edited.json に入り、「試し書き出し」「完成動画を書き出す」で反映される
"""
from __future__ import annotations

import json
from pathlib import Path

from gui_util import fmt as _fmt
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

import rc_broll

MEDIA_FILTER = ("画像・動画 (" + " ".join(f"*{e}" for e in sorted(rc_broll.IMAGE_EXT | rc_broll.VIDEO_EXT)) + ");;"
                "すべて (*.*)")


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


class BrollPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rv = win.review
        self.folder = QLabel("")
        self.folder.setWordWrap(True)
        self.b_pick = QPushButton("素材フォルダを選ぶ…")
        self.b_pick.setToolTip("Bロールに使う画像・動画を入れたフォルダを選びます")
        self.b_auto = QPushButton("素材を発言に割り当てる")
        self.b_auto.setToolTip("AIが素材を見て、どの発言のときに出すかを決めます（今の Bロールの表は置き換わります）")
        self.b_add = QPushButton("素材を1つ足す…")
        self.b_add.setToolTip("画像・動画を選んで、出したい発言を選びます")
        self.b_xml = QPushButton("Premiere用XML（Bロール入り）")
        self.b_xml.setToolTip("Premiere Pro 用に、荒編集のシーケンスへ Bロールを重ねた XML を作ります")
        self.b_pick.clicked.connect(self.pick_folder)
        self.b_auto.clicked.connect(lambda: self.assign())
        self.b_add.clicked.connect(self.add_one)
        self.b_xml.clicked.connect(self.make_xml)
        top = QHBoxLayout()
        top.addWidget(self.folder, 1)
        for b in (self.b_pick, self.b_auto, self.b_add, self.b_xml):
            top.addWidget(b)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["使う", "元の時刻", "素材", "出し方", "長さ", "出す所（発言）・理由"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(62)
        self.table.setIconSize(QSize(96, 54))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.cellClicked.connect(lambda r, c: self.rv._play_plan_row(self.table, r))
        note = QLabel("全画面＝画面いっぱいに出す / ワイプ＝画面の隅に小さく出す。写真はゆっくり寄ります。"
                      "動画の音は使いません。長さ「おまかせ」は発言の長さに合わせます。")
        note.setWordWrap(True)
        note.setProperty("role", "sub")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.table)
        lay.addWidget(note)

    def _cfg(self) -> dict:
        try:
            from rc_template import Template
            return rc_broll.config(Template(Path(self.rv._template())))
        except (Exception, SystemExit):  # noqa: BLE001
            return rc_broll.config(None)

    # ---------- 表 ----------
    def _ids_text(self, sid, eid) -> str:
        texts = [c["text"] for c in self.rv.chunks if sid <= c["id"] <= (eid if eid is not None else sid)]
        return " ".join(texts)

    def _add_row(self, b: dict, on: bool):
        t = self.table
        r = t.rowCount()
        t.insertRow(r)
        c = self.rv._chunk(b["start_id"]) or {"start": 0, "text": ""}
        chk = self.rv._check(b["start_id"], on)
        chk.setData(Qt.UserRole + 1, dict(b))
        t.setItem(r, 0, chk)
        t.setItem(r, 1, self.rv._ro(_fmt(c["start"])))
        p = Path(b["file"])
        it = self.rv._ro(p.name if p.exists() else f"（見つかりません）{p.name}")
        it.setToolTip(str(p))
        icon = self._icon(p)
        if icon:
            it.setIcon(icon)
        t.setItem(r, 2, it)
        t.setCellWidget(r, 3, self.rv._combo({"full": "全画面", "wipe": "ワイプ"}, b.get("mode", "full")))
        sp = QSpinBox()
        sp.setRange(0, 60)
        sp.setSpecialValueText("おまかせ")
        sp.setSuffix(" 秒")
        sp.setValue(int(round(float(b.get("sec") or 0))))
        t.setCellWidget(r, 4, sp)
        reason = b.get("reason", "")
        t.setItem(r, 5, self.rv._ro(f"「{self._ids_text(b['start_id'], b.get('end_id'))}」"
                                    + (f"　{reason}" if reason else "")))

    def _icon(self, p: Path):
        out = self.rv.out
        th = rc_broll.thumb_path(out, p) if out else None
        src = th if th and th.exists() else (p if p.suffix.lower() in rc_broll.IMAGE_EXT and p.exists() else None)
        if not src:
            return None
        pm = QPixmap(str(src))
        return QIcon(pm.scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)) if not pm.isNull() else None

    def fill(self, plan: dict | None):
        self.table.setRowCount(0)
        if plan:
            off = (plan.get("_off") or {}).get("broll", [])
            start = {c["id"]: c["start"] for c in self.rv.chunks}
            rows = [(b, True) for b in plan.get("broll", [])] + [(b, False) for b in off]
            for b, on in sorted(rows, key=lambda r: start.get(r[0].get("start_id"), 0)):
                self._add_row(b, on)
        self._show_folder()
        self._update_tab()

    def collect(self) -> tuple[list[dict], list[dict]]:
        on, off = [], []
        t = self.table
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            b = dict(it.data(Qt.UserRole + 1) or {})
            b["mode"] = t.cellWidget(r, 3).currentData()
            sec = t.cellWidget(r, 4).value()
            if sec:
                b["sec"] = sec
            else:
                b.pop("sec", None)
            (on if it.checkState() == Qt.Checked else off).append(b)
        return on, off

    def _update_tab(self):
        tabs = self.rv.plan_tabs
        i = tabs.indexOf(self)
        if i >= 0:
            tabs.setTabText(i, f"Bロール（{self.table.rowCount()}）")
        has = self.rv.out is not None
        for b in (self.b_pick, self.b_auto, self.b_add):
            b.setEnabled(has and not self.win.runner.busy)
        self.b_xml.setEnabled(has and self.table.rowCount() > 0 and not self.win.runner.busy)

    def _show_folder(self):
        f = None
        if self.rv.out:
            f = rc_broll.find_folder(self.rv.out, self.rv.sess, self._cfg())
        self.folder.setText(f"素材フォルダ: {f}" if f else
                            "素材フォルダ: まだありません（本編の動画と同じ場所に「broll」フォルダを作るか、右のボタンで選びます）")

    # ---------- 操作 ----------
    def pick_folder(self):
        if not self.rv.out:
            return
        start = str(Path(self.rv.sess["inputs"][0]).parent) if self.rv.sess.get("inputs") else ""
        d = QFileDialog.getExistingDirectory(self, "Bロールの素材フォルダを選ぶ", start)
        if not d:
            return
        rc_broll.save_folder(self.rv.out, d)
        self._show_folder()
        if QMessageBox.question(self, "Bロール", "このフォルダの素材を、発言に割り当てますか？") == QMessageBox.Yes:
            self.assign()

    def assign(self):
        rv = self.rv
        if not rv.out:
            return
        if not rc_broll.find_folder(rv.out, rv.sess, self._cfg()):
            QMessageBox.information(self, "素材フォルダがありません",
                                    "先に「素材フォルダを選ぶ…」で、画像・動画を入れたフォルダを選んでください。")
            return
        llm = self.win.create.llm.currentData()
        if llm == "api" and not self.win.check_api_key(llm):
            return
        rv._save_plan_edits()   # 表の手直し（テロップなど）を残したまま、Bロールだけ入れ替える
        args = [str(rv.out), "--template", rv._template(), "--llm", llm,
                "--effort", self.win.create.effort.currentData()]

        def done(ok):
            if ok:
                rv._fill_plan()
                rv.tabs.setCurrentIndex(1)
                rv.plan_tabs.setCurrentWidget(self)
            rv._update_buttons()
        self.win.runner.start("broll.py", args, "Bロールを割り当て中", done)

    def add_one(self):
        rv = self.rv
        if not rv.out:
            return
        f = rc_broll.find_folder(rv.out, rv.sess, self._cfg())
        path, _ = QFileDialog.getOpenFileName(self, "Bロールの素材を選ぶ", str(f or ""), MEDIA_FILTER)
        if not path:
            return
        cut = set(rv.sess.get("cut_ids", []))
        chunks = [c for c in rv.chunks if c["id"] not in cut]
        labels = [f"{_fmt(c['start'])}  {c['text']}" for c in chunks]
        if not labels:
            return
        sel, ok = QInputDialog.getItem(self, "出す所", "どの発言のときに出しますか？", labels, 0, False)
        if not ok:
            return
        c = chunks[labels.index(sel)]
        self._add_row({"file": str(Path(path).resolve()), "start_id": c["id"], "end_id": c["id"], "mode": "full",
                       "reason": "手で追加"}, True)
        self._update_tab()
        self.rv._save_plan_edits()

    def make_xml(self):
        rv = self.rv
        if not rv.out:
            return
        rv._save_plan_edits()

        def done(ok):
            xs = list(rv.out.glob("*_Bロール入り.xml"))
            if ok and xs:
                QMessageBox.information(self, "Premiereで開く",
                                        f"Premiere Pro の「ファイル → 読み込み」で次のファイルを選んでください。\n\n{xs[0]}")
        self.win.runner.start("broll.py", [str(rv.out), "--template", rv._template(), "--xml-only"],
                              "Bロール入りのXMLを作成中", done)


def register(win):
    rv = win.review
    panel = BrollPanel(win)
    rv.broll_panel = panel
    rv.plan_tabs.addTab(panel, "Bロール")

    # 演出の表の読み込み・保存に Bロールを足す（ほかの拡張が同じように包んでも動くように、元の関数を呼ぶ）
    orig_fill, orig_collect, orig_rows = rv._fill_plan, rv._collect_plan, rv._plan_rows
    orig_buttons = rv._update_buttons

    def fill_plan():
        orig_fill()
        src = rv._plan_source()
        panel.fill(_read_json(src) if src else None)

    def collect_plan():
        plan = orig_collect()
        on, off = panel.collect()
        plan["broll"] = on
        plan.setdefault("_off", {})["broll"] = off
        return plan

    def plan_rows():
        return orig_rows() + panel.table.rowCount()

    def update_buttons():
        orig_buttons()
        panel._update_tab()

    rv._fill_plan, rv._collect_plan, rv._plan_rows = fill_plan, collect_plan, plan_rows
    rv._update_buttons = update_buttons
    if rv.out:
        fill_plan()
