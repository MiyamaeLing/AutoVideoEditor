"""GUIの拡張：オープニング・エンディングの自動生成と、テロップの種類の見本。

「ツール」メニューに項目を足し、「確認・仕上げ」タブのテロップの表に、カード型テロップの書き方のヒントを出す。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

import rc_graphics

HINT = ("テロップの種類と、文字の書き方（「／」で改行）:\n"
        "  吹き出し … なにこれ！？\n"
        "  スペック表 … 見出し／重さ：1.2kg／価格：9,800円\n"
        "  比較表 … ｜A社｜B社／重さ｜1.2kg｜1.5kg（「｜」で列を区切る）\n"
        "  STEPカード … STEP 2 穴をあける（番号を省くと順に数えます）\n"
        "  場所カード … 高尾山 山頂／標高599m")


def _open(p: Path):
    from gui import open_path
    open_path(p)


class GraphicsDialog(QDialog):
    """作った動画の静止画を並べて見せる"""

    def __init__(self, parent, title: str, images: list[Path], videos: list[tuple[str, Path]], tdir: Path,
                 note: str = "", toggles: tuple[str, ...] = ()):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        if note:
            lab = QLabel(note)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        row = QHBoxLayout()
        for p in images:
            lab = QLabel()
            pm = QPixmap(str(p))
            if not pm.isNull():
                lab.setPixmap(pm.scaledToWidth(420 if len(images) <= 2 else 300, Qt.SmoothTransformation))
            else:
                lab.setText(f"（{p.name} がありません）")
            row.addWidget(lab)
        lay.addLayout(row)
        # 完成動画で使うかどうか（template.toml の enabled を、ここで切り替えられる）
        self.toggles = {}
        if toggles:
            tl = QHBoxLayout()
            tl.addWidget(QLabel(f"テンプレート「{tdir.name}」で:"))
            for sec in toggles:
                cb = QCheckBox(rc_graphics.TOGGLES.get(sec, sec))
                cb.setChecked(rc_graphics.get_enabled(tdir, sec))
                cb.toggled.connect(lambda on, sec=sec, cb=cb: self._toggle(tdir, sec, on, cb))
                tl.addWidget(cb)
                self.toggles[sec] = cb
            tl.addStretch()
            lay.addLayout(tl)
        bb = QHBoxLayout()
        for text, v in videos:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, v=v: _open(v))
            bb.addWidget(b)
        edit = QPushButton("template.toml を開く（見た目を変える）")
        edit.clicked.connect(lambda: _open(tdir / "template.toml"))
        bb.addWidget(edit)
        bb.addStretch()
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        bb.addWidget(close)
        lay.addLayout(bb)


    def _toggle(self, tdir: Path, sec: str, on: bool, cb):
        try:
            ok = rc_graphics.set_enabled(tdir, sec, on)
        except OSError as e:
            ok = False
            QMessageBox.warning(self, "保存できません", f"template.toml を書き換えられませんでした: {e}")
        if not ok:
            cb.blockSignals(True)
            cb.setChecked(rc_graphics.get_enabled(tdir, sec))
            cb.blockSignals(False)


def _template_dir(win) -> Path | None:
    try:
        d = win.create.template.currentData()
    except AttributeError:
        return None
    return Path(d) if d else None


def make_clips(win):
    tdir = _template_dir(win)
    if not tdir:
        QMessageBox.information(win, "テンプレートがありません", "「作成」タブでテンプレートを選んでください。")
        return
    gen = tdir / rc_graphics.GEN_DIR

    def done(ok):
        if not ok:
            return
        GraphicsDialog(win, "オープニング・エンディング", [gen / "intro_preview.png", gen / "outro_preview.png"],
                       [("オープニングを再生", gen / "intro.mp4"), ("エンディングを再生", gen / "outro.mp4")], tdir,
                       "下のチェックを入れると、完成動画の最初と最後に自動で付きます"
                       "（テンプレートの [opening] / [ending] の enabled が切り替わります）。"
                       "エンディングの枠には、YouTube Studio の「終了画面」で動画とチャンネル登録の要素を置いてください"
                       "（位置は generated フォルダの「outro_終了画面の枠.json」）。", ("opening", "ending")).exec()
    win.runner.start("graphics.py", ["--template", str(tdir)], "オープニング・エンディングを作成中", done)


def make_samples(win):
    tdir = _template_dir(win)
    if not tdir:
        QMessageBox.information(win, "テンプレートがありません", "「作成」タブでテンプレートを選んでください。")
        return
    gen = tdir / rc_graphics.GEN_DIR

    def done(ok):
        if not ok:
            return
        GraphicsDialog(win, "テロップの種類と章の進み具合の見本",
                       [gen / f"telop_sample_{i}.png" for i in range(1, 5)],
                       [("動画で見る", gen / "telop_samples.mp4")], tdir, HINT, ("progress",)).exec()
    win.runner.start("graphics.py", ["--template", str(tdir), "--samples"], "テロップの見本を作成中", done)


def register(win):
    a1 = win.tools_menu.addAction("オープニング・エンディングを作る（テンプレートから）")
    a1.triggered.connect(lambda: make_clips(win))
    a2 = win.tools_menu.addAction("テロップの種類と章の進み具合の見本を作る")
    a2.triggered.connect(lambda: make_samples(win))
    # 演出の表で、テロップの文字の欄にカーソルを合わせると書き方が出るようにする
    table = getattr(getattr(win, "review", None), "t_telop", None)
    if table is not None:
        table.setToolTip(HINT)
    win.graphics_actions = (a1, a2)
    if hasattr(win, "lock_while_busy"):
        for a in (a1, a2):
            win.lock_while_busy(a)
