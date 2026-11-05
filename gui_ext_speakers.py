"""GUIの拡張：話者の区別（字幕の色分け・実験的な機能）

「確認・仕上げ」タブに「話者の色分け（実験的）」ボタンを追加する。開いた画面で
  - 人数を選んで「話者を推定する」（speakers.py を実行）
  - 話者の名前を変える・話者を追加する
  - 発言ごとの話者を直す（選んで下のボタン、または数字キー 1〜9）
  - 完成動画の字幕を色分けするか／Premiere用の字幕（SRT）に名前を入れるか
ができる。結果は ○○_roughcut フォルダの speakers.json に保存する。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import rc_speakers

LOW_SCORE = 0.1          # これより自信が低い発言は、黄色で「確認がおすすめ」と出す
C_LOW = QColor("#FFF3C4")
C_CAPTION_BG = QColor("#2D3748")   # 字幕の色が見やすいように、話者の欄は暗い背景にする


def _fmt(t: float) -> str:
    t = max(0, int(t))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _ro(text: str) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    return it


class SpeakerDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.review = win.review
        self.setWindowTitle("話者の色分け（実験的な機能）")
        self.resize(980, 680)
        self.out: Path | None = None
        self.data: dict | None = None
        self.names: list[str] = []
        self.rows: list[dict] = []        # 表の行ごとの発言 {id, start, end, text}
        self.labels: dict[int, int] = {}  # 今の画面での 発言ID → 話者番号
        self.dirty = False
        self._loading = False

        note = QLabel("声の高さや響きから「だれが話しているか」を発言ごとに推定して、完成動画の字幕を人ごとの色にします。"
                      "<b>実験的な機能</b>なので、まちがいがあればここで直してください（黄色の行は自信が低い発言です）。"
                      "人数が分かっているときは、人数を選ぶと正確になります。")
        note.setWordWrap(True)

        # 推定
        self.num = QComboBox()
        self.num.addItem("自動で推定", "auto")
        for n in range(1, 7):
            self.num.addItem(f"{n}人", str(n))
        self.b_detect = QPushButton("話者を推定する")
        self.b_detect.setStyleSheet("QPushButton { background: #2B6CB0; color: white; font-weight: bold; }"
                                    " QPushButton:disabled { background: #CBD5E0; color: #F7FAFC; }")
        self.b_detect.clicked.connect(self.detect)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #555;")
        top = QHBoxLayout()
        top.addWidget(QLabel("話している人数:"))
        top.addWidget(self.num)
        top.addWidget(self.b_detect)
        top.addWidget(self.status, 1)

        # 話者の一覧（名前を変えられる）
        self.t_spk = QTableWidget(0, 3)
        self.t_spk.setHorizontalHeaderLabels(["字幕の色", "名前", "発言数"])
        self.t_spk.verticalHeader().setVisible(False)
        self.t_spk.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.t_spk.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.t_spk.setEditTriggers(QAbstractItemView.DoubleClicked)
        self.t_spk.itemChanged.connect(self._name_changed)
        b_add = QPushButton("話者を追加")
        b_add.clicked.connect(self.add_speaker)
        sl = QVBoxLayout()
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(QLabel("<b>話者</b>（名前はダブルクリックで変更）"))
        sl.addWidget(self.t_spk, 1)
        sl.addWidget(b_add)
        spk_box = QWidget()
        spk_box.setLayout(sl)

        # 発言ごとの話者
        self.t_utt = QTableWidget(0, 4)
        self.t_utt.setHorizontalHeaderLabels(["時刻", "話者", "自信", "発言（クリックで再生）"])
        self.t_utt.verticalHeader().setVisible(False)
        hh = self.t_utt.horizontalHeader()
        for i in range(3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.t_utt.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_utt.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.t_utt.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.t_utt.cellClicked.connect(self._play)
        self.t_utt.setContextMenuPolicy(Qt.CustomContextMenu)
        self.t_utt.customContextMenuRequested.connect(self._menu)
        self.only_low = QCheckBox("自信の低い発言だけ表示")
        self.only_low.toggled.connect(self._apply_filter)
        self.assign_bar = QHBoxLayout()
        ul = QVBoxLayout()
        ul.setContentsMargins(0, 0, 0, 0)
        uh = QHBoxLayout()
        uh.addWidget(QLabel("<b>発言ごとの話者</b>（複数選べます。数字キー 1〜9 でも変えられます）"))
        uh.addStretch()
        uh.addWidget(self.only_low)
        ul.addLayout(uh)
        ul.addWidget(self.t_utt, 1)
        ul.addLayout(self.assign_bar)
        utt_box = QWidget()
        utt_box.setLayout(ul)
        for n in range(1, 10):
            sc = QShortcut(QKeySequence(str(n)), self.t_utt)
            sc.activated.connect(lambda n=n: n - 1 < len(self.names) and self.assign_selected(n - 1))

        split = QSplitter(Qt.Horizontal)
        split.addWidget(spk_box)
        split.addWidget(utt_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([300, 680])

        # 設定と保存
        self.c_enabled = QCheckBox("完成動画の字幕を、話者ごとの色にする")
        self.c_srt = QCheckBox("Premiere用の字幕（SRT）の頭に話者名を入れる（例: 「たろう：こんにちは」）")
        for c in (self.c_enabled, self.c_srt):
            c.toggled.connect(self._mark_dirty)
        self.b_save = QPushButton("保存")
        self.b_save.setStyleSheet("QPushButton { font-weight: bold; }")
        self.b_save.clicked.connect(lambda: self.save())
        b_close = QPushButton("閉じる")
        b_close.clicked.connect(self.close)
        bl = QHBoxLayout()
        opts = QVBoxLayout()
        opts.addWidget(self.c_enabled)
        opts.addWidget(self.c_srt)
        bl.addLayout(opts, 1)
        bl.addWidget(self.b_save)
        bl.addWidget(b_close)

        lay = QVBoxLayout(self)
        lay.addWidget(note)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bl)

    # ---------- 読み込み ----------
    def _colors(self) -> list[str]:
        tpl = None
        try:
            from rc_template import Template
            p = Path(self.review._template())
            if (p / "template.toml").exists():
                tpl = Template(p)
        except (Exception, SystemExit):  # noqa: BLE001  テンプレートが読めなくても初期の色で表示する
            tpl = None
        return [rc_speakers.colors(tpl, i)["color"] for i in range(max(len(self.names), 1))]

    def load(self):
        self.out = self.review.out
        self._loading = True
        sess = self.review.sess or {}
        cut = set(sess.get("cut_ids", []))
        corr = {int(k): v for k, v in sess.get("corrections", {}).items()}
        self.rows = [dict(c, text=corr.get(c["id"], c["text"])) for c in self.review.chunks if c["id"] not in cut]
        self.data = None
        exists = bool(self.out and (self.out / rc_speakers.FILE).exists())
        if self.out:
            self.data = rc_speakers.load(self.out, quiet=True)
        if self.data:
            self.labels = rc_speakers.labels_of(self.data)
            n = max(len(self.data.get("names") or []), max(self.labels.values(), default=0) + 1)
            self.names = [rc_speakers.name_of(self.data, i) for i in range(n)]
            det = self.data.get("detected", {})
            low = sum(1 for r in self.rows if self._score(r["id"]) < LOW_SCORE)
            self.status.setText(f"{det.get('num', len(self.names))}人と推定しました"
                                + (f"（自信の低い発言 {low}個）" if low else ""))
            self.c_enabled.setChecked(bool(self.data.get("enabled", True)))
            self.c_srt.setChecked(bool(self.data.get("srt_names", False)))
        else:
            self.names, self.labels = [], {}
            self.status.setText("文字起こしの区切りが変わったので、もう一度「話者を推定する」を押してください。" if exists
                                else "まだ推定していません。人数を選んで「話者を推定する」を押してください。")
            self.c_enabled.setChecked(True)
            self.c_srt.setChecked(False)
        for w in (self.c_enabled, self.c_srt, self.b_save, self.t_utt, self.t_spk):
            w.setEnabled(bool(self.data))
        self._loading = False
        self._fill()
        self.dirty = False
        self._update_title()

    def _score(self, cid: int) -> float:
        if not self.data:
            return 1.0
        if str(cid) in self.data.get("manual", {}):
            return 1.0
        return float(self.data.get("score", {}).get(str(cid), 1.0))

    def _fill(self):
        self._loading = True
        cols = self._colors()
        counts = [0] * len(self.names)
        for r in self.rows:
            s = self.labels.get(r["id"], 0)
            if 0 <= s < len(counts):
                counts[s] += 1
        self.t_spk.setRowCount(len(self.names))
        for i, name in enumerate(self.names):
            sw = _ro("あいう")
            sw.setBackground(QBrush(C_CAPTION_BG))
            sw.setForeground(QBrush(QColor(cols[i % len(cols)])))
            f = sw.font()
            f.setBold(True)
            sw.setFont(f)
            sw.setTextAlignment(Qt.AlignCenter)
            self.t_spk.setItem(i, 0, sw)
            self.t_spk.setItem(i, 1, QTableWidgetItem(name))
            self.t_spk.setItem(i, 2, _ro(str(counts[i])))
        self.t_utt.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            t = _ro(_fmt(row["start"]))
            t.setData(Qt.UserRole, row["id"])
            self.t_utt.setItem(r, 0, t)
            self.t_utt.setItem(r, 1, _ro(""))
            sc = self._score(row["id"])
            self.t_utt.setItem(r, 2, _ro("低い" if sc < LOW_SCORE else ""))
            self.t_utt.setItem(r, 3, _ro(row["text"]))
            self._paint_row(r, cols)
        self._loading = False
        self._rebuild_assign_bar(cols)
        self._apply_filter()

    def _paint_row(self, r: int, cols: list[str] | None = None):
        cols = cols or self._colors()
        cid = self.rows[r]["id"]
        s = self.labels.get(cid, 0)
        it = self.t_utt.item(r, 1)
        it.setText(f"{s + 1} {self.names[s] if s < len(self.names) else ''}")
        it.setBackground(QBrush(C_CAPTION_BG))
        it.setForeground(QBrush(QColor(cols[s % len(cols)])))
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        low = self._score(cid) < LOW_SCORE
        for c in (0, 2, 3):
            self.t_utt.item(r, c).setBackground(QBrush(C_LOW) if low else QBrush())

    def _rebuild_assign_bar(self, cols: list[str]):
        while self.assign_bar.count():
            w = self.assign_bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self.names:
            return
        self.assign_bar.addWidget(QLabel("選んだ発言を:"))
        for i, name in enumerate(self.names):
            b = QPushButton(f"{i + 1} {name}")
            b.setStyleSheet(f"QPushButton {{ background: {C_CAPTION_BG.name()}; color: {cols[i % len(cols)]};"
                            " font-weight: bold; padding: 3px 10px; }")
            b.clicked.connect(lambda _=False, i=i: self.assign_selected(i))
            self.assign_bar.addWidget(b)
        self.assign_bar.addStretch()

    def _apply_filter(self):
        only = self.only_low.isChecked()
        for r, row in enumerate(self.rows):
            self.t_utt.setRowHidden(r, only and self._score(row["id"]) >= LOW_SCORE)

    # ---------- 手直し ----------
    def _mark_dirty(self, *_):
        if self._loading:
            return
        self.dirty = True
        self._update_title()

    def _update_title(self):
        self.b_save.setText("保存 ●" if self.dirty else "保存")

    def _name_changed(self, it: QTableWidgetItem):
        if self._loading or it.column() != 1:
            return
        name = it.text().strip() or f"話者{it.row() + 1}"
        self.names[it.row()] = name
        cols = self._colors()
        self._loading = True
        for r in range(self.t_utt.rowCount()):
            self._paint_row(r, cols)
        self._loading = False
        self._rebuild_assign_bar(cols)
        self._mark_dirty()

    def add_speaker(self):
        if not self.data:
            return
        self.names.append(f"話者{len(self.names) + 1}")
        self._fill()
        self._mark_dirty()

    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.t_utt.selectedItems()})

    def assign(self, rows: list[int], spk: int):
        cols = self._colors()
        for r in rows:
            self.labels[self.rows[r]["id"]] = spk
            self.data.setdefault("manual", {})[str(self.rows[r]["id"])] = spk   # 直した発言は「自信あり」の扱い
            self._paint_row(r, cols)
        counts = [0] * len(self.names)
        for row in self.rows:
            s = self.labels.get(row["id"], 0)
            if 0 <= s < len(counts):
                counts[s] += 1
        for i, c in enumerate(counts):
            self.t_spk.item(i, 2).setText(str(c))
        self._mark_dirty()

    def assign_selected(self, spk: int):
        rows = self.selected_rows()
        if rows:
            self.assign(rows, spk)

    def _menu(self, pos):
        rows = self.selected_rows()
        if not rows or not self.names:
            return
        m = QMenu(self)
        for i, name in enumerate(self.names):
            m.addAction(f"{i + 1} {name} にする", lambda i=i: self.assign(rows, i))
        m.exec(self.t_utt.viewport().mapToGlobal(pos))

    def _play(self, r: int, _c: int):
        if 0 <= r < len(self.rows):
            self.review.play_range(self.rows[r]["start"], self.rows[r]["end"])

    # ---------- 保存・推定 ----------
    def collect(self) -> dict:
        d = dict(self.data or {})
        auto = {int(k): int(v) for k, v in d.get("auto", {}).items()}
        d["names"] = list(self.names)
        d["manual"] = {str(i): s for i, s in sorted(self.labels.items()) if auto.get(i) != s}
        d["enabled"] = self.c_enabled.isChecked()
        d["srt_names"] = self.c_srt.isChecked()
        return d

    def save(self, then=None, quiet: bool = False) -> bool:
        if not self.out or not self.data:
            return False
        before = bool((rc_speakers.load(self.out, quiet=True) or {}).get("srt_names"))
        d = self.collect()
        rc_speakers.save(self.out, d)
        self.data = d
        self.dirty = False
        self._update_title()
        # Premiere 用の字幕は、名前を入れる設定にしたとき（または外したとき）に書き直す
        if d["srt_names"] or before:
            def done(ok):
                if ok and not quiet:
                    QMessageBox.information(self, "保存しました",
                                            "保存して、Premiere用の字幕（SRT）も書き直しました。\n"
                                            "完成動画に反映するには「完成動画を書き出す」を押してください。")
                if ok and then:
                    then()
            return self.win.runner.start("speakers.py", [str(self.out), "--no-detect", "--write-srt",
                                                         "--template", self.review._template()],
                                         "Premiere用の字幕を書き直し中", done)
        if not quiet:
            QMessageBox.information(self, "保存しました",
                                    "保存しました。完成動画に反映するには「完成動画を書き出す」を押してください。")
        if then:
            then()
        return True

    def detect(self):
        if not self.review.out:
            return
        if self.dirty and self.data:
            # 手直しを先に保存しておく（推定し直しても、手で直した発言はそのまま残る）
            rc_speakers.save(self.out, self.collect())
        args = [str(self.review.out), "--num", self.num.currentData(), "--template", self.review._template()]
        self.b_detect.setEnabled(False)

        def done(ok):
            self.b_detect.setEnabled(True)
            if ok:
                self.load()
        if not self.win.runner.start("speakers.py", args, "話者を推定中（実験的な機能）", done):
            self.b_detect.setEnabled(True)

    def closeEvent(self, e):
        if self.dirty and QMessageBox.question(self, "保存していません", "直した内容を保存しますか？") == QMessageBox.Yes:
            self.save(quiet=True)
        e.accept()


def open_dialog(win) -> SpeakerDialog | None:
    if not win.review.out:
        QMessageBox.information(win, "話者の色分け", "先に荒編集を実行するか、「別の結果を開く…」で結果を開いてください。")
        return None
    dlg = getattr(win, "speaker_dialog", None)
    if dlg is None:
        dlg = SpeakerDialog(win)
        win.speaker_dialog = dlg
    dlg.load()
    dlg.show()
    dlg.raise_()
    return dlg


def register(win):
    b = QPushButton("話者の色分け（実験的）")
    b.setToolTip("発言ごとに話している人を推定して、字幕を人ごとの色にします（名前の変更・手直しもここで）")
    b.clicked.connect(lambda: open_dialog(win))
    win.review.ext_bar.insertWidget(0, b)
    win.speaker_button = b
