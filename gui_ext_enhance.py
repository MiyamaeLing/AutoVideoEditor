"""画面の拡張: 声と映像の補正（rc_enhance）の切り替え。

- 「作成」タブの「詳細設定」に「声と映像の補正」の行を足す（声を聞き取りやすく / 明るさ・色 / 手ブレ補正）
  選んだ内容は、finish.py などを動かすときに環境変数 AVE_ENHANCE で渡す（テンプレートより優先）
- 「色の見本…」: 素材の1場面で、色の補正の前後を並べた画像を見る
- 「確認・仕上げ」タブに「2台目の色合わせ」ボタン: 2台目カメラの色を本編に寄せる LUT（Premiere 用）を作る
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

import rc_enhance

CHOICES = [("auto", "おまかせ"), ("on", "使う"), ("off", "使わない")]
ITEMS = [
    ("audio_clean", "声を聞き取りやすく",
     "低音カット・ノイズ除去・サ行の刺さりを抑える・声の大きさをならす。\n"
     "おまかせ = テンプレートの [audio_clean] に従う（初期値は「使う」。屋外企画は風切り音対策を強め）"),
    ("color", "明るさ・色",
     "明るさと色かぶりを自動で少し整える（テンプレートの LUT も使う）。\n"
     "おまかせ = テンプレートの [color] に従う（初期値は自動補正なし）"),
    ("stabilize", "手ブレ補正",
     "歩き撮りなどの揺れをおさえる（少し拡大されます。書き出しに時間がかかります）。\n"
     "おまかせ = テンプレートの [stabilize] に従う（初期値は屋外企画だけ使う）"),
]


def current(settings) -> dict:
    """設定に保存した選択（おまかせ以外）を {"audio_clean": True, ...} の形で返す"""
    out = {}
    for key, _, _ in ITEMS:
        v = settings.value(f"enhance/{key}", "auto")
        if v in ("on", "off"):
            out[key] = v == "on"
    return out


class EnhanceRow(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combos = {}
        for key, label, tip in ITEMS:
            cb = QComboBox()
            for k, v in CHOICES:
                cb.addItem(v, k)
            cb.setCurrentIndex(max(0, cb.findData(win.settings.value(f"enhance/{key}", "auto"))))
            cb.setToolTip(tip)
            cb.currentIndexChanged.connect(lambda _i, k=key, c=cb: win.settings.setValue(f"enhance/{k}", c.currentData()))
            lab = QLabel(label)
            lab.setToolTip(tip)
            lay.addWidget(lab)
            lay.addWidget(cb)
            self.combos[key] = cb
        self.sample = QPushButton("色の見本…")
        self.sample.setToolTip("1つ目の素材の真ん中の場面で、色の補正の前と後を並べて表示します")
        self.sample.clicked.connect(self.show_sample)
        lay.addWidget(self.sample)
        lay.addStretch()

    def show_sample(self):
        c = self.win.create
        if not c.files.count():
            QMessageBox.information(self, "素材がありません", "素材の動画を入れてから押してください。")
            return
        video = c.files.paths()[0]
        out = video.parent / f"{video.stem}_補正の比較.jpg"
        args = ["compare", str(video), "--template", str(c.template.currentData()), "--mode", c.mode(),
                "--out", str(out)]
        if self.combos["color"].currentData() == "off":
            QMessageBox.information(self, "色の補正を使わない設定です",
                                    "「明るさ・色」を「おまかせ」か「使う」にしてから押してください。")
            return

        def done(ok):
            if ok and out.exists():
                SampleDialog(self, out).exec()
        self.win.runner.start("enhance.py", args, "色の補正の見本を作成中", done)


class SampleDialog(QDialog):
    def __init__(self, parent, img: Path):
        super().__init__(parent)
        self.setWindowTitle("色の補正の見本（左: そのまま / 右: 補正後）")
        lay = QVBoxLayout(self)
        pm = QPixmap(str(img))
        pm = pm.scaledToWidth(min(pm.width(), 1100), Qt.SmoothTransformation)
        heads = QHBoxLayout()
        for text in ("そのまま", "補正後"):
            lab = QLabel(text)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("font-weight: bold;")
            heads.addWidget(lab)
        lay.addLayout(heads)
        pic = QLabel()
        pic.setPixmap(pm)
        lay.addWidget(pic)
        note = QLabel("補正の強さや LUT は、テンプレートの template.toml の [color] で変えられます。")
        note.setStyleSheet("color: #555;")
        lay.addWidget(note)
        bb = QDialogButtonBox()
        close = bb.addButton("閉じる", QDialogButtonBox.RejectRole)
        close.setDefault(True)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def match_cam2(win):
    rv = win.review
    if not getattr(rv, "out", None):
        QMessageBox.information(rv, "結果がありません", "先に荒編集をするか、「ほかの結果を開く」で結果を開いてください。")
        return
    sess = getattr(rv, "sess", None) or {}
    if not (sess.get("args") or {}).get("cam2"):
        QMessageBox.information(rv, "2台目の素材がありません",
                                "この結果には2台目カメラの素材がありません。\n"
                                "「作成」タブの「2台目を追加…」で素材を入れて、荒編集し直してください。")
        return
    out = Path(rv.out)

    def done(ok):
        dst = out / "cam2_色合わせ.cube"
        if ok and dst.exists():
            QMessageBox.information(rv, "2台目の色合わせ",
                                    f"{dst.name} を作りました（結果のフォルダにあります）。\n\n"
                                    "Premiere Pro での使い方:\n"
                                    "V2（2台目）のクリップを選ぶ → Lumetri カラー → 基本補正 →\n"
                                    "「入力LUT」の「参照…」でこのファイルを選びます。")
    win.runner.start("enhance.py", ["cam2", str(out)], "2台目カメラの色合わせ（LUT）を作成中", done)


def register(win):
    # ---- 「作成」タブの詳細設定に行を足す ----
    c = win.create
    form = c.adv.parentWidget().layout()
    row, _role = form.getWidgetPosition(c.adv)
    win.enhance_row = EnhanceRow(win)
    form.insertRow(row + 1, QLabel("声と映像の補正"), win.enhance_row)
    form.setRowVisible(win.enhance_row, c.adv.isChecked())
    c.adv.toggled.connect(lambda on: form.setRowVisible(win.enhance_row, on))

    # ---- 書き出しなどのときに、選んだ内容を渡す ----
    orig_env = win.runner.env

    def env():
        e = orig_env()
        e.insert(rc_enhance.ENV_NAME, json.dumps(current(win.settings)))
        return e
    win.runner.env = env

    # ---- 「確認・仕上げ」タブ ----
    b = QPushButton("2台目の色合わせ")
    b.setToolTip("2台目カメラ（スマホなど）の色を本編に寄せる LUT（.cube）を作ります。Premiere の Lumetri で使えます")
    b.clicked.connect(lambda: match_cam2(win))
    win.review.ext_bar.insertWidget(0, b)
