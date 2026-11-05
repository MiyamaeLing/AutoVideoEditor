"""GUIの拡張：「MV」タブ（曲のビートに合わせて、映像素材をつないだMVのラフを作る）

中身の処理は mv.py を呼び出しています。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

import rc_mv

SONG_FILTER = "曲（音声）ファイル (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.MP3 *.WAV *.M4A);;すべて (*.*)"
ORDERS = [
    ("auto", "おまかせ（動き・明るさで自動）", "動きの量・明るさで自動で選びます。盛り上がる所ほど動きのある絵を使います。"),
    ("ai", "AIが選ぶ（Claude）", "Claudeが素材の代表フレームを見て、曲の雰囲気・場面に合う順番を考えます（API料金がかかります）。"),
    ("files", "撮った順", "撮影した順番（分からなければファイル名の順）に、曲の頭から最後まで並べます。"),
]
PACES = [("normal", "ふつう"), ("fast", "細かめ（速い曲・激しい曲）"), ("slow", "ゆったり（バラード）")]
SIZES = [("1920x1080", "横長 1920x1080"), ("1280x720", "横長 1280x720（早く作れる）"),
         ("1080x1920", "縦長 1080x1920（ショート）"), ("1080x1080", "正方形 1080x1080")]
PARTS = [("all", "曲全体"), ("sabi", "サビだけ（一番盛り上がる所）")]
SECTION_COLOR = {"chorus": "#F6AD55", "intro": "#CBD5E0", "outro": "#CBD5E0", "inter": "#E2E8F0",
                 "verse": "#90CDF4", "pre": "#B794F4"}
CLIP_COLORS = ["#2B6CB0", "#C05621", "#2F855A", "#B83280", "#6B46C1", "#B7791F", "#2C7A7B", "#9B2C2C"]


def _gui():
    """本体（gui.py）のモジュール。python gui.py で起動したときは __main__ になっている"""
    return sys.modules.get("gui") or sys.modules["__main__"]


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ============================================================
#  曲の形と、カットの位置を描く
# ============================================================
class SongGraph(QWidget):
    def __init__(self):
        super().__init__()
        self.an: dict | None = None
        self.plan: dict | None = None
        self.setMinimumHeight(170)
        self.setMouseTracking(True)

    def set_data(self, an: dict | None, plan: dict | None = None):
        self.an, self.plan = an, plan
        self.update()

    def tip_at(self, x: float) -> str:
        """マウスの位置の説明（区間・カットの素材）"""
        if not self.an:
            return ""
        r = self.rect().adjusted(10, 0, -10, 0)
        t = (x - r.left()) / max(r.width(), 1) * self.an["duration"]
        lines = [rc_mv._fmt(t)]
        sec = next((s for s in self.an["sections"] if s["start"] <= t < s["end"]), None)
        if sec:
            lines[0] += f"  {sec['label']}（盛り上がり {int(sec['energy'] * 100)}%）"
        if self.plan and self.plan.get("cuts"):
            a = self.plan["range"][0]
            c = next((c for c in self.plan["cuts"] if a + c["t0"] <= t < a + c["t1"]), None)
            if c:
                lines.append(f"{Path(self.plan['files'][c['file']]).name} の {rc_mv._fmt(c['src_in'])}〜"
                             f"（{c['beats']}拍）")
                if c.get("why"):
                    lines.append(c["why"])
        return "\n".join(lines)

    def mouseMoveEvent(self, e):
        from PySide6.QtWidgets import QToolTip
        text = self.tip_at(e.position().x())
        if text:
            QToolTip.showText(e.globalPosition().toPoint(), text, self)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(6, 4, -6, -4)
        p.fillRect(self.rect(), QColor("#FFFFFF"))
        p.setPen(QColor("#E2E8F0"))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        if not self.an:
            p.setPen(QColor("#888"))
            p.drawText(r, Qt.AlignCenter, "曲を選んで「曲を解析」を押すと、テンポとサビの位置がここに出ます")
            return
        an = self.an
        dur = max(an["duration"], 0.1)
        x0, w = r.left() + 4, r.width() - 8

        def X(t):
            return x0 + w * min(max(t, 0), dur) / dur
        sec_h, cut_h = 24, 16
        top = r.top() + 2
        f = QFont(self.font())
        f.setPointSize(9)
        p.setFont(f)
        # 区間（イントロ・Aメロ・サビ…）
        for s in an["sections"]:
            rect = QRectF(X(s["start"]), top, X(s["end"]) - X(s["start"]), sec_h)
            p.fillRect(rect, QColor(SECTION_COLOR.get(s["kind"], "#E2E8F0")))
            p.setPen(QColor("#FFFFFF"))
            p.drawLine(rect.topLeft(), rect.bottomLeft())
            p.setPen(QColor("#1A202C"))
            p.drawText(rect.adjusted(3, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft, s["label"])
        # 音量のカーブ
        env = an.get("env") or []
        g_top = top + sec_h + cut_h + 6
        g_bot = r.bottom() - 16
        if env:
            lo = max(min(env), max(env) - 50)
            hi = max(env)
            rate = an.get("env_rate", 10)
            poly = QPolygonF()
            poly.append(QPointF(X(0), g_bot))
            step = max(1, len(env) // max(w, 1))
            for i in range(0, len(env), step):
                v = (max(env[i], lo) - lo) / max(hi - lo, 1e-6)
                poly.append(QPointF(X(i / rate), g_bot - v * (g_bot - g_top)))
            poly.append(QPointF(X(len(env) / rate), g_bot))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(74, 85, 104, 90)))
            p.drawPolygon(poly)
        # 拍（小節の頭は長く）
        down = an.get("downbeats", [])
        di = 0
        for t in an.get("beats", []):
            is_down = di < len(down) and abs(down[di] - t) < 1e-3
            if is_down:
                di += 1
            p.setPen(QPen(QColor("#2D3748" if is_down else "#A0AEC0"), 1))
            p.drawLine(QPointF(X(t), r.bottom() - (12 if is_down else 6)), QPointF(X(t), r.bottom() - 1))
        # 一番の盛り上がり
        bc = an.get("best_chorus")
        if bc:
            p.setPen(QPen(QColor("#DD6B20"), 2))
            path = QPainterPath()
            path.moveTo(X(bc["start"]), g_top - 2)
            path.lineTo(X(bc["end"]), g_top - 2)
            p.drawPath(path)
        # カット（素材ごとに色を変える）
        if self.plan and self.plan.get("cuts"):
            a, b = self.plan["range"]
            ct = top + sec_h + 3
            for c in self.plan["cuts"]:
                col = QColor(CLIP_COLORS[c["file"] % len(CLIP_COLORS)])
                rect = QRectF(X(a + c["t0"]), ct, max(1.0, X(a + c["t1"]) - X(a + c["t0"]) - 1), cut_h)
                p.fillRect(rect, col)
            shade = QColor(255, 255, 255, 170)
            if a > 0.01:
                p.fillRect(QRectF(X(0), top, X(a) - X(0), r.bottom() - top), shade)
            if b < dur - 0.01:
                p.fillRect(QRectF(X(b), top, X(dur) - X(b), r.bottom() - top), shade)
        p.end()


# ============================================================
#  MVタブ
# ============================================================
class MvTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        st = win.settings
        G = _gui()
        self.setAcceptDrops(True)

        # 1. 曲
        self.song = QLineEdit(st.value("mv/song", ""))
        self.song.setPlaceholderText("曲のファイル（mp3・wav など）をドラッグ＆ドロップ、または「曲を選ぶ…」")
        self.song.setReadOnly(True)
        b_song = QPushButton("曲を選ぶ…")
        b_song.clicked.connect(self._pick_song)
        g_song = QGroupBox("1. 曲")
        ls = QHBoxLayout(g_song)
        ls.addWidget(self.song, 1)
        ls.addWidget(b_song)

        # 2. 素材
        self.files = MaterialList(G.FileList, self)
        fb = QHBoxLayout()
        for text, fn in (("動画を追加…", self._pick_files), ("フォルダを追加…", self._pick_folder),
                         ("選択を削除", self.files.remove_selected), ("全部消す", self.files.clear)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            fb.addWidget(b)
        fb.addStretch()
        g_mat = QGroupBox("2. 映像素材")
        lm = QVBoxLayout(g_mat)
        lm.addWidget(self.files)
        lm.addLayout(fb)

        # 3. 作り方
        g_opt = QGroupBox("3. 作り方")
        form = QFormLayout(g_opt)
        self.order_group = QButtonGroup(self)
        orow = QHBoxLayout()
        self.order_desc = QLabel()
        self.order_desc.setWordWrap(True)
        self.order_desc.setProperty("role", "sub")
        for i, (key, label, desc) in enumerate(ORDERS):
            rb = QRadioButton(label)
            rb.setProperty("key", key)
            self.order_group.addButton(rb, i)
            orow.addWidget(rb)
            rb.toggled.connect(lambda on, d=desc: on and self.order_desc.setText(d))
        orow.addStretch()
        keys = [o[0] for o in ORDERS]
        self.order_group.button(keys.index(st.value("mv/order", "auto")) if st.value("mv/order", "auto") in keys
                                else 0).setChecked(True)
        ob = QVBoxLayout()
        ob.addLayout(orow)
        ob.addWidget(self.order_desc)
        form.addRow("素材の選び方", ob)
        self.note = QLineEdit(st.value("mv/note", ""))
        self.note.setPlaceholderText("例: 夏の終わり・青春・疾走感のあるロック（「AIが選ぶ」のときに使います）")
        form.addRow("曲の雰囲気", self.note)
        self.pace = self._combo(PACES, st.value("mv/pace", "normal"))
        self.size = self._combo(SIZES, st.value("mv/size", "1920x1080"))
        self.part = self._combo(PARTS, st.value("mv/part", "all"))
        self.bpm = QSpinBox()
        self.bpm.setRange(0, 300)
        self.bpm.setSpecialValueText("自動")
        self.bpm.setSuffix(" BPM")
        self.bpm.setValue(int(st.value("mv/bpm", 0) or 0))
        row = QHBoxLayout()
        for lab, wdg in (("カットの速さ", self.pace), ("画面", self.size), ("使う範囲", self.part), ("テンポ", self.bpm)):
            row.addWidget(QLabel(lab))
            row.addWidget(wdg)
        row.addStretch()
        form.addRow(row)

        # 実行
        self.b_analyze = QPushButton("曲を解析")
        self.b_analyze.setToolTip("テンポ・拍・サビの位置を調べます（すぐ終わります）")
        self.b_preview = QPushButton("試しに20秒")
        self.b_preview.setToolTip("最初の20秒だけ書き出して、雰囲気を確かめます")
        self.b_make = QPushButton("MVラフを作る")
        self.b_make.setToolTip("ビートに合わせてカットをつなぎ、mp4 と Premiere用XML を作ります")
        for b in (self.b_analyze, self.b_preview, self.b_make):
            b.setMinimumHeight(40)
        self.b_make.setStyleSheet("QPushButton { font-size: 15px; font-weight: bold; background: #2B6CB0; color: white; }"
                                  + G.DISABLED_CSS)
        self.b_analyze.clicked.connect(self.analyze)
        self.b_preview.clicked.connect(lambda: self.make(preview=True))
        self.b_make.clicked.connect(lambda: self.make(preview=False))
        br = QHBoxLayout()
        br.addWidget(self.b_analyze)
        br.addWidget(self.b_preview)
        br.addWidget(self.b_make, 2)

        # 結果
        self.graph = SongGraph()
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #2D3748;")
        self.b_play = QPushButton("MVラフを再生")
        self.b_play.clicked.connect(self._play)
        self.b_xml = QPushButton("Premiere用XMLを開く")
        self.b_xml.clicked.connect(self._xml)
        self.b_folder = QPushButton("フォルダを開く")
        self.b_folder.clicked.connect(lambda: self.out_dir() and G.open_path(self.out_dir()))
        rr = QHBoxLayout()
        rr.addWidget(self.info, 1)
        for b in (self.b_play, self.b_xml, self.b_folder):
            rr.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addWidget(g_song)
        lay.addWidget(g_mat, 2)
        lay.addWidget(g_opt)
        lay.addLayout(br)
        lay.addWidget(self.graph, 2)
        lay.addLayout(rr)

        win.runner.busy_changed.connect(self._update_buttons)
        self.refresh()

    # --- 補助 ---
    def _combo(self, items, value) -> QComboBox:
        cb = QComboBox()
        for k, v in items:
            cb.addItem(v, k)
        cb.setCurrentIndex(max(0, cb.findData(value)))
        return cb

    def order(self) -> str:
        return self.order_group.checkedButton().property("key")

    def song_path(self) -> Path | None:
        s = self.song.text().strip()
        return Path(s) if s else None

    def out_dir(self) -> Path | None:
        s = self.song_path()
        return s.parent / f"{s.stem}_mv" if s else None

    def set_song(self, path) -> None:
        self.song.setText(str(path))
        self.win.settings.setValue("mv/song", str(path))
        self.refresh()

    def _pick_song(self):
        f, _ = QFileDialog.getOpenFileName(self, "曲を選ぶ", self.win.settings.value("last_dir", ""), SONG_FILTER)
        if f:
            self.set_song(f)

    def _pick_files(self):
        G = _gui()
        fs, _ = QFileDialog.getOpenFileNames(self, "映像素材を選ぶ", self.win.settings.value("last_dir", ""), G.VIDEO_EXT)
        if fs:
            self.files.add(fs)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "映像素材のフォルダを選ぶ", self.win.settings.value("last_dir", ""))
        if d:
            self.files.add([d])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.files.add(u.toLocalFile() for u in e.mimeData().urls())
        e.acceptProposedAction()

    def save_settings(self):
        st = self.win.settings
        st.setValue("mv/order", self.order())
        st.setValue("mv/note", self.note.text())
        st.setValue("mv/pace", self.pace.currentData())
        st.setValue("mv/size", self.size.currentData())
        st.setValue("mv/part", self.part.currentData())
        st.setValue("mv/bpm", self.bpm.value())

    def _template(self) -> str:
        try:
            return str(self.win.create.template.currentData())
        except Exception:  # noqa: BLE001
            return str(Path(rc_mv.__file__).parent / "templates" / "default")

    # --- 実行 ---
    def _base_args(self) -> list[str] | None:
        s = self.song_path()
        if not s or not s.exists():
            QMessageBox.information(self, "曲がありません", "曲のファイル（mp3・wav など）を選んでください。")
            return None
        self.save_settings()
        args = [str(s), "--out", str(self.out_dir())]
        if self.bpm.value():
            args += ["--bpm", str(self.bpm.value())]
        return args

    def analyze(self):
        args = self._base_args()
        if args is None:
            return
        self.win.runner.start("mv.py", args + ["--analyze-only"], "曲を解析中", lambda ok: self.refresh())

    def make(self, preview: bool = False):
        args = self._base_args()
        if args is None:
            return
        if not self.files.count():
            QMessageBox.information(self, "素材がありません", "映像素材の動画（またはフォルダ）をドラッグ＆ドロップしてください。")
            return
        if self.order() == "ai" and not self.win.check_api_key(
                "api", "APIキーなしで作るときは、素材の選び方を「おまかせ（動き・明るさで自動）」にしてください。"):
            return
        args = [args[0], *map(str, self.files.paths()), *args[1:],
                "--order", self.order(), "--pace", self.pace.currentData(), "--size", self.size.currentData(),
                "--part", self.part.currentData(), "--template", self._template()]
        if self.note.text().strip():
            args += ["--note", self.note.text().strip()]
        try:
            args += ["--effort", self.win.create.effort.currentData()]
        except Exception:  # noqa: BLE001
            pass
        if preview:
            args += ["--preview", "20"]
        self.win.runner.start("mv.py", args, "MVラフを作成中", self._done)

    def _done(self, ok: bool):
        self.refresh()
        if ok:
            v = self._video()
            if v:
                self.info.setText(self.info.text() + f"\n✔ {v.name} ができました")

    # --- 結果の表示 ---
    def _plan(self) -> dict | None:
        o = self.out_dir()
        return read_json(o / "mv_plan.json") if o else None

    def _video(self) -> Path | None:
        p = self._plan()
        v = Path(p["video"]) if p and p.get("video") else None
        return v if v and v.exists() else None

    def refresh(self):
        o = self.out_dir()
        an = read_json(o / "mv_song.json") if o else None
        if an and self.song_path() and Path(an.get("song", "")).name != self.song_path().name:
            an = None
        plan = self._plan() if an else None
        self.graph.set_data(an, plan)
        if an:
            n_ch = sum(1 for s in an["sections"] if s["kind"] == "chorus")
            bc = an["best_chorus"]
            text = (f"テンポ {an['bpm']:.0f} BPM・{an['beats_per_bar']}拍子・長さ {rc_mv._fmt(an['duration'])}　"
                    f"サビ {n_ch}か所（一番の盛り上がり {rc_mv._fmt(bc['start'])}〜{rc_mv._fmt(bc['end'])}）")
            if plan and plan.get("cuts"):
                cuts = plan["cuts"]
                text += f"\nカット {len(cuts)}個・素材 {len({c['file'] for c in cuts})}本を使用"
                if plan.get("concept"):
                    text += f"\nAIの考え: {plan['concept']}"
            self.info.setText(text)
        else:
            self.info.setText("")
        self._update_buttons()

    def _update_buttons(self, busy: bool | None = None):
        busy = self.win.runner.busy if busy is None else busy
        for b in (self.b_analyze, self.b_preview, self.b_make):
            b.setEnabled(not busy)
        self.b_play.setEnabled(self._video() is not None)
        p = self._plan()
        self.b_xml.setEnabled(bool(p and Path(p.get("xml", "")).exists()))
        self.b_folder.setEnabled(bool(self.out_dir() and self.out_dir().exists()))

    def _play(self):
        v = self._video()
        if v:
            _gui().open_path(v)

    def _xml(self):
        p = self._plan()
        if p and Path(p["xml"]).exists():
            QMessageBox.information(self, "Premiereで開く",
                                    "Premiere Pro の「ファイル → 読み込み」で次のファイルを選んでください。\n"
                                    "拍の位置にマーカーが入り、素材は並べた状態で開きます。\n\n"
                                    f"{p['xml']}\n\n（このあとフォルダを開きます）")
            _gui().open_path(Path(p["xml"]).parent)


def MaterialList(base, tab: MvTab):
    """素材の一覧（動画ファイルとフォルダを受け付ける。曲のファイルを落としたら「曲」の欄へ）"""

    class _List(base):
        def __init__(self):
            super().__init__("ここに映像素材の動画、または動画の入ったフォルダをドラッグ＆ドロップ\n"
                             "（曲のファイルを落とすと「曲」の欄に入ります）")

        def add(self, paths):
            have = {str(p) for p in self.paths()}
            for p in sorted(Path(x) for x in paths):
                if p.is_file() and p.suffix.lower() in rc_mv.AUDIO_EXT:
                    tab.set_song(p)
                    continue
                if str(p) in have or not (p.is_dir() or (p.is_file() and p.suffix.lower() in rc_mv.VIDEO_EXT)):
                    continue
                self.addItem(f"[フォルダ] {p.name}" if p.is_dir() else p.name)
                it = self.item(self.count() - 1)
                it.setData(Qt.UserRole, str(p))
                it.setToolTip(str(p))
                tab.win.settings.setValue("last_dir", str(p.parent))
    return _List()


def register(win):
    win.mv_tab = MvTab(win)
    win.tabs.addTab(win.mv_tab, "MV")
