"""GUIの拡張: 人物の切り抜きサムネイル（rc_thumbspro）

「ツール」メニューに次の2つを追加する。
  - 人物の切り抜きサムネイルの準備 … 切り抜き用のデータを先にダウンロードして、使えるか確かめる
  - 人物の切り抜きデザインを画像で試す… … 好きな写真で、背景5種類のデザインを作って並べて見せる
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QDialog, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

STYLE_NAMES = {"blur": "ぼかし", "vivid": "鮮やか", "burst": "集中線", "dark": "暗く", "color": "単色"}


def _template(win) -> str:
    try:
        return win.create.template.currentData() or ""
    except AttributeError:
        return ""


class TryResult(QDialog):
    """お試しで作ったデザインを並べて見せる"""

    def __init__(self, win, folder: Path):
        super().__init__(win)
        import gui
        self.setWindowTitle("人物の切り抜きデザイン（お試し）")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("クリックすると大きく開きます。背景の種類は、テンプレートの [thumbnail.pro] の "
                             "backgrounds で選べます（案1・案2・案3の順に使われます）。"))
        grid = QGridLayout()
        files = [folder / f"person_{k}.jpg" for k in STYLE_NAMES if (folder / f"person_{k}.jpg").exists()]
        for i, f in enumerate(files):
            box = QVBoxLayout()
            box.addWidget(gui.ClickLabel(f))
            key = f.stem.split("_", 1)[1]
            box.addWidget(QLabel(f"{STYLE_NAMES.get(key, key)}（{key}）"))
            grid.addLayout(box, i // 3, i % 3)
        lay.addLayout(grid)
        row = QHBoxLayout()
        row.addStretch()
        b = QPushButton("フォルダを開く")
        b.clicked.connect(lambda: gui.open_path(folder))
        c = QPushButton("閉じる")
        c.clicked.connect(self.accept)
        row.addWidget(b)
        row.addWidget(c)
        lay.addLayout(row)


def register(win):
    menu = win.tools_menu

    def setup():
        args = ["--setup"]
        if _template(win):
            args += ["--template", _template(win)]
        win.runner.start("thumbspro.py", args, "人物の切り抜きの準備")

    def try_image():
        img, _ = QFileDialog.getOpenFileName(win, "人物が写った写真を選ぶ", win.settings.value("last_dir", ""),
                                             "画像 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not img:
            return
        text, ok = QInputDialog.getText(win, "サムネイルの文字", "入れる文字（2行にするときは \\n で区切る）",
                                        QLineEdit.Normal, "ここに\\n文字")
        if not ok:
            return
        out = Path(img).parent / f"{Path(img).stem}_人物デザイン"
        args = ["--try", img, "--text", text or " ", "--out", str(out)]
        if _template(win):
            args += ["--template", _template(win)]

        def done(ok):
            if ok and out.exists():
                TryResult(win, out).exec()
        win.runner.start("thumbspro.py", args, "人物の切り抜きデザインを作成中", done)

    a1 = menu.addAction("人物の切り抜きサムネイルの準備（初回のダウンロード）")
    a1.setToolTip("サムネイル案で人物を切り抜くためのデータ（約6MB）を先にダウンロードします")
    a1.triggered.connect(setup)
    a2 = menu.addAction("人物の切り抜きデザインを画像で試す…")
    a2.triggered.connect(try_image)
    win.thumbspro_actions = (a1, a2)
    if hasattr(win, "lock_while_busy"):
        for a in (a1, a2):
            win.lock_while_busy(a)
