"""「企画」タブと、「確認・仕上げ」タブの「③ 撮り漏れチェック」（拡張機能）

- 企画: テーマとジャンルを入れると、構成案・台本（話す要点）・撮影リストを作る。表で直して保存、印刷用ページも開ける
- 撮り漏れチェック: 荒編集の文字起こしと台本を照らし合わせ、話し忘れ・撮っていないかもしれないカットを一覧にする
中身の処理は preprod.py / rc_preprod.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

import rc_preprod as P

import rc_theme   # 行の色は今のテーマ（ライト／ダーク）に合わせる: rc_theme.row_colors()

C_BAD = QColor("#F3D6D6")   # ライトのときの色（互換のため残す）
C_WARN = QColor("#FFF3C4")
C_OK = QColor("#D8F0DA")


def _g(win):
    """gui.py のモジュール（python gui.py で起動したときは __main__）"""
    return sys.modules[type(win).__module__]


def _ro(text) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return it


def _chk(on: bool) -> QTableWidgetItem:
    it = QTableWidgetItem()
    it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    it.setCheckState(Qt.Checked if on else Qt.Unchecked)
    return it


# ============================================================
#  企画タブ
# ============================================================
class PlanTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.G = _g(win)
        self.pdir: Path | None = None
        self.data: dict | None = None
        self._loading = False

        # ---- 新しい企画 ----
        self.theme = QLineEdit()
        self.theme.setPlaceholderText("例: マキタの新しいドリルをレビュー")
        self.genre = QComboBox()
        for k, (name, _hint) in P.GENRES.items():
            self.genre.addItem(name, k)
        self.minutes = QDoubleSpinBox()
        self.minutes.setRange(0, 600)
        self.minutes.setDecimals(0)
        self.minutes.setSuffix(" 分")
        self.minutes.setSpecialValueText("おまかせ")
        self.name = QLineEdit()
        self.name.setPlaceholderText("空ならテーマと同じ")
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("伝えたいこと・使う道具・撮影場所・見てほしい人など（なくても大丈夫）")
        self.notes.setMaximumHeight(90)
        self.b_new = QPushButton("構成案・台本・撮影リストを作る")
        rc_theme.style(self.b_new, kind="primary")
        self.b_new.clicked.connect(self.generate)
        hint = QLabel("AIは「作成」タブの設定を使います（AIを使わない場合は、ジャンルの定番の流れのひな形を作ります）")
        hint.setWordWrap(True)
        hint.setProperty("role", "sub")
        form = QFormLayout()
        form.addRow("テーマ", self.theme)
        form.addRow("ジャンル", self.genre)
        form.addRow("長さの目安", self.minutes)
        form.addRow("企画の名前", self.name)
        form.addRow("メモ", self.notes)
        newbox = QGroupBox("新しい企画")
        nl = QVBoxLayout(newbox)
        nl.addLayout(form)
        nl.addWidget(self.b_new)
        nl.addWidget(hint)

        # ---- これまでの企画 ----
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda cur, _prev: cur and self.open_project(Path(cur.data(Qt.UserRole))))
        listbox = QGroupBox("これまでの企画")
        ll = QVBoxLayout(listbox)
        ll.addWidget(self.list)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(newbox)
        lv.addWidget(listbox, 1)

        # ---- 中身 ----
        self.head = QLabel("左で企画を作るか、選んでください。")
        self.head.setStyleSheet("font-weight: bold; font-size: 15px;")
        self.head.setWordWrap(True)
        self.sub = QLabel("")
        self.sub.setProperty("role", "sub")
        self.sub.setWordWrap(True)
        for lab in (self.head, self.sub):
            lab.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.t_pts = QTableWidget(0, 4)
        self.t_pts.setHorizontalHeaderLabels(["構成（見出し）", "必須", "話す要点", "確かめる言葉（、区切り）"])
        self.t_shots = QTableWidget(0, 6)
        self.t_shots.setHorizontalHeaderLabels(["撮った", "必須", "種類", "使う場面", "撮るもの", "コツ"])
        for t, stretch in ((self.t_pts, 2), (self.t_shots, 4)):
            t.verticalHeader().setVisible(False)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
            hh = t.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(stretch, QHeaderView.Stretch)
            t.itemChanged.connect(self._changed)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.inner = QTabWidget()
        self.inner.addTab(self.t_pts, "台本（話す要点）")
        self.inner.addTab(self.t_shots, "撮影リスト")
        self.inner.addTab(self.view, "印刷イメージ")
        self.b_add = QPushButton("行を追加")
        self.b_add.clicked.connect(self.add_row)
        self.b_del = QPushButton("選んだ行を消す")
        self.b_del.clicked.connect(self.del_rows)
        self.b_save = QPushButton("保存")
        self.b_save.clicked.connect(self.save)
        self.b_print = QPushButton("印刷用ページを開く")
        self.b_print.setToolTip("ブラウザで開きます。Ctrl+P で紙やPDFにできます")
        self.b_print.clicked.connect(self.open_print)
        self.b_folder = QPushButton("フォルダを開く")
        self.b_folder.clicked.connect(lambda: self.pdir and self.G.open_path(self.pdir))
        bl = QHBoxLayout()
        for b in (self.b_add, self.b_del):
            bl.addWidget(b)
        bl.addStretch()
        for b in (self.b_save, self.b_print, self.b_folder):
            bl.addWidget(b)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.head)
        rv.addWidget(self.sub)
        rv.addWidget(self.inner, 1)
        rv.addLayout(bl)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setSizes([360, 840])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        self.refresh_list()
        self._update_buttons()

    # ---------- 一覧 ----------
    def refresh_list(self, select: Path | None = None):
        self.list.blockSignals(True)
        self.list.clear()
        for d, data in P.list_projects():
            g = P.GENRES.get(data.get("genre"), P.GENRES["other"])[0]
            it = QListWidgetItem(f"{d.name}　（{g}）")
            it.setToolTip(f"{data.get('title', '')}\n更新: {data.get('updated', '')}")
            it.setData(Qt.UserRole, str(d))
            self.list.addItem(it)
            if select and Path(select).resolve() == d.resolve():
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        if select:
            self.open_project(Path(select))

    def _update_buttons(self):
        has = self.data is not None
        for b in (self.b_add, self.b_del, self.b_save, self.b_print, self.b_folder):
            b.setEnabled(has)
        self.b_new.setEnabled(not self.win.runner.busy)

    def _dirty(self, on: bool):
        self.b_save.setText("保存 ●" if on else "保存")

    def is_dirty(self) -> bool:
        return self.data is not None and self.b_save.text().endswith("●")

    def ask_save(self, why: str = "") -> bool:
        """保存していない手直しがあれば、保存するか聞く。続けてよければ True（「キャンセル」なら False）"""
        if not self.is_dirty():
            return True
        r = QMessageBox.question(self, "保存していません", f"直した内容を保存しますか？{why}",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            self.save()
            return True
        return r == QMessageBox.Discard

    def open_print(self):
        """印刷用ページを開く。直した内容は、保存してから開く（撮影に持っていく紙に入るように）"""
        if not self.pdir:
            return
        if not self.ask_save("\n（印刷用ページは、保存した内容で作られます）"):
            return
        self.G.open_path(self.pdir / "plan.html")

    def _changed(self, _it):
        if not self._loading:
            self._dirty(True)

    # ---------- 表示 ----------
    def open_project(self, d: Path):
        if self.data is not None and self.b_save.text().endswith("●") and self.pdir and d != self.pdir:
            if QMessageBox.question(self, "保存していません", "直した内容を保存しますか？") == QMessageBox.Yes:
                self.save()
        try:
            self.pdir, self.data = P.load_project(d)
        except SystemExit as e:
            QMessageBox.warning(self, "読み込めません", str(e))
            return
        self.fill()

    def fill(self):
        d = self.data
        self._loading = True
        g = P.GENRES.get(d.get("genre"), P.GENRES["other"])[0]
        self.head.setText(d.get("title") or d.get("name"))
        sub = [f"ジャンル: {g}"]
        if P._min(d.get("minutes")):
            sub.append(f"長さ: {P._min(d['minutes'])}")
        if d.get("audience"):
            sub.append(f"見てほしい人: {d['audience']}")
        if d.get("source") == "waiting":
            sub.append("※ claude.ai の返答を待っています（今はひな形です）")
        self.sub.setText("　/　".join(sub) + (f"\n{d['concept']}" if d.get("concept") else ""))
        rows = list(P.all_points(d))
        self.t_pts.setRowCount(len(rows))
        for r, (_si, s, p) in enumerate(rows):
            sec = QTableWidgetItem(s["name"])
            sec.setToolTip(s.get("purpose", ""))
            txt = QTableWidgetItem(p["text"])
            txt.setData(Qt.UserRole, p["id"])
            kw = QTableWidgetItem("、".join(p["keywords"]))
            kw.setToolTip("撮り漏れチェックで、話したかを確かめる言葉。表記ゆれは「|」で区切ります（例: 重さ|おもさ）"
                          + ("" if p["keywords"] else f"\n空のときは自動: {'・'.join(P.auto_keywords(p['text'])) or 'なし'}"))
            for col, it in enumerate([sec, _chk(p["must"]), txt, kw]):
                self.t_pts.setItem(r, col, it)
        # 前の企画の選択欄は、消えるまで少し残るので、先に隠しておく
        for r in range(self.t_shots.rowCount()):
            for col in (2, 3):
                w = self.t_shots.cellWidget(r, col)
                if w:
                    w.hide()
                    self.t_shots.removeCellWidget(r, col)
        self.t_shots.setRowCount(len(d["shots"]))
        for r, k in enumerate(d["shots"]):
            what = QTableWidgetItem(k["what"])
            what.setData(Qt.UserRole, k["id"])
            for col, it in enumerate([_chk(k["done"]), _chk(k["must"]), None, None, what, QTableWidgetItem(k["tip"])]):
                if it is not None:
                    self.t_shots.setItem(r, col, it)
            self.t_shots.setCellWidget(r, 2, self._kind_combo(k["kind"]))
            self.t_shots.setCellWidget(r, 3, self._sec_combo(k["section"]))
        self._fit_combos()
        self._loading = False
        self.view.setHtml(P.to_html(d))
        self._dirty(False)
        self._update_buttons()

    def _fit_combos(self):
        """表の中の選択欄（種類・使う場面）が、隣の列に重ならない幅にする"""
        fm = self.t_shots.fontMetrics()
        for col in (2, 3):
            w = self.t_shots.horizontalHeader().sectionSizeHint(col)
            for r in range(self.t_shots.rowCount()):
                cb = self.t_shots.cellWidget(r, col)
                if cb:
                    w = max(w, max(fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())) + 44)
            self.t_shots.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
            self.t_shots.setColumnWidth(col, w)

    @staticmethod
    def _small(cb: QComboBox) -> QComboBox:
        cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        cb.setMinimumContentsLength(3)
        return cb

    def _kind_combo(self, kind: str) -> QComboBox:
        cb = QComboBox()
        for k, v in P.SHOT_KINDS.items():
            cb.addItem(v, k)
        cb.setCurrentIndex(list(P.SHOT_KINDS).index(kind))
        cb.currentIndexChanged.connect(lambda _i: self._dirty(True))
        return self._small(cb)

    def _sec_combo(self, sec: int) -> QComboBox:
        cb = QComboBox()
        cb.addItem("（全体）", 0)
        for i, s in enumerate(self.data["sections"], 1):
            cb.addItem(f"{i}. {s['name']}", i)
        cb.setCurrentIndex(sec if 0 <= sec < cb.count() else 0)
        cb.currentIndexChanged.connect(lambda _i: self._dirty(True))
        return self._small(cb)

    # ---------- 編集 ----------
    def collect(self) -> dict:
        """表の内容から企画を組み立て直す（構成は、見出しの名前が出てきた順）"""
        d = dict(self.data)
        old = {s["name"]: s for s in d["sections"]}
        order: list[str] = []
        pts: dict[str, list] = {}
        for r in range(self.t_pts.rowCount()):
            name = (self.t_pts.item(r, 0).text() if self.t_pts.item(r, 0) else "").strip() or "（見出しなし）"
            txt = self.t_pts.item(r, 2)
            if not txt or not txt.text().strip():
                continue
            kws = self.t_pts.item(r, 3).text() if self.t_pts.item(r, 3) else ""
            if name not in pts:
                order.append(name)
                pts[name] = []
            pts[name].append({"id": txt.data(Qt.UserRole), "text": txt.text().strip(),
                              "must": self.t_pts.item(r, 1).checkState() == Qt.Checked,
                              "keywords": [k.strip() for k in kws.replace(",", "、").split("、") if k.strip()]})
        old_names = [s["name"] for s in d["sections"]]
        # 要点がなくなった見出しも消さずに、もとの位置あたりに残す（撮影リストで使っていることがあるので）
        for i, n in enumerate(old_names):
            if n not in pts:
                order.insert(min(i, len(order)), n)
                pts[n] = []
        d["sections"] = [{"name": n, "purpose": old.get(n, {}).get("purpose", ""),
                          "minutes": old.get(n, {}).get("minutes", 0), "points": pts[n]} for n in order]
        new_idx = {n: i for i, n in enumerate(order, 1)}
        shots = []
        for r in range(self.t_shots.rowCount()):
            what = self.t_shots.item(r, 4)
            if not what or not what.text().strip():
                continue
            sec_old = self.t_shots.cellWidget(r, 3).currentData() or 0
            sec_name = old_names[sec_old - 1] if 1 <= sec_old <= len(old_names) else None
            shots.append({"id": what.data(Qt.UserRole), "done": self.t_shots.item(r, 0).checkState() == Qt.Checked,
                          "must": self.t_shots.item(r, 1).checkState() == Qt.Checked,
                          "kind": self.t_shots.cellWidget(r, 2).currentData(),
                          "section": new_idx.get(sec_name, 0), "what": what.text().strip(),
                          "tip": self.t_shots.item(r, 5).text() if self.t_shots.item(r, 5) else ""})
        d["shots"] = shots
        if d.get("source") == "waiting":
            d["source"] = "edited"
        return d

    def save(self):
        if self.data is None:
            return
        self.data = P.normalize(self.collect())
        P.save_project(self.pdir, self.data)
        self.fill()
        self.win.statusBar().showMessage(f"保存しました: {self.pdir / 'plan.json'}", 5000)

    def add_row(self):
        if self.data is None:
            return
        self._loading = True
        if self.inner.currentWidget() is self.t_shots:
            r = self.t_shots.rowCount()
            self.t_shots.insertRow(r)
            for col, it in enumerate([_chk(False), _chk(False), None, None, QTableWidgetItem(""), QTableWidgetItem("")]):
                if it is not None:
                    self.t_shots.setItem(r, col, it)
            self.t_shots.setCellWidget(r, 2, self._kind_combo("insert"))
            self.t_shots.setCellWidget(r, 3, self._sec_combo(0))
            self._fit_combos()
            self.t_shots.setCurrentCell(r, 4)
            self.t_shots.editItem(self.t_shots.item(r, 4))
        else:
            self.inner.setCurrentWidget(self.t_pts)
            cur = self.t_pts.currentRow()
            r = cur + 1 if cur >= 0 else self.t_pts.rowCount()
            sec = self.t_pts.item(cur, 0).text() if cur >= 0 and self.t_pts.item(cur, 0) else \
                (self.data["sections"][-1]["name"] if self.data["sections"] else "本編")
            self.t_pts.insertRow(r)
            for col, it in enumerate([QTableWidgetItem(sec), _chk(False), QTableWidgetItem(""), QTableWidgetItem("")]):
                self.t_pts.setItem(r, col, it)
            self.t_pts.setCurrentCell(r, 2)
            self.t_pts.editItem(self.t_pts.item(r, 2))
        self._loading = False
        self._dirty(True)

    def del_rows(self):
        t = self.t_shots if self.inner.currentWidget() is self.t_shots else self.t_pts
        rows = sorted({i.row() for i in t.selectedIndexes()}, reverse=True)
        for r in rows:
            t.removeRow(r)
        if rows:
            self._dirty(True)

    # ---------- 作成 ----------
    def generate(self):
        theme = self.theme.text().strip()
        if not theme:
            QMessageBox.information(self, "テーマを入れてください", "どんな動画を撮るか（テーマ）を入れてください。")
            return
        llm = self.win.create.llm.currentData()
        if not self.win.check_api_key(llm):
            return
        name = P.safe_name(self.name.text().strip() or theme)
        d = P.projects_dir() / name
        args = ["new", theme, "--name", name, "--genre", self.genre.currentData(), "--llm", llm,
                "--effort", self.win.create.effort.currentData()]
        if self.minutes.value() > 0:
            args += ["--minutes", f"{self.minutes.value():g}"]
        if self.notes.toPlainText().strip():
            args += ["--notes", self.notes.toPlainText().strip()]
        if (d / "plan.json").exists():
            if QMessageBox.question(self, "同じ名前の企画があります",
                                    f"「{name}」はもうあります。作り直しますか？（前の内容は消えます）") != QMessageBox.Yes:
                return
            args.append("--overwrite")

        def done(ok):
            self._update_buttons()
            if not ok:
                return
            if llm == "manual" and (d / "prompt_plan.md").exists():
                dlg = self.G.ManualDialog(self, d / "prompt_plan.md", "構成案・台本・撮影リストを作る")
                if dlg.exec():
                    ans = d / "claude_plan.json"
                    ans.write_text(dlg.text(), encoding="utf-8")
                    self.win.runner.start("preprod.py", ["new", "--name", name, "--answer", str(ans)],
                                          "claude.ai の返答を取り込み中", lambda ok2: self.refresh_list(d))
                    return
            self.refresh_list(d)
        if self.win.runner.start("preprod.py", args, "企画（構成案・台本・撮影リスト）を作成中", done):
            self._update_buttons()


# ============================================================
#  撮り漏れチェック（確認・仕上げタブの中）
# ============================================================
class CheckPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.G = _g(win)
        self.out: Path | None = None
        self.res: dict | None = None
        self._loading = False
        self.project = QComboBox()
        self.project.setMinimumWidth(200)
        self.project.currentIndexChanged.connect(lambda _i: self._update_buttons())
        self.b_local = QPushButton("照合する")
        self.b_local.setToolTip("台本の「確かめる言葉」が文字起こしに出てくるかで調べます（AIなし・すぐ終わる）")
        self.b_local.clicked.connect(lambda: self.run(ai=False))
        self.b_ai = QPushButton("AIで詳しく照合")
        self.b_ai.setToolTip("Claude が文字起こしを読んで、要点を話したかを判断します（作成タブのAIの設定を使います）")
        self.b_ai.clicked.connect(lambda: self.run(ai=True))
        self.b_md = QPushButton("結果を開く")
        self.b_md.clicked.connect(lambda: self.out and self.G.open_path(self.out / "preprod_check.md"))
        self.filter = QComboBox()
        self.filter.addItems(["気になるものだけ", "すべて"])
        self.filter.currentIndexChanged.connect(self._apply_filter)
        row = QHBoxLayout()
        row.addWidget(QLabel("台本（企画）:"))
        row.addWidget(self.project, 1)
        row.addWidget(self.b_local)
        row.addWidget(self.b_ai)
        row.addWidget(self.filter)
        row.addWidget(self.b_md)
        self.summary = QLabel("台本（企画タブで作ったもの）を選んで「照合する」を押すと、話し忘れた要点と、"
                              "撮っていないかもしれないカットが出ます。")
        self.summary.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["状態", "構成", "内容", "時刻", "メモ（クリックで場面を再生）"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(4, 220)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellClicked.connect(self._clicked)
        self.table.itemChanged.connect(self._item_changed)
        legend = QLabel()
        legend.setWordWrap(True)
        self.legend = legend
        self._legend()
        rc_theme.on_change(self._retheme)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.summary)
        lay.addWidget(self.table, 1)
        lay.addWidget(legend)
        self._update_buttons()

    def _legend(self):
        tk = rc_theme.tokens()

        def span(key, text):
            return f"<span style='background:{tk[key]}; color:{tk['text']}'>&nbsp;{text}&nbsp;</span>"
        self.legend.setText(f"{span('row_cut', '話していない・撮った記録なし')} {span('row_review', '一部だけ・要確認')} "
                            f"{span('row_hil', 'OK')}　カットは、撮れていたら「状態」に印を付けてください")

    def _retheme(self):
        self._legend()
        if self.res:
            self.show_result(self.res)

    def _update_buttons(self):
        ok = self.out is not None and not self.win.runner.busy and self.project.count() > 0 \
            and self.project.currentData() is not None
        self.b_local.setEnabled(ok)
        self.b_ai.setEnabled(ok)
        self.b_md.setEnabled(bool(self.out and (self.out / "preprod_check.md").exists()))

    def refresh(self, out: Path | None):
        self.out = out
        self.project.blockSignals(True)
        self.project.clear()
        items = P.list_projects()
        if not items:
            self.project.addItem("（まだ企画がありません。「企画」タブで作れます）", None)
        linked = P.linked_project(out) if out else None
        res = P.load_check(out) if out else None
        want = linked or (Path(res["project"]) if res and res.get("project") else None)
        if items and not want:
            self.project.addItem("（選んでください）", None)
        for d, data in items:
            self.project.addItem(f"{d.name}　{data.get('title', '')}", str(d))
            if want and Path(want).resolve() == d.resolve():
                self.project.setCurrentIndex(self.project.count() - 1)
        self.project.blockSignals(False)
        self.show_result(res)
        self._update_buttons()

    def show_result(self, res: dict | None):
        self.res = res
        self._loading = True
        self.table.setRowCount(0)
        if not res:
            self._loading = False
            return
        c = res["counts"]
        how = "Claude" if res.get("method") == "ai" else "言葉の一致"
        self.summary.setText(f"話していない要点 <b>{c['missing']}</b> / {c['points']}（一部だけ {c['partial']}）　"
                             f"撮った記録がないカット <b>{c['shots_missing']}</b> / {c['shots']}　"
                             f"<span style='color:{rc_theme.tokens()['subtext']}'>（{how}で照合・{res.get('created', '')}）</span>")
        rows = [("p", p) for p in res["points"]] + [("s", s) for s in res["shots"]]
        self.table.setRowCount(len(rows))
        fmt = self.G.fmt
        rc = rc_theme.row_colors()
        C_OK, C_WARN, C_BAD, fg = rc["ok"], rc["warn"], rc["bad"], QBrush(rc["fg"])
        for r, (kind, it) in enumerate(rows):
            if kind == "p":
                st = _ro(P.POINT_STATUS.get(it["status"], it["status"]))
                text = ("★" if it["must"] else "") + it["text"]
                color = C_OK if it["status"] == "said" else C_WARN if it["status"] in ("partial", "unknown") else C_BAD
            else:
                st = QTableWidgetItem(P.SHOT_STATUS.get(it["status"], it["status"]))
                st.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                st.setCheckState(Qt.Checked if it["status"] == "done" else Qt.Unchecked)
                st.setToolTip("撮れていたら印を付けます（企画の撮影リストにも記録します）")
                text = f"【{P.SHOT_KINDS[it['kind']]}】" + ("★" if it["must"] else "") + it["what"]
                color = C_OK if it["status"] == "done" else C_WARN if it["status"] == "likely" else C_BAD
            st.setData(Qt.UserRole, (kind, it["id"]))
            cells = [st, _ro(it.get("section_name", "")), _ro(text),
                     _ro(fmt(it["t"]) if it.get("t") is not None else ""), _ro(it.get("note", ""))]
            for col, cell in enumerate(cells):
                cell.setBackground(QBrush(color))
                cell.setForeground(fg)   # 淡い背景の上でも読めるように、文字の色も決める
                if col == 4:
                    cell.setToolTip(it.get("note", ""))
                self.table.setItem(r, col, cell)
            self.table.item(r, 0).setData(Qt.UserRole + 1, color != C_OK)
        self._loading = False
        self._apply_filter()

    def _apply_filter(self):
        only = self.filter.currentIndex() == 0
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, only and not self.table.item(r, 0).data(Qt.UserRole + 1))

    def _row(self, r: int):
        it = self.table.item(r, 0)
        if not it or not self.res:
            return None
        kind, iid = it.data(Qt.UserRole)
        lst = self.res["points"] if kind == "p" else self.res["shots"]
        return kind, next((x for x in lst if x["id"] == iid), None)

    def _clicked(self, r: int, _c: int):
        got = self._row(r)
        if not got or not got[1] or not got[1].get("chunk_ids"):
            return
        rv = self.win.review
        ids = got[1]["chunk_ids"]
        chunks = [c for c in rv.chunks if c["id"] in ids]
        if chunks:
            rv.play_range(chunks[0]["start"], chunks[-1]["end"])

    def _item_changed(self, cell: QTableWidgetItem):
        """カットの「撮った」の印を、結果と企画の撮影リストに記録する"""
        if self._loading or cell.column() != 0:
            return
        got = self._row(cell.row())
        if not got or got[0] != "s" or not got[1]:
            return
        shot = got[1]
        on = cell.checkState() == Qt.Checked
        shot["status"] = "done" if on else (shot.get("auto_status") or "missing")
        self.res["counts"]["shots_missing"] = sum(s["status"] == "missing" for s in self.res["shots"])
        pdir = Path(self.res["project"])
        if (pdir / "plan.json").exists():
            pd, data = P.load_project(pdir)
            for k in data["shots"]:
                if k["id"] == shot["id"]:
                    k["done"] = on
            P.save_project(pd, data)
            tab = getattr(self.win, "plan_tab", None)
            if tab and tab.pdir and tab.pdir.resolve() == pd.resolve() and not tab.b_save.text().endswith("●"):
                tab.open_project(pd)
        P.save_check(self.out, self.res)
        filt = self.filter.currentIndex()
        self.show_result(self.res)
        self.filter.setCurrentIndex(filt)

    def run(self, ai: bool):
        pdir = self.project.currentData()
        if not self.out or not pdir:
            QMessageBox.information(self, "台本を選んでください", "照らし合わせる企画（台本）を選んでください。")
            return
        pt = getattr(self.win, "plan_tab", None)   # 企画タブで直したまま保存していない台本は、保存してから照合する
        if pt is not None and pt.pdir and Path(pt.pdir).resolve() == Path(pdir).resolve() \
                and not pt.ask_save("\n（撮り漏れチェックは、保存した台本で照合します）"):
            return
        llm = "none"
        if ai:
            llm = self.win.create.llm.currentData()
            if llm == "none":
                QMessageBox.information(self, "AIを使わない設定です",
                                        "「作成」タブのAIが「AIを使わない」になっているので、言葉の一致で照合します。")
            elif not self.win.check_api_key(llm):
                return
        out = self.out
        args = ["check", str(out), "--project", pdir, "--llm", llm, "--effort", self.win.create.effort.currentData()]

        def done(ok):
            if ok and llm == "manual" and (out / "prompt_preprod_check.md").exists():
                dlg = self.G.ManualDialog(self, out / "prompt_preprod_check.md", "台本と照合する")
                if dlg.exec():
                    ans = out / "claude_preprod_check.json"
                    ans.write_text(dlg.text(), encoding="utf-8")
                    self.win.runner.start("preprod.py", ["check", str(out), "--project", pdir, "--answer", str(ans)],
                                          "claude.ai の返答を取り込み中", lambda _ok: self.refresh(out))
                    return
            self.refresh(out)
        if self.win.runner.start("preprod.py", args, "撮り漏れチェック（台本と照合）", done):
            self._update_buttons()


def register(win):
    win.plan_tab = PlanTab(win)
    if hasattr(win, "close_hooks"):   # ツールを閉じるときも、企画の手直しの保存を確かめる
        win.close_hooks.append(lambda: win.plan_tab.ask_save())
    win.tabs.addTab(win.plan_tab, "企画")
    win.check_panel = CheckPanel(win)
    win.review.tabs.addTab(win.check_panel, "③ 撮り漏れチェック")

    # 荒編集の結果を開いたら、撮り漏れチェックも読み直す
    orig_load = win.review.load

    def load(out, *a, **kw):
        r = orig_load(out, *a, **kw)
        if win.review.out is not None:
            win.check_panel.refresh(win.review.out)
        return r
    win.review.load = load

    def busy(_b):
        win.plan_tab._update_buttons()
        win.check_panel._update_buttons()
    win.runner.busy_changed.connect(busy)
