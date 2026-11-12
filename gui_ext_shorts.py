"""画面（GUI）の拡張：縦型ショート

「確認・仕上げ」タブに「ショートを作る」ボタンと、できたショートの一覧（再生・フォルダを開く）を追加する。
中身の処理は shorts.py をそのまま呼び出している。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

TIP = ("本編の中から、ショートに向いた所（30〜60秒・最初につかみがある所）を選んで、"
       "縦型（1080x1920）の動画を最大3本作ります。人の顔を追いかけて切り抜き、大きな字幕とタイトルを入れます。")


def _gui(win):
    """gui.py のモジュール（python gui.py で起動したときは __main__）"""
    return sys.modules[type(win).__module__]


def _fmt(t: float) -> str:
    t = max(0, int(t))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


WHOLE_TIP = ("区間を選ばず、カットした素材全体を1本の縦型ショート（1080x1920）にします（ショートだけ作りたいとき）。\n"
             "演出プランがあれば、強調テロップとBGMも入ります（先に「演出プランを作る」を押してください）。")


def read_whole(out: Path | None) -> dict | None:
    """素材全体を1本にしたショート（short_whole.json）。動画が無ければ None"""
    if not out:
        return None
    try:
        s = json.loads((out / "short_whole.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) and (out / str(s.get("file", ""))).exists() else None


def read_shorts(out: Path | None) -> list[dict]:
    if not out:
        return []
    try:
        data = json.loads((out / "shorts" / "shorts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data.get("shorts", []) if (out / "shorts" / s.get("file", "")).exists()]


class ShortsDialog(QDialog):
    """できたショートを並べて表示する"""

    def __init__(self, ext: "ShortsExt"):
        super().__init__(ext.win)
        self.ext = ext
        g = _gui(ext.win)
        out = ext.win.review.out
        sdir = out / "shorts"
        self.setWindowTitle("縦型ショート")
        self.resize(1000, 660)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("「再生」で確認できます。タイトルと説明文（#shorts 付き）は「コピー」を押して、"
                             "YouTube Studio にそのまま貼り付けられます。"))
        row = QHBoxLayout()
        shorts = read_shorts(out)
        whole = read_whole(out)
        if whole:
            row.addWidget(self._card(g, out, whole))
        for s in shorts:
            row.addWidget(self._card(g, sdir, s))
        if not shorts and not whole:
            row.addWidget(QLabel("まだショートがありません。「ショートを作る」を押してください。"))
        row.addStretch(1)
        box = QWidget()
        box.setLayout(row)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(box)
        lay.addWidget(sc, 1)
        bl = QHBoxLayout()
        folder = QPushButton("フォルダを開く")
        folder.clicked.connect(lambda: g.open_path(sdir))
        redo = QPushButton("作り直す")
        redo.setToolTip("今の設定（「作成」タブの AI の欄）で、本編から切り出すショートを作り直します")
        redo.clicked.connect(lambda: (self.accept(), self.ext.make()))
        redo_whole = QPushButton("全体のショートを作り直す")
        redo_whole.setToolTip(WHOLE_TIP)
        redo_whole.clicked.connect(lambda: (self.accept(), self.ext.make_whole()))
        redo_whole.setVisible(bool(whole))
        if whole and not shorts:
            folder.clicked.disconnect()
            folder.clicked.connect(lambda: g.open_path(out))
            redo.setVisible(False)
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        bl.addStretch(1)
        for b in (folder, redo_whole, redo, close):
            bl.addWidget(b)
        lay.addLayout(bl)

    def _card(self, g, sdir: Path, s: dict) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setFixedWidth(300)
        cl = QVBoxLayout(card)
        img = QLabel()
        img.setAlignment(Qt.AlignCenter)
        pm = QPixmap(str(sdir / s.get("thumb", "")))
        if not pm.isNull():
            img.setPixmap(pm.scaled(180, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            img.setText("（画像なし）")
            img.setMinimumHeight(320)
        cl.addWidget(img)
        is_whole = s.get("by") == "whole"
        title = QLabel(s.get("title", "") if is_whole else f"{s.get('no', '')}. {s.get('title', '')}")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 11pt;")
        cl.addWidget(title)
        by = "AIが選んだ区間" if s.get("by") == "ai" else "見どころから自動で選んだ区間"
        info = QLabel(f"{s.get('duration', 0):.0f}秒 ／ 本編 {_fmt(s.get('start', 0))}〜{_fmt(s.get('end', 0))}\n{by}")
        if is_whole:
            info.setText(f"{_fmt(s.get('duration', 0))} ／ 素材全体を1本に{'・BGMあり' if s.get('bgm') else ''}"
                         + ("\n⚠ 3分を超えています（YouTubeではショートになりません）" if s.get("over_limit") else ""))
        info.setProperty("role", "sub")
        cl.addWidget(info)
        desc = QPlainTextEdit(s.get("description", ""))
        desc.setReadOnly(True)
        desc.setFixedHeight(90)
        cl.addWidget(desc)
        cl.addStretch(1)
        br = QHBoxLayout()
        play = QPushButton("▶ 再生")
        play.setStyleSheet("QPushButton { background: #2B6CB0; color: white; font-weight: bold; }")
        play.clicked.connect(lambda: g.open_path(sdir / s["file"]))
        copy = QPushButton("コピー")
        copy.setToolTip("タイトルと説明文をコピーします")
        copy.clicked.connect(lambda: (QGuiApplication.clipboard().setText(f"{s.get('title', '')}\n\n"
                                                                          f"{s.get('description', '')}"),
                                      copy.setText("コピーしました")))
        br.addWidget(play, 1)
        br.addWidget(copy)
        cl.addLayout(br)
        return card


class ShortsExt:
    def __init__(self, win):
        self.win = win
        rv = win.review
        self.btn = QPushButton("ショートを作る")
        self.btn.setToolTip(TIP)
        self.btn.setStyleSheet("QPushButton { background: #D53F8C; color: white; font-weight: bold; }"
                               " QPushButton:disabled { background: #CBD5E0; color: #F7FAFC; }")
        self.btn.clicked.connect(lambda: self.make())
        self.count = QSpinBox()
        self.count.setRange(1, 3)
        self.count.setValue(3)
        self.count.setPrefix("最大 ")
        self.count.setSuffix(" 本")
        self.count.setToolTip("作るショートの本数の上限です（良い所が少なければ、少なくなります）")
        self.whole = QPushButton("全体を1本のショートに")
        self.whole.setToolTip(WHOLE_TIP)
        self.whole.setStyleSheet("QPushButton { background: #B83280; color: white; font-weight: bold; }"
                                 " QPushButton:disabled { background: #CBD5E0; color: #F7FAFC; }")
        self.whole.clicked.connect(lambda: self.make_whole())
        self.view = QPushButton("ショートを見る")
        self.view.clicked.connect(self.show_list)
        for i, w in enumerate((self.btn, self.count, self.whole, self.view)):
            rv.ext_bar.insertWidget(i, w)
        # 「確認・仕上げ」タブのボタンの状態が変わるとき（読み込み・処理の開始と終了）に、こちらも合わせる
        orig = rv._update_buttons

        def update():
            orig()
            self.refresh()
        rv._update_buttons = update
        self.refresh()

    def refresh(self):
        rv = self.win.review
        busy = self.win.runner.busy
        has = rv.out is not None
        self.btn.setEnabled(has and not busy)
        self.count.setEnabled(has and not busy)
        self.whole.setEnabled(has and not busy)
        n = len(read_shorts(rv.out)) + (1 if read_whole(rv.out) else 0)
        self.view.setEnabled(n > 0 and not busy)
        self.view.setText(f"ショートを見る（{n}本）" if n else "ショートを見る")

    def show_list(self):
        if self.win.review.out:
            ShortsDialog(self).exec()

    def make_whole(self, plan: bool = False):
        """カットした素材全体を、1本の縦型ショートにする。plan=True なら、演出プランが無ければ先に作る（一気に作るとき）"""
        rv = self.win.review
        if not rv.out:
            return
        if rv._dirty():   # カットの変更がまだ反映されていなければ、先に反映する
            rv.apply_cuts(then=lambda: self.make_whole(plan))
            return
        out = rv.out
        if plan and not any((out / n).exists() for n in ("plan_edited.json", "plan_used.json")):
            rv.make_plan(then=lambda: self.make_whole())   # テロップ・BGMを入れるため
            return
        rv._save_plan_edits()   # 演出の表の手直し（テロップ）もショートに使う
        llm = self.win.create.llm.currentData()
        args = [str(out), "--template", rv._template(), "--whole", "--llm", llm,
                "--effort", self.win.create.effort.currentData()]

        def done(ok):
            rv._update_buttons()
            if ok and read_whole(out):
                self.show_list()
            elif ok:
                QMessageBox.information(self.win, "ショート", "ショートを作れませんでした。下のログを確認してください。")
        self.win.runner.start("shorts.py", args, "縦型ショート（素材全体を1本に）を作成中", done)

    def make(self, picks: Path | None = None):
        rv = self.win.review
        if not rv.out:
            return
        llm = self.win.create.llm.currentData()
        if picks is None and llm == "api" and not _gui(self.win).has_api_key(self.win.settings):
            # APIキーがないときは、AIを使わずに見どころから選ぶ（README のとおり）。先に確かめる
            if QMessageBox.question(self, "APIキーが未設定です",
                                    "Claude の APIキーが設定されていません。\n"
                                    "AIを使わずに、見どころ（盛り上がった所）から自動で選んで作りますか？") != QMessageBox.Yes:
                return
            llm = "none"
        if rv._dirty():   # カットの変更がまだ反映されていなければ、先に反映する
            rv.apply_cuts(then=lambda: self.make(picks))
            return
        rv._save_plan_edits()   # 演出の表の手直し（テロップ）もショートに使う
        out = rv.out
        args = [str(out), "--template", rv._template(), "--count", str(self.count.value()),
                "--effort", self.win.create.effort.currentData()]
        if picks:
            args += ["--picks", str(picks)]
        else:
            args += ["--llm", llm]
        prompt = out / "shorts" / "prompt_shorts.md"
        if llm == "manual" and not picks:
            prompt.unlink(missing_ok=True)

        def done(ok):
            rv._update_buttons()
            if not ok:
                return
            if llm == "manual" and not picks and prompt.exists():
                g = _gui(self.win)
                dlg = g.ManualDialog(self.win, prompt, "ショートの区間を選ぶ")
                if dlg.exec():
                    p = out / "shorts" / "claude_shorts.json"
                    p.write_text(dlg.text(), encoding="utf-8")
                    self.make(picks=p)
                return
            if read_shorts(out):
                self.show_list()
            else:
                QMessageBox.information(self.win, "ショート", "ショートを作れませんでした。下のログを確認してください。")
        self.win.runner.start("shorts.py", args, "縦型ショートを作成中", done)


def register(win):
    win.shorts_ext = ShortsExt(win)
