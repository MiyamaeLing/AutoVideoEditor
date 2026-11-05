"""「素材検索」タブ（拡張機能）：全部の動画をまたいで、発言を探す

フォルダを選んで「索引を更新」を押すと、その中の ○○_roughcut の文字起こしを索引に入れます。
言葉を入れて検索し、結果をクリックするとその場面を再生します。ダブルクリックで「確認・仕上げ」タブに開きます。
中身の処理は search.py / rc_search.py。
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAbstractTextDocumentLayout, QPalette, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import rc_search


def _g(win):
    return sys.modules[type(win).__module__]


class HtmlDelegate(QStyledItemDelegate):
    """表のセルに、見つかった言葉を太字・色付きで表示する"""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        selected = bool(opt.state & QStyle.State_Selected)
        # 選んだ行（青い背景）では、色付きの文字を白にして読みやすくする
        doc.setHtml(re.sub(r"color:#[0-9A-Fa-f]{6}", "color:#FFFFFF", opt.text) if selected else opt.text)
        doc.setTextWidth(opt.rect.width())
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        if selected:
            ctx.palette.setColor(QPalette.Text, opt.palette.color(QPalette.HighlightedText))
        painter.save()
        painter.translate(opt.rect.topLeft())
        painter.setClipRect(opt.rect.translated(-opt.rect.topLeft()))
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setHtml(opt.text)
        doc.setTextWidth(max(200, opt.rect.width()))
        return QSize(int(doc.idealWidth()), int(doc.size().height()))


def highlight(text: str, spans: list) -> str:
    out, pos = [], 0
    for a, b in spans:
        out.append(html.escape(text[pos:a]))
        out.append(f"<b style='color:#C53030'>{html.escape(text[a:b])}</b>")
        pos = b
    out.append(html.escape(text[pos:]))
    return "".join(out)


class SearchTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.G = _g(win)
        self.rows: list[dict] = []
        st = win.settings

        self.root = QLineEdit(st.value("search_root", "") or "")
        self.root.setPlaceholderText("動画を保存しているフォルダ（この中の ○○_roughcut を全部探します）")
        pick = QPushButton("選ぶ…")
        pick.clicked.connect(self._pick)
        self.b_index = QPushButton("索引を更新")
        self.b_index.setToolTip("新しく荒編集した動画や、直した字幕を索引に入れます（2回目からは変わった所だけ）")
        self.b_index.clicked.connect(self.update_index)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("探すフォルダ:"))
        r1.addWidget(self.root, 1)
        r1.addWidget(pick)
        r1.addWidget(self.b_index)

        self.query = QLineEdit()
        self.query.setPlaceholderText("探す言葉（例: ドリル　空白で区切ると、全部を含む発言を探します）")
        self.query.returnPressed.connect(self.search)
        go = QPushButton("検索")
        go.setStyleSheet("QPushButton { background: #2B6CB0; color: white; font-weight: bold; padding: 4px 16px; }")
        go.clicked.connect(self.search)
        self.inc_cut = QCheckBox("カットした発言も含める")
        self.inc_cut.toggled.connect(lambda _on: self.query.text().strip() and self.search())
        r2 = QHBoxLayout()
        r2.addWidget(self.query, 1)
        r2.addWidget(go)
        r2.addWidget(self.inc_cut)
        self.info = QLabel("")
        self.info.setStyleSheet("color: #555;")

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["動画", "時刻", "発言（前後の発言）"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 200)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.setItemDelegateForColumn(2, HtmlDelegate(self.table))
        self.table.cellClicked.connect(lambda r, _c: self.play(r))
        self.table.cellDoubleClicked.connect(lambda r, _c: self.open_in_review(r))

        # プレーヤー
        self.player = None
        pl = QVBoxLayout()
        G = self.G
        if G.HAS_MEDIA:
            self.video = G.QVideoWidget()
            self.video.setMinimumSize(320, 180)
            self.player = G.QMediaPlayer(self)
            self.audio = G.QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.stop_at = None
            self.timer = QTimer(self)
            self.timer.setInterval(50)
            self.timer.timeout.connect(self._tick)
            pl.addWidget(self.video, 1)
        else:
            pl.addWidget(QLabel("（この環境では動画の再生部品が使えません）"), 1)
        self.now = QLabel("結果をクリックすると、その場面を再生します。")
        self.now.setWordWrap(True)
        b_play = QPushButton("▶ もう一度再生")
        b_play.clicked.connect(lambda: self.play(self.table.currentRow()))
        b_stop = QPushButton("■ 停止")
        b_stop.clicked.connect(lambda: self.player and self.player.pause())
        self.b_open = QPushButton("確認タブで開く")
        self.b_open.setToolTip("この動画の荒編集を「確認・仕上げ」タブで開きます（ダブルクリックでも開きます）")
        self.b_open.clicked.connect(lambda: self.open_in_review(self.table.currentRow()))
        pr = QHBoxLayout()
        for b in (b_play, b_stop, self.b_open):
            pr.addWidget(b)
        pl.addWidget(self.now)
        pl.addLayout(pr)
        pbox = QWidget()
        pbox.setLayout(pl)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(pbox)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.addLayout(r1)
        lay.addLayout(r2)
        lay.addWidget(self.info)
        lay.addWidget(split, 1)
        self.refresh_info()

    # ---------- 索引 ----------
    def _index(self) -> rc_search.Index:
        return rc_search.Index()

    def refresh_info(self):
        try:
            idx = self._index()
            s = idx.stats()
            idx.close()
        except Exception as e:  # noqa: BLE001
            self.info.setText(f"索引を開けませんでした: {e}")
            return
        if s["videos"]:
            self.info.setText(f"索引: 動画 {s['videos']}本・発言 {s['chunks']:,}個")
        else:
            self.info.setText("索引はまだ空です。フォルダを選んで「索引を更新」を押してください。")

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "動画を保存しているフォルダを選ぶ", self.root.text())
        if d:
            self.root.setText(d)
            self.win.settings.setValue("search_root", d)

    def update_index(self):
        root = self.root.text().strip()
        if not root or not Path(root).is_dir():
            self.G.QMessageBox.information(self, "フォルダを選んでください", "動画を保存しているフォルダを選んでください。")
            return
        self.win.settings.setValue("search_root", root)

        def done(_ok):
            self.refresh_info()
            if self.query.text().strip():
                self.search()
        args = ["index", root]
        db = rc_search.default_db()
        args = ["--db", str(db)] + args
        self.win.runner.start("search.py", args, "発言の索引を作成中", done)

    # ---------- 検索 ----------
    def search(self):
        q = self.query.text().strip()
        if not q:
            return
        idx = self._index()
        try:
            self.rows = idx.search(q, include_cut=self.inc_cut.isChecked(), limit=300)
            stats = idx.stats()
        finally:
            idx.close()
        fmt = self.G.fmt
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            name = row["stem"] + (f"\n{row['title']}" if row["title"] else "")
            v = QTableWidgetItem(name)
            v.setToolTip(row["folder"])
            t = QTableWidgetItem(fmt(row["start"]))
            body = highlight(row["text"], row["spans"])
            if row["cut"]:
                body = "<span style='color:#C53030'>［カット済み］</span>" + body
            ctx = []
            if row["before"]:
                ctx.append(f"<span style='color:#718096'>…{html.escape(row['before'][-30:])}</span>")
            ctx.append(body)
            if row["after"]:
                ctx.append(f"<span style='color:#718096'>{html.escape(row['after'][:30])}…</span>")
            self.table.setItem(r, 0, v)
            self.table.setItem(r, 1, t)
            self.table.setItem(r, 2, QTableWidgetItem("<br>".join(ctx)))
        self.table.resizeRowsToContents()
        n_videos = len({r["folder"] for r in self.rows})
        if self.rows:
            more = "（多すぎるので最初の300件）" if len(self.rows) >= 300 else ""
            self.info.setText(f"「{q}」: {len(self.rows)}件（動画 {n_videos}本）{more}　"
                              f"／ 索引: 動画 {stats['videos']}本")
        elif not stats["videos"]:
            self.info.setText("索引が空です。フォルダを選んで「索引を更新」を押してください。")
        else:
            self.info.setText(f"「{q}」は見つかりませんでした（索引: 動画 {stats['videos']}本）。"
                              "言葉を短くするか、「カットした発言も含める」を試してください。")

    # ---------- 再生 ----------
    def play(self, r: int):
        if not (0 <= r < len(self.rows)):
            return
        row = self.rows[r]
        path, local = rc_search.locate(row["inputs"], max(0.0, row["start"] - 0.8))
        fmt = self.G.fmt
        self.now.setText(f"{row['stem']}　{fmt(row['start'])}〜{fmt(row['end'])}\n{row['text']}")
        if not path or not Path(path).exists():
            self.now.setText(self.now.text() + "\n（元の素材が見つかりません。移動・削除したときは、索引を更新してください）")
            return
        if not self.player:
            return
        rp = getattr(self.win.review, "player", None)
        if rp:
            rp.pause()
        url = QUrl.fromLocalFile(path)
        if self.player.source() != url:
            self.player.setSource(url)
        self.player.setPosition(int(local * 1000))
        self.stop_at = (local + (row["end"] - row["start"]) + 1.6) * 1000
        self.player.play()
        self.timer.start()

    def _tick(self):
        if self.stop_at and self.player.position() >= self.stop_at:
            self.player.pause()
            self.timer.stop()

    def open_in_review(self, r: int):
        if not (0 <= r < len(self.rows)):
            return
        row = self.rows[r]
        folder = Path(row["folder"])
        if not (folder / "session.json").exists():
            self.G.QMessageBox.warning(self, "開けません", f"{folder} が見つかりません。索引を更新してください。")
            return
        if self.player:
            self.player.pause()
        rv = self.win.review
        rv.load(folder)
        self.win.tabs.setCurrentWidget(rv)
        rv.tabs.setCurrentIndex(0)
        rv.filter.setCurrentIndex(1)   # すべての発言を表示
        for i, c in enumerate(rv.chunks):
            if c["id"] == row["id"]:
                rv.table.selectRow(i)
                rv.table.scrollToItem(rv.table.item(i, 0), QAbstractItemView.PositionAtCenter)
                break


def register(win):
    win.search_tab = SearchTab(win)
    win.tabs.addTab(win.search_tab, "素材検索")
