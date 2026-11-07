"""拡張: 見た目テンプレートの編集画面と、AIによるデザイン案。

「作成」タブの「見た目テンプレート」の横に、2つのボタンを足す。
  - 編集…          … 色・フォント・大きさ・位置・縁取りを画面で変えて、見本画像ですぐ確かめる
  - AIでデザイン案… … チャンネルの雰囲気を入れると、Claude が3つの案を作る。並べて比べて保存する

見本画像は、本番の書き出しと同じ字幕ファイル（rc_ass）と FFmpeg の ass フィルターで作る。
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

import rc_design
import rc_thumb

APP_DIR = Path(__file__).resolve().parent
MEDIA_FILTER = ("画像・動画 (*.png *.jpg *.jpeg *.webp *.bmp *.mp4 *.MP4 *.mov *.MOV *.mts *.MTS *.m2ts *.mkv *.avi);;"
                "すべて (*.*)")
VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".mkv", ".avi", ".webm"}
MOOD_CHIPS = ["落ち着いた", "ポップ", "ガジェット系", "ナチュラル", "高級感", "かわいい", "レトロ", "音楽・MV"]

# 各部分の初期値（template.toml に書いていないときに rc_ass が使う値と同じ）
DEFAULTS = {"font": "BIZ UDPGothic", "size": 64, "bold": True, "color": "#FFFFFF", "outline_color": "#000000",
            "outline": 4, "shadow": 0, "margin_bottom": 56, "box_color": "#2B6CB0", "position": "top",
            "margin_v": 70, "animation": "pop", "duration": 2.5, "text_color": "#FFE14D", "outer_color": "#FFFFFF",
            "accent_color": "#2B6CB0", "style": rc_thumb.DEFAULT_STYLE, "effort": "high"}

# (キー, 画面の名前, 種類, 追加の情報)
SPEC_CAPTION = [("font", "フォント", "font", None), ("size", "文字の大きさ", "int", (20, 200)),
                ("bold", "太字", "bool", None), ("color", "文字の色", "color", None),
                ("outline_color", "縁取りの色", "color", None), ("outline", "縁取りの太さ", "int", (0, 20)),
                ("shadow", "影の濃さ", "int", (0, 10)), ("margin_bottom", "画面の下からの距離", "int", (0, 500))]
SPEC_TELOP = [("font", "フォント", "font", None), ("size", "文字の大きさ", "int", (20, 220)),
              ("bold", "太字", "bool", None), ("color", "文字の色", "color", None),
              ("look", "見せ方", "look", None), ("outline_color", "縁取りの色", "color", None),
              ("outline", "縁取りの太さ", "int", (0, 20)), ("box_color", "帯の色", "color", None),
              ("position", "位置", "choice", rc_design.POSITIONS),
              ("margin_v", "画面の端からの距離", "int", (0, 500)),
              ("animation", "出かた", "choice", rc_design.ANIMATIONS)]
SPEC_CHAPTER = [("font", "フォント", "font", None), ("size", "文字の大きさ", "int", (20, 220)),
                ("bold", "太字", "bool", None), ("color", "文字の色", "color", None),
                ("box_color", "帯の色", "color", None), ("duration", "表示する秒数", "float", (0.5, 10.0))]
SPEC_THUMB = [("style", "見せ方", "choice", [(k, v["label"]) for k, v in rc_thumb.STYLES.items()]),
              ("font_file", "フォント", "thumbfont", None), ("text_color", "文字の色", "color", None),
              ("outline_color", "縁取り・影の色", "color", None), ("outer_color", "外側の縁取り（ポップ）", "color", None),
              ("accent_color", "帯・線の色", "color", None), ("emphasis_color", "強調する言葉の色", "color", None),
              ("badge_color", "バッジの色", "color", None),
              ("art_director", "デザインもAIに任せる", "bool", "強調する言葉・バッジ・効果・見せ方の組み合わせ"),
              ("effort", "AIの考える深さ", "choice", [("high", "じっくり"), ("medium", "ふつう"), ("low", "速い")])]
# (タブ名, 設定の場所, 項目, 見本のパネル)
SECTIONS = [("字幕", ("caption",), SPEC_CAPTION, "emphasis")] + \
    [(label, ("telop", key), SPEC_TELOP, key) for key, label in rc_design.TELOPS] + \
    [("章カード", ("chapter_card",), SPEC_CHAPTER, "chapter"), ("サムネイル", ("thumbnail",), SPEC_THUMB, "thumb")]


def _gui(win):
    """gui.py（python gui.py で起動したときは __main__）のモジュール"""
    return sys.modules[type(win).__module__]


def _open(win, p: Path):
    _gui(win).open_path(Path(p))


# ============================================================
#  小さな部品
# ============================================================
class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, title: str):
        super().__init__()
        self.title = title
        self.value = "#FFFFFF"
        self.setMinimumWidth(130)
        self.clicked.connect(self._pick)

    def set_value(self, v: str):
        self.value = rc_design.norm_color(v, "#FFFFFF")
        c = QColor(self.value)
        fg = "#000000" if c.lightness() > 140 else "#FFFFFF"
        self.setText(self.value)
        self.setStyleSheet(f"QPushButton {{ background: {self.value}; color: {fg}; border: 1px solid #888;"
                           f" padding: 4px 10px; }}")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.value), self, f"{self.title}を選ぶ")
        if c.isValid():
            self.set_value(c.name().upper())
            self.changed.emit(self.value)


class FontBox(QComboBox):
    """フォントの一覧（フォント名をそのフォントで表示する）"""
    changed = Signal(str)

    def __init__(self, families: list[dict]):
        super().__init__()
        self.setMaxVisibleItems(18)
        self.setMinimumWidth(240)
        for f in families:
            self.addItem(f["label"], f["name"])
            self.setItemData(self.count() - 1, QFont(f["name"], 11), Qt.FontRole)
        self.currentIndexChanged.connect(lambda _i: self.changed.emit(self.currentData() or ""))

    def set_value(self, name: str | None, missing_label: str = ""):
        self.blockSignals(True)
        i = self.findData(name) if name else -1
        if i < 0 and name:
            i = next((k for k in range(self.count()) if str(self.itemData(k)).lower() == name.lower()), -1)
        if i < 0:
            self.insertItem(0, missing_label or f"{name}（このPCにはありません）", name)
            i = 0
        self.setCurrentIndex(i)
        self.blockSignals(False)


class Renderer(QObject):
    """見本画像を裏で作る。作っている間に変更があれば、終わってから最新の内容でもう一度作る"""
    finished = Signal(object, str)
    _done = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.running = False
        self.pending = None
        self._frame_key = None
        self._done.connect(self._on_done)

    def request(self, **kw):
        self.pending = kw
        if not self.running:
            self._start()

    def _start(self):
        kw, self.pending = self.pending, None
        self.running = True
        threading.Thread(target=self._work, args=(kw,), daemon=True).start()

    def _work(self, kw):
        try:
            bg = kw.pop("bg_source", None)
            if bg and bg[0] == "video":
                work = Path(kw["tpl_dir"]) / rc_design.WORK_NAME
                frame = work / "bg_frame.png"
                if self._frame_key != (bg, str(work)) or not frame.exists():   # 同じコマなら取り出し直さない
                    rc_design.extract_frame(Path(bg[1]), float(bg[2]), frame)
                    self._frame_key = (bg, str(work))
                kw["bg"] = frame
            elif bg and bg[0] == "image":
                kw["bg"] = Path(bg[1])
            res, err = rc_design.render_preview(**kw), ""
        except Exception as e:  # noqa: BLE001  見本が作れなくても画面は止めない
            res, err = None, str(e)
        self._done.emit(res, err)

    def _on_done(self, res, err):
        self.running = False
        if self.pending:
            self._start()
        self.finished.emit(res, err)


class ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit()


# ============================================================
#  テンプレートの編集画面
# ============================================================
class TemplateEditor(QDialog):
    def __init__(self, win, tdir: Path, cfg: dict | None = None, title: str = ""):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("見た目テンプレートの編集")
        self.resize(1320, 820)
        self.tdir = Path(tdir).resolve()
        self.is_new = cfg is not None          # AIの案などから開いたとき（まだ保存していない）
        self.cfg = copy.deepcopy(cfg) if cfg is not None else rc_design.load_cfg(self.tdir)
        self.saved = None if self.is_new else copy.deepcopy(self.cfg)
        self.from_title = title
        self.panel = "emphasis"
        self.images: dict = {}
        self.bg_image: Path | None = None
        self._load_fonts()

        # ---- 上: どのテンプレートか ----
        self.tpl_combo = QComboBox()
        self._fill_templates()
        self.tpl_combo.activated.connect(self._switch_template)
        b_folder = QPushButton("フォルダを開く")
        b_folder.clicked.connect(lambda: _open(self.win, self.tdir))
        self.state = QLabel()
        top = QHBoxLayout()
        top.addWidget(QLabel("テンプレート"))
        top.addWidget(self.tpl_combo, 1)
        top.addWidget(b_folder)
        top.addWidget(self.state, 2)

        # ---- 左: 項目 ----
        self.forms: list[SectionForm] = []
        self.tabs = QTabWidget()
        for label, path, spec, panel in SECTIONS:
            f = SectionForm(self, path, spec, panel)
            self.forms.append(f)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(f)
            sc.setFrameShape(QFrame.NoFrame)
            self.tabs.addTab(sc, label)
        self.tabs.currentChanged.connect(lambda i: self._show_panel(self.forms[i].panel))
        note = QLabel("色のボタンを押すと色を選べます。変えるとすぐ右の見本が変わります。\n"
                      "保存するまで、元のテンプレートは変わりません。")
        note.setProperty("role", "sub")
        note.setWordWrap(True)
        left = QVBoxLayout()
        left.addWidget(self.tabs, 1)
        left.addWidget(note)

        # ---- 右: 見本 ----
        self.bg_combo = QComboBox()
        self._fill_backgrounds()
        self.bg_combo.activated.connect(self._bg_changed)
        self.bg_at = QDoubleSpinBox()
        self.bg_at.setRange(0, 6 * 3600)
        self.bg_at.setDecimals(1)
        self.bg_at.setSuffix(" 秒目")
        self.bg_at.setValue(5.0)
        self.bg_at.setKeyboardTracking(False)
        self.bg_at.valueChanged.connect(lambda _v: self.schedule())
        bg_row = QHBoxLayout()
        bg_row.addWidget(QLabel("背景"))
        bg_row.addWidget(self.bg_combo, 1)
        bg_row.addWidget(self.bg_at)
        self.big_title = QLabel()
        self.big_title.setStyleSheet("font-weight: bold;")
        self.big = ClickLabel("見本を作成中…")
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setFixedSize(704, 396)
        self.big.setStyleSheet("background: #202024; color: #DDD;")
        self.big.setToolTip("クリックすると、全部を並べた画像を大きく開きます")
        self.big.clicked.connect(lambda: self.images.get("grid") and _open(self.win, self.images["grid"]))
        strip = QGridLayout()
        strip.setSpacing(4)
        self.thumbs: dict[str, QToolButton] = {}
        for i, (key, label) in enumerate(rc_design.PANELS):
            b = QToolButton()
            b.setText(label.replace("サムネイル", "サムネ"))
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFixedSize(168, 118)
            b.clicked.connect(lambda _c=False, k=key: self._show_panel(k))
            strip.addWidget(b, i // 4, i % 4)
            self.thumbs[key] = b
        self.status = QLabel("")
        self.status.setProperty("role", "sub")
        right = QVBoxLayout()
        right.addLayout(bg_row)
        right.addWidget(self.big_title)
        right.addWidget(self.big)
        right.addLayout(strip)
        right.addWidget(self.status)
        right.addStretch()

        mid = QHBoxLayout()
        mid.addLayout(left, 5)
        mid.addLayout(right, 6)

        # ---- 下: ボタン ----
        b_ai = QPushButton("AIにデザイン案を作ってもらう…")
        b_ai.setStyleSheet("font-weight: bold;")
        b_ai.clicked.connect(self._ai)
        self.b_reset = QPushButton("元に戻す")
        self.b_reset.clicked.connect(self._reset)
        self.b_save = QPushButton("上書き保存")
        self.b_save.clicked.connect(self.save)
        b_saveas = QPushButton("別名で保存…")
        b_saveas.setStyleSheet("background: #2B6CB0; color: white; font-weight: bold; padding: 4px 14px;")
        b_saveas.clicked.connect(self.save_as)
        b_close = QPushButton("閉じる")
        b_close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(b_ai)
        bottom.addWidget(self.b_reset)
        bottom.addStretch()
        bottom.addWidget(self.b_save)
        bottom.addWidget(b_saveas)
        bottom.addWidget(b_close)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(mid, 1)
        lay.addLayout(bottom)

        self.renderer = Renderer()
        self.renderer.finished.connect(self._rendered)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.render_now)
        self._bg_changed()
        self.reload_forms()
        self._show_panel("emphasis")
        self._update_state()
        self.render_now()

    # --- 準備 ---
    def _load_fonts(self):
        tpl = rc_design.MemTemplate(self.cfg, self.tdir)
        extra = [tpl.fonts_dir] if tpl.fonts_dir else []
        self.faces = rc_design.scan_fonts(extra)
        for d in extra:   # テンプレートの fonts フォルダのフォントも、一覧でその字形が見えるようにする
            for p in d.iterdir():
                if p.suffix.lower() in rc_design.FONT_EXT:
                    QFontDatabase.addApplicationFont(str(p))
        self.families = rc_design.font_families(self.faces)

    def _fill_templates(self):
        self.tpl_combo.blockSignals(True)
        self.tpl_combo.clear()
        dirs = rc_design.list_templates(rc_design.TEMPLATES_DIR)
        if self.tdir not in dirs:
            dirs.append(self.tdir)
        for d in dirs:
            self.tpl_combo.addItem(d.name, str(d))
        self.tpl_combo.setCurrentIndex(max(0, self.tpl_combo.findData(str(self.tdir))))
        self.tpl_combo.blockSignals(False)

    def _fill_backgrounds(self):
        self.bg_combo.addItem("サンプル（明るい部屋）", ("sample", "light"))
        self.bg_combo.addItem("サンプル（夜・暗い）", ("sample", "dark"))
        seen = set()
        for p in material_candidates(self.win):
            if str(p) not in seen:
                seen.add(str(p))
                self.bg_combo.addItem(f"素材: {p.name}", ("video", str(p)))
        self.bg_combo.addItem("画像・動画を選ぶ…", ("pick", ""))

    def _bg_changed(self, _i=None):
        kind, val = self.bg_combo.currentData()
        if kind == "pick":
            f, _ = QFileDialog.getOpenFileName(self, "背景にする画像・動画", "", MEDIA_FILTER)
            if not f:
                self.bg_combo.setCurrentIndex(0)
                kind, val = self.bg_combo.currentData()
            else:
                p = Path(f)
                kind = "video" if p.suffix.lower() in VIDEO_EXT else "image"
                self.bg_combo.insertItem(self.bg_combo.count() - 1, f"{'素材' if kind == 'video' else '画像'}: {p.name}",
                                         (kind, str(p)))
                self.bg_combo.setCurrentIndex(self.bg_combo.count() - 2)
        self.bg_at.setEnabled(kind == "video")
        self.schedule()

    def bg_source(self):
        kind, val = self.bg_combo.currentData()
        if kind == "video":
            return ("video", val, self.bg_at.value())
        if kind == "image":
            return ("image", val)
        return None

    def bg_kind(self) -> str:
        kind, val = self.bg_combo.currentData()
        return val if kind == "sample" else "light"

    def reload_forms(self):
        for f in self.forms:
            f.load()

    # --- 変更と見本 ---
    def changed(self, panel: str | None = None):
        if panel and panel != self.panel:
            self._show_panel(panel)
        self._update_state()
        self.schedule()

    def dirty(self) -> bool:
        return self.saved is None or self.cfg != self.saved

    def _update_state(self):
        name = self.tdir.name if not self.is_new else f"新しい案（{self.from_title or '未保存'}）"
        d = self.dirty()
        self.state.setText(f"編集中: {name}" + ("　● まだ保存していない変更があります" if d else "　（保存済み）"))
        self.state.setStyleSheet("color: #C05621;" if d else "color: #2F855A;")
        self.setWindowTitle(f"見た目テンプレートの編集 - {name}" + (" ●" if d else ""))
        self.b_save.setEnabled(d and not self.is_new)
        self.b_reset.setEnabled(d and not self.is_new)

    def schedule(self):
        self.status.setText("見本を作成中…")
        self.timer.start()

    def render_now(self):
        self.renderer.request(cfg=copy.deepcopy(self.cfg), tpl_dir=self.tdir, bg_source=self.bg_source(),
                              bg_kind=self.bg_kind(), faces=self.faces, prefix="editor")

    def _rendered(self, res, err):
        if self.renderer.running or self.timer.isActive():
            return   # もっと新しい見本を作っている途中
        if not res:
            self.status.setText(f"見本を作れませんでした: {err}")
            self.status.setStyleSheet("color: #C53030;")
            return
        self.status.setProperty("role", "sub")
        self.status.setText("見本は本番の書き出しと同じ方法で作っています。")
        self.images = res
        for key, b in self.thumbs.items():
            pm = QPixmap(str(res[key]))
            b.setIcon(pm)
            b.setIconSize(pm.size().scaled(160, 90, Qt.KeepAspectRatio))
        self._show_panel(self.panel)

    def _show_panel(self, key: str):
        self.panel = key
        if key in self.thumbs:
            self.thumbs[key].setChecked(True)
        self.big_title.setText(f"見本: {dict(rc_design.PANELS).get(key, '')}")
        if key in self.images:
            pm = QPixmap(str(self.images[key]))
            self.big.setPixmap(pm.scaled(self.big.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # --- ボタン ---
    def _reset(self):
        if self.saved is None:
            return
        self.cfg = copy.deepcopy(self.saved)
        self.reload_forms()
        self.changed()

    def _ask_discard(self) -> bool:
        """保存していない変更があれば聞く。続けてよければ True"""
        if not self.dirty():
            return True
        r = QMessageBox.question(self, "保存していない変更", "保存していない変更があります。保存しますか？",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard or r == QMessageBox.No

    def _switch_template(self, _i):
        d = Path(self.tpl_combo.currentData())
        if d == self.tdir and not self.is_new:
            return
        if not self._ask_discard():
            self._fill_templates()
            return
        self.load_template(d)

    def load_template(self, d: Path):
        self.tdir = Path(d).resolve()
        self.cfg = rc_design.load_cfg(self.tdir)
        self.saved = copy.deepcopy(self.cfg)
        self.is_new = False
        self._load_fonts()
        for f in self.forms:
            f.rebuild_fonts()
        self._fill_templates()
        self.reload_forms()
        self.changed()

    def save(self) -> bool:
        if self.is_new:
            return self.save_as()
        if self.tdir.name == "default" and QMessageBox.question(
                self, "default を書き換えます",
                "最初から入っている default テンプレートを書き換えます。\n"
                "（「別名で保存」で新しいテンプレートにすると、元の見た目も残せます）\n\n書き換えますか？") \
                != QMessageBox.Yes:
            return False
        try:
            rc_design.save_template(self.cfg, self.tdir, self.tdir, self.faces)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存できませんでした", str(e))
            return False
        self.saved = copy.deepcopy(self.cfg)
        self._update_state()
        self.win.statusBar().showMessage(f"テンプレート {self.tdir.name} を保存しました（前の内容は template.toml.bak）", 8000)
        return True

    def save_as(self) -> bool:
        dest = ask_new_template(self, rc_design.safe_name(self.from_title) if self.is_new else f"{self.tdir.name}_2")
        if not dest:
            return False
        try:
            rc_design.save_template(self.cfg, self.tdir, dest, self.faces)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存できませんでした", str(e))
            return False
        after_save(self.win, dest)
        self.load_template(dest)
        QMessageBox.information(self, "保存しました",
                                f"テンプレート「{dest.name}」を作りました。\n"
                                "「作成」タブの「見た目テンプレート」で選ぶと、書き出しに使われます。")
        return True

    def _ai(self):
        dlg = ProposeDialog(self.win, self.tdir, self.cfg, editor=self)
        keep(self.win, dlg)
        dlg.show()

    def closeEvent(self, e):  # noqa: N802
        if not self._ask_discard():
            e.ignore()
            return
        self.timer.stop()
        e.accept()

    def reject(self):
        """Esc で閉じたときも、保存していない変更を確かめる（QDialog は Esc では closeEvent を通らないため）"""
        if not self._ask_discard():
            return
        self.timer.stop()
        super().reject()


class SectionForm(QWidget):
    """1つの部分（字幕・テロップ・章カード・サムネイル）の項目"""

    def __init__(self, ed: TemplateEditor, path: tuple, spec: list, panel: str):
        super().__init__()
        self.ed, self.path, self.spec, self.panel = ed, path, spec, panel
        self.w: dict[str, QWidget] = {}
        self.rows: dict[str, tuple[QLabel, QWidget]] = {}
        self.form = QFormLayout(self)
        self.form.setLabelAlignment(Qt.AlignRight)
        for key, label, kind, extra in spec:
            wd = self._make(key, label, kind, extra)
            lab = QLabel(label)
            self.form.addRow(lab, wd)
            self.rows[key] = (lab, wd)
        if panel == "thumb":
            n = QLabel("サムネイルは、書き出しのあとに自動で作られる案（「サムネイル」画面）に使われます。"
                       "見せ方を変えると、色は見せ方の色に戻ります。帯・線の色が「自動」なら、写真から選びます。"
                       "「デザインもAIに任せる」なら、AIが案ごとに強調する言葉・バッジ（「限定」など）・"
                       "写真の効果（主役の切り抜き・背景のぼかし）・縦書きなどの組み合わせを決めます。"
                       "AIの考える深さは動画の編集とは別で、サムネイルだけ深く考えさせられます。")
            n.setWordWrap(True)
            n.setProperty("role", "sub")
            self.form.addRow(n)
        if path[0] == "telop":
            n = QLabel("「位置」が真ん中のときは「画面の端からの距離」は使われません。")
            n.setWordWrap(True)
            n.setProperty("role", "sub")
            self.form.addRow(n)

    def sec(self) -> dict:
        d = self.ed.cfg
        for p in self.path:
            d = d.setdefault(p, {})
        return d

    def _make(self, key, label, kind, extra) -> QWidget:
        if kind in ("font", "thumbfont"):
            w = FontBox(self.ed.families)
            w.changed.connect(lambda v, k=key, t=kind: self._set_font(k, v, t))
        elif kind == "int":
            w = QSpinBox()
            w.setRange(*extra)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, k=key: self._set(k, int(v)))
        elif kind == "float":
            w = QDoubleSpinBox()
            w.setRange(*extra)
            w.setSingleStep(0.5)
            w.setDecimals(1)
            w.setSuffix(" 秒")
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, k=key: self._set(k, round(float(v), 2)))
        elif kind == "bool":
            w = QCheckBox(extra or "太くする")
            w.toggled.connect(lambda v, k=key: self._set(k, bool(v)))
        elif kind == "color":
            w = ColorButton(label)
            w.changed.connect(lambda v, k=key: self._set(k, v))
        elif kind == "choice":
            w = QComboBox()
            for val, text in extra:
                w.addItem(f"{text}（{val}）", val)
            w.currentIndexChanged.connect(lambda _i, k=key, ww=w: self._set(k, ww.currentData()))
        elif kind == "look":
            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(0, 0, 0, 0)
            self.r_outline = QRadioButton("縁取り")
            self.r_box = QRadioButton("帯（文字の後ろに箱）")
            grp = QButtonGroup(w)
            grp.addButton(self.r_outline)
            grp.addButton(self.r_box)
            hl.addWidget(self.r_outline)
            hl.addWidget(self.r_box)
            hl.addStretch()
            self.r_box.toggled.connect(self._set_look)
        else:
            raise ValueError(kind)
        self.w[key] = w
        return w

    def rebuild_fonts(self):
        for key, _l, kind, _e in self.spec:
            if kind in ("font", "thumbfont"):
                old = self.w[key]
                new = self._make(key, _l, kind, _e)
                self.form.replaceWidget(old, new)
                old.deleteLater()
                self.rows[key] = (self.rows[key][0], new)

    def load(self):
        s = self.sec()
        for key, _label, kind, _extra in self.spec:
            w = self.w[key]
            w.blockSignals(True)
            if kind == "font":
                w.set_value(s.get(key, DEFAULTS["font"]))
            elif kind == "thumbfont":
                ff = s.get("font_file", "")
                fam = rc_design.thumbnail_family(self.ed.cfg, self.ed.tdir)
                if fam:
                    w.set_value(fam)
                else:
                    w.set_value(ff or "（自動）", f"（{Path(ff).name or '自動'}: このPCにはありません。自動で選びます）")
            elif kind in ("int", "float"):
                w.setValue(s.get(key, DEFAULTS.get(key, 0)))
            elif kind == "bool":
                w.setChecked(bool(s.get(key, True)))
            elif kind == "color":
                d = DEFAULTS.get(key, "#FFFFFF")
                if key == "box_color" and self.path[0] == "chapter_card":
                    d = "#1A1A1A"
                if self.path[0] == "thumbnail":   # サムネイルは、決めていない色は見せ方の色
                    d = rc_thumb.STYLES.get(s.get("style") or rc_thumb.DEFAULT_STYLE, {}).get(key) or ""
                v = s.get(key) or d
                w.set_value(v)
                if self.path[0] == "thumbnail" and str(v).lower() in ("auto", ""):
                    w.setText("自動（写真から）" if str(v).lower() == "auto" else "なし")
            elif kind == "choice":
                w.setCurrentIndex(max(0, w.findData(s.get(key, DEFAULTS.get(key)))))
            elif kind == "look":
                for r in (self.r_outline, self.r_box):
                    r.blockSignals(True)
                (self.r_box if "box_color" in s else self.r_outline).setChecked(True)
                for r in (self.r_outline, self.r_box):
                    r.blockSignals(False)
            w.blockSignals(False)
        self._enable()

    def _enable(self):
        if "look" not in self.w:
            return
        box = self.r_box.isChecked()
        for k, on in (("outline_color", not box), ("outline", not box), ("box_color", box)):
            lab, w = self.rows[k]
            lab.setEnabled(on)
            w.setEnabled(on)

    def _set(self, key, value):
        self.sec()[key] = value
        if key == "style" and self.path[0] == "thumbnail":
            # 前の見せ方で決めた色が残ると、新しい見せ方に合わないので、見せ方の色に戻す（あとで変えられる）
            for k in ("text_color", "outline_color", "outer_color", "accent_color", "emphasis_color", "badge_color"):
                self.sec().pop(k, None)
            self.load()
        self.ed.changed(self.panel)

    def _set_look(self, box: bool):
        s = self.sec()
        if box:
            s["box_color"] = self.w["box_color"].value
        else:
            s.pop("box_color", None)
        self._enable()
        self.ed.changed(self.panel)

    def _set_font(self, key, value, kind):
        if not value:
            return
        if kind == "thumbfont":
            if not rc_design.set_thumbnail_font(self.ed.cfg, value, self.ed.faces, self.ed.tdir):
                return
        else:
            self.sec()[key] = value
        self.ed.changed(self.panel)


# ============================================================
#  AIにデザイン案を作ってもらう
# ============================================================
class ProposeDialog(QDialog):
    def __init__(self, win, tdir: Path, base_cfg: dict, editor: TemplateEditor | None = None):
        super().__init__(win)
        self.win, self.tdir, self.base_cfg, self.editor = win, Path(tdir).resolve(), copy.deepcopy(base_cfg), editor
        self.setWindowTitle("AIにデザイン案を作ってもらう")
        self.resize(1400, 860)
        self.out = self.tdir / rc_design.WORK_NAME / "proposals"
        self.proposals: list[dict] = []
        self.panel = "emphasis"

        st = win.settings
        self.mood = QLineEdit(st.value("design_mood", ""))
        self.mood.setPlaceholderText("例: 落ち着いた・ガジェット系。工具や家電のレビューが多い。チャンネルカラーは青")
        chips = QHBoxLayout()
        chips.addWidget(QLabel("ことばの例:"))
        for word in MOOD_CHIPS:
            b = QPushButton(word)
            b.setStyleSheet("QPushButton { border: 1px solid #A0AEC0; border-radius: 10px; padding: 2px 10px; }")
            b.clicked.connect(lambda _c=False, t=word: self._add_word(t))
            chips.addWidget(b)
        chips.addStretch()
        llm_label = win.create.llm.currentText()
        info = QLabel(f"元にするテンプレート: <b>{self.tdir.name}</b>"
                      + ("（編集中の内容）" if editor else "")
                      + f"　／　AI: {llm_label}（「作成」タブの設定）<br>"
                      "フォントは、このPCに入っている日本語のフォントの中から選ばれます。")
        self.b_go = QPushButton("3つの案を作る")
        self.b_go.setStyleSheet("QPushButton { background: #2B6CB0; color: white; font-weight: bold; padding: 6px 18px; }"
                                + getattr(_gui(win), "DISABLED_CSS", ""))
        self.b_go.clicked.connect(lambda: self.run())
        row = QHBoxLayout()
        row.addWidget(QLabel("チャンネルの雰囲気"))
        row.addWidget(self.mood, 1)
        row.addWidget(self.b_go)

        # 比べる場所
        self.scene_row = QHBoxLayout()
        self.scene_row.addWidget(QLabel("見比べる所:"))
        self.scene_btns = QButtonGroup(self)
        for key, label in rc_design.PANELS + [("grid", "全部")]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, k=key: self._show(k))
            self.scene_btns.addButton(b)
            self.scene_row.addWidget(b)
            if key == "emphasis":
                b.setChecked(True)
        self.scene_row.addStretch()
        self.cards_box = QWidget()
        self.cards = QHBoxLayout(self.cards_box)
        self.message = QLabel("雰囲気を入れて「3つの案を作る」を押してください。下の「進み具合」に経過が出ます"
                              "（AIの返事まで1〜2分かかることがあります）。")
        self.message.setProperty("role", "sub")
        self.message.setStyleSheet("padding: 30px;")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addLayout(chips)
        lay.addWidget(info)
        lay.addLayout(self.scene_row)
        lay.addWidget(self.message)
        lay.addWidget(self.cards_box, 1)
        self.cards_box.setVisible(False)
        self._load_existing()

    def _add_word(self, w: str):
        t = self.mood.text().strip()
        self.mood.setText(f"{t}・{w}" if t and w not in t else (t or w))

    def _load_existing(self):
        """前に作った案が同じテンプレートにあれば表示しておく"""
        p = self.out / "proposals.json"
        if p.exists():
            try:
                self.load(p)
                self.message.setText("前回作った案です。雰囲気を変えて作り直すこともできます。")
                self.message.setVisible(True)
            except Exception:  # noqa: BLE001
                pass

    def run(self, answer: Path | None = None):
        llm = self.win.create.llm.currentData()
        if not self.win.check_api_key(llm):
            return
        self.win.settings.setValue("design_mood", self.mood.text())
        self.out.mkdir(parents=True, exist_ok=True)
        base = self.out / "base_cfg.json"
        base.write_text(json.dumps(self.base_cfg, ensure_ascii=False), encoding="utf-8")
        fonts = self.out / "fonts.txt"
        tpl = rc_design.MemTemplate(self.base_cfg, self.tdir)
        fams = rc_design.font_families(rc_design.scan_fonts([tpl.fonts_dir] if tpl.fonts_dir else []))
        fonts.write_text("\n".join(f["name"] for f in fams), encoding="utf-8")
        args = ["propose", "--template", str(self.tdir), "--mood", self.mood.text(), "--llm", llm,
                "--base-config", str(base), "--fonts", str(fonts), "--out", str(self.out),
                "--effort", self.win.create.effort.currentData() or "medium"]
        if self.editor:
            src = self.editor.bg_source()
            if src:
                args += ["--bg", src[1]] + (["--at", str(src[2])] if src[0] == "video" else [])
            else:
                args += ["--sample", self.editor.bg_kind()]
        if answer:
            args += ["--answer", str(answer)]

        def done(ok):
            self.b_go.setEnabled(True)
            if not ok:
                return
            if (self.out / "proposals.json").exists():
                self.load(self.out / "proposals.json")
                self.raise_()
                self.activateWindow()
            elif llm == "manual" and (self.out / "prompt_design.md").exists() and answer is None:
                dlg = _gui(self.win).ManualDialog(self, self.out / "prompt_design.md", "デザイン案を作る")
                if dlg.exec():
                    p = self.out / "claude_design.json"
                    p.write_text(dlg.text(), encoding="utf-8")
                    self.run(answer=p)

        if self.win.runner.start("design.py", args, "AIがデザイン案を作成中", done):
            self.b_go.setEnabled(False)
            self.message.setText("作成中です…（下の「進み具合」に経過が出ます）")
            self.message.setVisible(True)

    def load(self, path: Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.proposals = data.get("proposals", [])
        while self.cards.count():
            it = self.cards.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.card_imgs, self.card_overviews = [], []
        for i, p in enumerate(self.proposals):
            self.cards.addWidget(self._card(i, p), 1)
        self.message.setVisible(False)
        self.cards_box.setVisible(True)
        self._show(self.panel)

    def _card(self, i: int, p: dict) -> QWidget:
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        v = QVBoxLayout(box)
        title = QLabel(f"案{i + 1}　{p.get('name', '')}")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        concept = QLabel(p.get("concept", ""))
        concept.setWordWrap(True)
        img = ClickLabel()
        img.setAlignment(Qt.AlignCenter)
        img.setMinimumSize(420, 236)
        img.setStyleSheet("background: #202024;")
        img.setToolTip("クリックすると、全部を並べた画像を開きます")
        img.clicked.connect(lambda: _open(self.win, self.out / p["images"]["grid"]))
        self.card_imgs.append((img, p))
        overview = ClickLabel()
        overview.setAlignment(Qt.AlignCenter)
        overview.setToolTip("全部の見本（クリックで大きく開く）")
        overview.clicked.connect(lambda: _open(self.win, self.out / p["images"]["grid"]))
        self.card_overviews.append((overview, p))
        cfg = p["cfg"]
        fonts = rc_design.used_families(cfg)
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(3)
        for name, c in rc_design.palette(cfg):
            chip = QLabel()
            chip.setFixedSize(22, 22)
            chip.setToolTip(f"{name} {c}")
            chip.setStyleSheet(f"background: {c}; border: 1px solid #888;")
            sl.addWidget(chip)
        sl.addStretch()
        fl = QLabel("フォント: " + "、".join(fonts))
        fl.setWordWrap(True)
        b_save = QPushButton("この案で保存…")
        b_save.setStyleSheet("background: #2B6CB0; color: white; font-weight: bold;")
        b_save.clicked.connect(lambda: self.save(i))
        b_edit = QPushButton("細かく調整する…")
        b_edit.clicked.connect(lambda: self.tune(i))
        br = QHBoxLayout()
        br.addWidget(b_save)
        br.addWidget(b_edit)
        for w in (title, concept, img, sw, fl, overview):
            v.addWidget(w)
        v.addStretch()
        v.addLayout(br)
        return box

    def _show(self, key: str):
        self.panel = key
        for img, p in getattr(self, "card_imgs", []):
            f = self.out / p["images"].get(key, p["images"]["grid"])
            pm = QPixmap(str(f))
            w = max(img.width(), 420)
            img.setPixmap(pm.scaled(w, int(w * pm.height() / max(pm.width(), 1)), Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation))
        for ov, p in getattr(self, "card_overviews", []):
            pm = QPixmap(str(self.out / p["images"]["grid"]))
            w = max(ov.width(), 400)
            ov.setPixmap(pm.scaledToWidth(w, Qt.SmoothTransformation))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        QTimer.singleShot(0, lambda: self._show(self.panel))

    def save(self, i: int):
        p = self.proposals[i]
        dest = ask_new_template(self, rc_design.safe_name(p.get("name", "")))
        if not dest:
            return
        tpl = rc_design.MemTemplate(p["cfg"], self.tdir)
        faces = rc_design.scan_fonts([tpl.fonts_dir] if tpl.fonts_dir else [])
        try:
            rc_design.save_template(p["cfg"], self.tdir, dest, faces)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存できませんでした", str(e))
            return
        after_save(self.win, dest)
        QMessageBox.information(self, "保存しました",
                                f"「{p.get('name', '')}」をテンプレート「{dest.name}」として保存しました。\n"
                                "「作成」タブの「見た目テンプレート」で選ばれた状態になっています。")

    def tune(self, i: int):
        p = self.proposals[i]
        ed = TemplateEditor(self.win, self.tdir, p["cfg"], title=p.get("name", ""))
        keep(self.win, ed)
        ed.show()


# ============================================================
#  共通の処理
# ============================================================
def ask_new_template(parent, default: str) -> Path | None:
    """新しいテンプレートの名前を聞いて、保存先のフォルダを返す（やめたら None）"""
    name = default
    while True:
        name, ok = QInputDialog.getText(parent, "別名で保存",
                                        "新しいテンプレートの名前（templates フォルダの中に、この名前のフォルダを作ります）",
                                        QLineEdit.Normal, name)
        if not ok:
            return None
        name = name.strip()
        err = rc_design.check_name(name)
        if err:
            QMessageBox.warning(parent, "その名前は使えません", err)
            continue
        dest = rc_design.TEMPLATES_DIR / name
        if dest.exists() and QMessageBox.question(
                parent, "同じ名前があります",
                f"テンプレート「{name}」はもうあります。見た目の設定（template.toml）を上書きしますか？\n"
                "（BGMなどのファイルはそのまま。前の設定は template.toml.bak に残ります）") != QMessageBox.Yes:
            continue
        return dest


def after_save(win, dest: Path):
    """保存したテンプレートを「作成」タブで選んだ状態にする"""
    try:
        win.settings.setValue("template", dest.name)
        win.create.refresh_templates()
    except Exception:  # noqa: BLE001
        pass
    win.statusBar().showMessage(f"テンプレート「{dest.name}」を保存しました", 8000)


def material_candidates(win) -> list[Path]:
    """背景に使える自分の素材（作成タブの素材・確認タブで開いている動画）"""
    out = []
    try:
        out += [p for p in win.create.files.paths() if p.exists()]
    except Exception:  # noqa: BLE001
        pass
    try:
        rv = win.review
        if getattr(rv, "out", None) and (Path(rv.out) / "session.json").exists():
            sess = json.loads((Path(rv.out) / "session.json").read_text(encoding="utf-8"))
            out += [Path(p) for p in sess.get("inputs", [])[:1] if Path(p).exists()]
    except Exception:  # noqa: BLE001
        pass
    return out


def keep(win, w):
    """閉じるまで画面を消さないように覚えておく"""
    lst = win.__dict__.setdefault("_design_windows", [])
    lst[:] = [x for x in lst if x.isVisible()]
    lst.append(w)


def current_template(win) -> Path:
    d = win.create.template.currentData()
    return Path(d) if d else rc_design.TEMPLATES_DIR / "default"


def open_editor(win) -> TemplateEditor:
    ed = TemplateEditor(win, current_template(win))
    keep(win, ed)
    ed.show()
    return ed


def open_propose(win) -> ProposeDialog:
    tdir = current_template(win)
    dlg = ProposeDialog(win, tdir, rc_design.load_cfg(tdir))
    keep(win, dlg)
    dlg.show()
    return dlg


def _template_row(win):
    """「作成」タブの「見た目テンプレート」の行（ボタンを足す場所）"""
    combo = win.create.template
    for form in win.create.findChildren(QFormLayout):
        for r in range(form.rowCount()):
            it = form.itemAt(r, QFormLayout.FieldRole)
            lay = it.layout() if it else None
            if lay is not None and lay.indexOf(combo) >= 0:
                return lay
    return None


def register(win):
    b_edit = QPushButton("編集…")
    b_edit.setToolTip("色・フォント・大きさ・位置を画面で変えて、見本ですぐ確かめます")
    b_edit.clicked.connect(lambda: open_editor(win))
    b_ai = QPushButton("AIでデザイン案…")
    b_ai.setToolTip("チャンネルの雰囲気から、AIが配色・フォント・テロップの組み合わせを3つ作ります")
    b_ai.clicked.connect(lambda: open_propose(win))
    row = _template_row(win)
    if row is not None:
        i = row.indexOf(win.create.template)
        row.insertWidget(i + 2, b_edit)
        row.insertWidget(i + 3, b_ai)
    else:
        win.review.ext_bar.insertWidget(0, b_ai)
        win.review.ext_bar.insertWidget(0, b_edit)
    win.design_buttons = (b_edit, b_ai)
    if hasattr(win, "lock_while_busy"):
        win.lock_while_busy(b_ai)
