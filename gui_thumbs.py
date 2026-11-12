"""画面（GUI）: サムネイル案の一覧（作り直しの入口）と、サムネイルの場面を選ぶ画面。

gui.py の「確認・仕上げ」タブ（ReviewPage.thumbnails）から開く。作る処理は thumbs.py を呼び出している。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QToolButton, QVBoxLayout, QWidget,
)

import rc_theme
from gui_util import open_path, read_json


class ClickLabel(QLabel):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        pm = QPixmap(str(path))
        self.setPixmap(pm.scaledToWidth(320, Qt.SmoothTransformation))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{path.name}（クリックで大きく開く）")

    def mousePressEvent(self, e):
        open_path(self.path)


class ThumbDialog(QDialog):
    """サムネイル案を並べて表示する"""

    def __init__(self, page: "ReviewPage", tdir: Path):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("サムネイル案")
        info = read_json(tdir / "thumbnails.json", []) or []
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("クリックすると大きく開きます。気に入ったものは「YouTubeへ」画面の「サムネイル」で選ぶと、"
                             "動画に自動で設定されます。"))
        grid = QGridLayout()
        import rc_thumb
        import rc_thumbart
        # 並べる順は rc_thumb.grid_files（AIに指示するとき、「案2の右端」などがAIにも同じ位置で伝わる）
        for r, (no, files) in enumerate(rc_thumb.grid_files(tdir).items()):
            meta = next((i for i in info if str(i.get("image")) == no), {})
            look = rc_thumbart.describe(meta)
            when = ""
            if meta.get("video_time") is not None:   # 完成動画のどこの場面か
                when = f"（{rc_thumb.fmt_time(meta['video_time'])}{'・選んだ場面' if meta.get('chosen') else ''}）"
            cap = QLabel(f"案{no}{when}：「{meta.get('text', '').replace(chr(92) + 'n', ' ').replace(chr(10), ' ')}」  "
                         f"{meta.get('reason', '')}"
                         + (f"\n　デザイン: {look}" if look else ""))
            cap.setStyleSheet("font-weight: bold; margin-top: 6px;")
            grid.addWidget(cap, r * 2, 0, 1, 3)
            for c, f in enumerate(files):
                grid.addWidget(ClickLabel(f), r * 2 + 1, c)
        lay.addLayout(grid)
        lay.addStretch(1)
        # AIに指示して作り直す（前回の案の画像と、これまでの指示もAIに見せる）
        api = page.win.create.llm.currentData() == "api"
        made = rc_thumb.requests_made(tdir)
        if made:
            self.made = QLabel("反映ずみの指示: " + " ／ ".join(made[-3:]))
            self.made.setWordWrap(True)
            rc_theme.style(self.made, role="sub")
        ask_row = QHBoxLayout()
        self.request = QLineEdit()
        self.request.setPlaceholderText("例: 案2は文字を左に・もっと明るく・バッジは要らない・真ん中の段の右端は別の場面で")
        self.request.returnPressed.connect(self._ask)
        self.b_ask = QPushButton("AIに指示して作り直す")
        self.b_ask.clicked.connect(self._ask)
        if api:
            self.b_ask.setToolTip("今の案の画像と、これまでの指示もAIに見せて作り直します（指示は積み重なります。"
                                  "「別の場面から選び直す」などで作り直すと、まっさらに戻ります）")
        else:
            for w in (self.request, self.b_ask):
                w.setEnabled(False)
                w.setToolTip("「作成」タブの AI を「Claude APIで自動」にすると、AIに指示して作り直せます。")
        ask_row.addWidget(self.request, 1)
        ask_row.addWidget(self.b_ask)
        # 作り直しの入口は1か所にまとめる（AIに指示 / 文字を決める / 場面を変える）
        box = QGroupBox("作り直す")
        form = QFormLayout(box)
        if made:
            form.addRow("", self.made)
        form.addRow("AIに指示", ask_row)
        row = QHBoxLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("例: 軽すぎ！　（2行にするときは \\n で区切る）")
        self.text.returnPressed.connect(self._redo)
        redo = QPushButton("この文字で作り直す")
        redo.clicked.connect(self._redo)
        row.addWidget(self.text, 1)
        row.addWidget(redo)
        form.addRow("文字を決める", row)
        # 場面を変える: 自分で選ぶ / 今の案とは別の場面から選び直す
        row2 = QHBoxLayout()
        self.b_scenes = QPushButton("場面を選んで作り直す…")
        self.b_scenes.setToolTip("動画から取り出した場面を並べて表示します。使いたい場面を選ぶか、完成動画の時刻で指定できます。")
        self.b_scenes.clicked.connect(lambda: (self.accept(), SceneDialog(self.page, tdir).exec()))
        self.b_again = QPushButton("別の場面から選び直す" + ("（AI）" if api else ""))
        self.b_again.setToolTip("今の案に使っている場面を外し、動画全体から新しく取り出した場面から"
                                + ("AIが選び直します。" if api else "選び直します（AIなしなので、くっきりした場面を自動で）。"))
        self.b_again.clicked.connect(lambda: (self.accept(), self.page.thumbnails(fresh=True)))
        folder = QPushButton("フォルダを開く")
        folder.clicked.connect(lambda: open_path(tdir))
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        for b in (self.b_scenes, self.b_again):
            row2.addWidget(b)
        row2.addStretch(1)
        form.addRow("場面を変える", row2)
        lay.addWidget(box)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        for b in (folder, close):
            bottom.addWidget(b)
        lay.addLayout(bottom)

    def _redo(self):
        t = self.text.text().strip()
        if t:
            self.accept()
            self.page.thumbnails(text=t)

    def _ask(self):
        t = self.request.text().strip()
        if t and self.b_ask.isEnabled():
            self.accept()
            self.page.thumbnails(request=t)


class SceneDialog(QDialog):
    """サムネイルに使う場面を選ぶ。動画から取り出した場面（thumbnails/candidates.json）を、完成動画の時刻の順に並べる。
    完成動画の時刻を入れても選べる。足りない分は、AI（AIなしなら自動）がほかの場面から選ぶ"""
    MAX = 3
    MARKS = "①②③"

    def __init__(self, page: "ReviewPage", tdir: Path):
        super().__init__(page)
        import rc_thumb
        self.T = rc_thumb
        self.page = page
        self.setWindowTitle("サムネイルの場面を選ぶ")
        self.resize(1240, 860)
        lay = QVBoxLayout(self)
        api = page.win.create.llm.currentData() == "api"
        lay.addWidget(QLabel(
            "サムネイルに使いたい場面をクリックして選んでください（3つまで。選んだ順に案1・案2・案3になります。もう一度押すと外れます）。\n"
            + ("文字とデザインはAIが付けます。" if api else "文字は動画のタイトル案から付けます。")
            + "選んだ場面が3つより少ないときは、残りを" + ("AIが" if api else "自動で") + "ほかの場面から選びます。"))
        pool = rc_thumb.load_pool(tdir) if rc_thumb.pool_valid(page.out) else {}
        work = tdir / "_candidates"
        cands = sorted((c for c in pool.get("cands", []) if (work / str(c.get("file", ""))).exists()),
                       key=lambda c: c.get("ot", 0))
        used = rc_thumb._used_times(tdir)
        self.order: list[QToolButton] = []
        self.tiles: list[QToolButton] = []
        if cands:
            box = QWidget()
            grid = QGridLayout(box)
            grid.setSpacing(8)
            for k, c in enumerate(cands):
                b = QToolButton()
                b.setCheckable(True)
                b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                b.setIcon(QIcon(QPixmap(str(work / c["file"])).scaledToWidth(272, Qt.SmoothTransformation)))
                b.setIconSize(QSize(272, 153))
                now = any(abs(float(c["t"]) - u) < 0.05 for u in used)
                b.setProperty("ot", float(c.get("ot", 0.0)))
                b.setProperty("cap", f"{rc_thumb.fmt_time(c.get('ot', 0))}{'（今の案）' if now else ''}")
                b.setText(b.property("cap"))
                b.setToolTip(f"{c.get('why', '')}（右クリックで大きく開く）")
                rc_theme.style(b, role="tile")
                b.toggled.connect(lambda on, b=b: self._toggle(b, on))
                b.setContextMenuPolicy(Qt.CustomContextMenu)
                b.customContextMenuRequested.connect(lambda _p, f=work / c["file"]: open_path(f))
                grid.addWidget(b, k // 4, k % 4)
                self.tiles.append(b)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(box)
            lay.addWidget(scroll, 1)
        else:
            empty = QLabel("場面の一覧がまだありません（カットを変えたときも作り直しが要ります）。"
                           "「ほかの場面も出す」を押すと、動画全体から場面を取り出します。")
            empty.setWordWrap(True)
            lay.addWidget(empty)
            lay.addStretch(1)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("時刻で指定:"))
        self.times = QLineEdit()
        self.times.setPlaceholderText("完成動画の時刻（例: 1:23, 4:56）。上で選んだ場面の後に続きます")
        self.times.returnPressed.connect(self._make)
        trow.addWidget(self.times, 1)
        lay.addLayout(trow)
        self.status = QLabel("")
        rc_theme.style(self.status, role="error")
        lay.addWidget(self.status)
        row = QHBoxLayout()
        more = QPushButton(f"ほかの場面も出す（＋{rc_thumb.N_CANDIDATES}）")
        more.setToolTip("今の一覧とは別の所を、動画全体から取り出して足します（サムネイル案は作り直しません）。")
        more.clicked.connect(lambda: (self.accept(), self.page.thumbnails(more=True)))
        self.b_make = QPushButton("選んだ場面で作る")
        rc_theme.style(self.b_make, kind="primary")
        self.b_make.setDefault(True)
        self.b_make.clicked.connect(self._make)
        close = QPushButton("閉じる")
        close.clicked.connect(self.reject)
        row.addWidget(more)
        row.addStretch(1)
        row.addWidget(self.b_make)
        row.addWidget(close)
        lay.addLayout(row)

    def _toggle(self, b: QToolButton, on: bool):
        if on and b not in self.order:
            if len(self.order) >= self.MAX:
                b.blockSignals(True)
                b.setChecked(False)
                b.blockSignals(False)
                self.status.setText(f"選べるのは{self.MAX}つまでです。外したい場面をもう一度押してください。")
                return
            self.order.append(b)
        elif not on and b in self.order:
            self.order.remove(b)
        self.status.setText("")
        for t in self.tiles:
            k = self.order.index(t) if t in self.order else -1
            t.setText((f"{self.MARKS[k]} " if k >= 0 else "") + t.property("cap"))

    def scenes(self) -> list[float] | None:
        """選んだ場面（完成動画の時刻・秒）。おかしいときは理由を出して None"""
        out = [float(b.property("ot")) for b in self.order]
        try:
            typed = self.T.parse_times(self.times.text())
        except ValueError as e:
            self.status.setText(str(e))
            return None
        for t in typed:
            if all(abs(t - u) > 0.5 for u in out):
                out.append(t)
        if not out:
            self.status.setText("場面をクリックして選ぶか、時刻を入れてください。")
            return None
        if len(out) > self.MAX:
            self.status.setText(f"場面は{self.MAX}つまでです（今 {len(out)}つ）。")
            return None
        return out

    def _make(self):
        s = self.scenes()
        if s:
            self.accept()
            self.page.thumbnails(scenes=s)
