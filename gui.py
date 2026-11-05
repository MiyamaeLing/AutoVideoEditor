"""AI動画編集ツールの画面（GUI）。

起動: start_gui.bat をダブルクリック（または python gui.py）
中身の処理は roughcut.py / finish.py をそのまま呼び出しています。
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_MEDIA = True
except Exception:  # 環境によっては動画再生の部品が使えない
    HAS_MEDIA = False

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import rc_win  # noqa: E402,F401  (FFmpeg実行時に黒い画面を出さない)
from rc_transcribe import is_filler_only  # noqa: E402
VIDEO_EXT = "動画ファイル (*.mp4 *.MP4 *.mov *.MOV *.mts *.MTS *.m2ts *.mkv *.avi);;すべて (*.*)"
MODES = [
    ("talk", "レビュー・解説", "喋っていない所をどんどん詰めます。言い直しやNGはAIがカットします。"),
    ("craft", "工作", "喋りの間は詰めますが、無言の作業シーンは残して「早送り候補」の印を付けます。"),
    ("outdoor", "屋外企画", "自動ではカットしません。NG候補や見どころに印を付けるだけです（確認画面でカットできます）。"),
]
LLMS = [("api", "Claude APIで自動"), ("manual", "claude.ai に貼り付けて使う（API料金なし）"), ("none", "AIを使わない")]
TYPE_JA = {"retake": "言い直し", "mistake": "言い間違い", "filler": "フィラー", "offscript": "段取り・独り言",
           "other": "その他"}
STYLE_JA = {"emphasis": "強調", "info": "補足", "warning": "注意", "funny": "ツッコミ"}
SE_JA = {"none": "なし", "pop": "ポンッ", "ding": "チーン", "whoosh": "シュッ", "boing": "ボヨン"}
MOOD_JA = {"calm": "落ち着いた", "upbeat": "明るい", "tension": "緊張感", "emotional": "しんみり", "none": "BGMなし"}

# 色を付けたボタンも、押せないときは灰色にして分かるようにする
DISABLED_CSS = " QPushButton:disabled { background: #CBD5E0; color: #F7FAFC; }"

C_CUT = QColor("#F3D6D6")
C_REVIEW = QColor("#FFF3C4")
C_HILITE = QColor("#D8F0DA")


def fmt(t: float) -> str:
    t = max(0, t)
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def open_path(p: Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def has_api_key(settings: QSettings) -> bool:
    return bool(settings.value("api_key", "") or os.environ.get("ANTHROPIC_API_KEY"))


# コマンド向けの案内を、画面での操作に言い換える（エラーのダイアログに添える）
GUI_HINTS = [
    ("ANTHROPIC_API_KEY", "「作成」タブの「APIキーを設定…」でキーを入れるか、AIの欄で「claude.ai に貼り付けて使う」"
                          "または「AIを使わない」を選んでから、もう一度押してください。"),
    ("pip install", "必要なライブラリが入っていません。setup.bat をもう一度実行してください。"),
    ("--video-id", "「YouTubeへ」画面の「動画ID」欄に YouTube Studio の動画IDを入れるか、"
                   "表示される一覧から動画を選んでください。"),
    ("Googleアカウントとの連携が必要", "「YouTubeへ」画面で「Googleアカウントと連携」を押してください。"),
    ("client_secret", "「YouTubeへ」画面の「連携の準備（client_secret.json）…」で、"
                      "Google Cloud からダウンロードしたファイルを選んでください。"),
    ("--llm manual", "AIの欄で「claude.ai に貼り付けて使う」を選ぶと、APIを使わずに進められます。"),
    ("h264_nvenc", "テンプレートの encoder を \"libx264\" にすると、GPUを使わずに書き出せます。"),
    ("作業フォルダを消せませんでした", "少し待ってから、もう一度押してください。"),
    ("out of memory", "「作成」タブの詳細設定で「文字起こし」を「CPU」にするか、ほかのアプリを閉じてください。"),
]


def gui_hint(text: str) -> str:
    keys = [key for key, _ in GUI_HINTS if key in text]
    if "ANTHROPIC_API_KEY" in keys and "--llm manual" in keys:
        keys.remove("--llm manual")   # 同じことを2回言わない
    return "\n".join(h for key, h in GUI_HINTS if key in keys)


# ============================================================
#  処理の実行（roughcut.py / finish.py を別プロセスで動かす）
# ============================================================
class Runner(QWidget):
    """下部の「進み具合」パネル。コマンドを順番に実行して、ログと進捗を表示する。"""

    busy_changed = Signal(bool)

    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.proc: QProcess | None = None
        self.on_done = None
        self.handle_error = None
        self._buf = ""
        self._dec = None
        self._stopped = False
        self._err: list[str] = []      # 最後の「[エラー]」の内容
        self._err_open = False
        self._tb = ""                  # Traceback の最後の行
        self._in_tb = False
        self.last_error = ""

        self.stage = QLabel("待機中")
        self.stage.setStyleSheet("font-weight: bold;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.stop_btn = QPushButton("中止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.toggle = QToolButton()
        self.toggle.setText("ログを表示")
        self.toggle.setCheckable(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setVisible(False)
        self.toggle.toggled.connect(lambda on: (self.log.setVisible(on),
                                                self.toggle.setText("ログを隠す" if on else "ログを表示")))
        top = QHBoxLayout()
        top.addWidget(self.stage, 1)
        top.addWidget(self.toggle)
        top.addWidget(self.stop_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 8)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        lay.addWidget(self.log)

    @property
    def busy(self) -> bool:
        return self.proc is not None

    def env(self) -> QProcessEnvironment:
        e = QProcessEnvironment.systemEnvironment()
        e.insert("PYTHONIOENCODING", "utf-8")
        e.insert("PYTHONUNBUFFERED", "1")
        key = self.settings.value("api_key", "")
        if key:
            e.insert("ANTHROPIC_API_KEY", key)
        return e

    def start(self, script: str, args: list[str], title: str, on_done=None, handle_error=None) -> bool:
        """script を実行する。handle_error(エラー文) が True を返したら、エラーのダイアログは出さない"""
        if self.busy:
            QMessageBox.information(self, "実行中", "ほかの処理が実行中です。終わるまでお待ちください。")
            return False
        import codecs
        self.on_done = on_done
        self.handle_error = handle_error
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")  # 文字の途中で区切られても化けない
        self._stopped = False
        self._err, self._err_open, self._tb, self._in_tb, self.last_error = [], False, "", False, ""
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setProcessEnvironment(self.env())
        self.proc.setWorkingDirectory(str(APP_DIR))
        if os.name != "nt" and hasattr(QProcess, "setUnixProcessParameters"):
            try:   # 中止のときに、FFmpeg などの孫プロセスもまとめて止められるようにする
                self.proc.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
            except Exception:  # noqa: BLE001
                pass
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.stage.setText(title)
        self.bar.setRange(0, 0)
        self.stop_btn.setEnabled(True)
        self.log.appendPlainText(f"\n===== {title} =====")
        self.proc.start(sys.executable, ["-u", str(APP_DIR / script), *args])
        self.busy_changed.emit(True)
        return True

    def stop(self, wait: bool = False):
        """実行中の処理を、FFmpeg などの子プロセスも含めて止める"""
        if not self.proc:
            return
        self._stopped = True
        pid = int(self.proc.processId() or 0)
        if pid:
            try:
                if os.name == "nt":
                    import subprocess
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
                else:
                    import signal
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        if self.proc:
            self.proc.kill()
            if wait:
                self.proc.waitForFinished(5000)
        self.log.appendPlainText("[中止しました]")

    def _read(self):
        data = self._dec.decode(bytes(self.proc.readAllStandardOutput()))
        self._buf += data.replace("\r", "\n")
        *lines, self._buf = self._buf.split("\n")
        for ln in lines:
            if ln.strip():
                self.log.appendPlainText(ln)
                self._progress(ln)

    def _progress(self, ln: str):
        s = ln.strip()
        if s.startswith("■"):
            self.stage.setText(s.lstrip("■ "))
            self.bar.setRange(0, 0)
        elif m := re.search(r"\((\d+)%\)\s*$", s):           # 文字起こし
            self._set(int(m.group(1)))
        elif m := re.fullmatch(r"(\d+)%", s):                 # 最終書き出し
            self.stage.setText("最終書き出し（字幕・テロップの焼き込み）")
            self._set(int(m.group(1)))
        elif m := re.search(r"本編の書き出し (\d+)/(\d+)", s):
            self.stage.setText(f"本編の書き出し {m.group(1)}/{m.group(2)}")
            self._set(int(int(m.group(1)) / int(m.group(2)) * 100))
        elif s.startswith(("Claudeで", "文字起こし中", "音声のミックス", "最終書き出し")):
            self.stage.setText(s)
            self.bar.setRange(0, 0)
        if s.startswith("[エラー]") or s.startswith("Traceback"):
            self.toggle.setChecked(True)
        self._note_error(ln)

    def _note_error(self, ln: str):
        """ログの中の「[エラー]」とその続きの数行、または Traceback の最後の行を覚えておく"""
        s = ln.strip()
        if s.startswith("[エラー]"):
            self._err, self._err_open = [s], True
            return
        if s.startswith("Traceback"):
            self._in_tb, self._err_open = True, False
            return
        if self._in_tb:
            if not ln.startswith((" ", "\t")):
                self._tb, self._in_tb = s, False
            return
        if self._err_open:
            if s.startswith(("■", "[注意]", "[お知らせ]")) or len(self._err) >= 8:
                self._err_open = False
            else:
                self._err.append(s)

    def _set(self, v: int):
        self.bar.setRange(0, 100)
        self.bar.setValue(min(v, 100))

    def _finished(self, code: int, _status):
        if self._dec:
            self._buf += self._dec.decode(b"", final=True)
        if self._buf.strip():
            for ln in self._buf.split("\n"):
                if ln.strip():
                    self.log.appendPlainText(ln)
                    self._note_error(ln)
            self._buf = ""
        self.proc = None
        self.stop_btn.setEnabled(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(100 if code == 0 else 0)
        stopped, self._stopped = self._stopped, False
        if stopped:
            self.stage.setText("中止しました")
        elif code != 0:
            err = "\n".join(self._err) if self._err else self._tb
            self.last_error = err
            first = err.splitlines()[0].replace("[エラー]", "").strip() if err else ""
            self.stage.setText(f"エラーで止まりました：{first}" if first else "エラーで止まりました（ログを確認してください）")
            self.toggle.setChecked(True)
            handler, self.handle_error = self.handle_error, None
            handled = False
            if handler:
                try:
                    handled = bool(handler(err))
                except Exception:  # noqa: BLE001
                    handled = False
            body = "処理がエラーで止まりました。"
            if err:
                body += "\n\n" + (err if len(err) < 1200 else err[:1200] + "…")
            hint = gui_hint(err)
            if hint:
                body += "\n\n【どうすればいい？】\n" + hint
            body += "\n\n詳しくは下のログを確認してください。"
            if not handled:
                QMessageBox.warning(self, "エラー", body)
        else:
            self.stage.setText("完了")
        self.busy_changed.emit(False)
        cb, self.on_done = self.on_done, None
        if cb:
            cb(code == 0 and not stopped)


# ============================================================
#  小さな部品
# ============================================================
class FileList(QListWidget):
    """動画ファイルをドラッグ＆ドロップで受け付けるリスト"""

    def __init__(self, placeholder: str):
        super().__init__()
        self.placeholder = placeholder
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumHeight(70)

    def paths(self) -> list[Path]:
        return [Path(self.item(i).data(Qt.UserRole)) for i in range(self.count())]

    def add(self, paths):
        have = {str(p) for p in self.paths()}
        for p in sorted(Path(x) for x in paths):
            if p.is_file() and str(p) not in have:
                self.addItem(p.name)
                self.item(self.count() - 1).setData(Qt.UserRole, str(p))
                self.item(self.count() - 1).setToolTip(str(p))

    def remove_selected(self):
        for it in self.selectedItems():
            self.takeItem(self.row(it))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            self.add(u.toLocalFile() for u in e.mimeData().urls())
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            from PySide6.QtGui import QPainter
            p = QPainter(self.viewport())
            p.setPen(QColor("#888"))
            p.drawText(self.viewport().rect(), Qt.AlignCenter, self.placeholder)


class ManualDialog(QDialog):
    """claude.ai に貼り付けて使う場合の、コピー＆貼り付け画面"""

    def __init__(self, parent, prompt_path: Path, what: str, can_skip: bool = False):
        super().__init__(parent)
        self.setWindowTitle(f"claude.ai で{what}")
        self.resize(720, 560)
        self.skipped = False
        self.prompt = prompt_path.read_text(encoding="utf-8")
        steps = QLabel(
            f"<b>手順</b><br>"
            f"① 「プロンプトをコピー」を押す<br>"
            f"② <a href='https://claude.ai/new'>claude.ai</a> を開いて貼り付け、送信する<br>"
            f"③ Claudeの返答（JSON）を全部コピーして、下の欄に貼り付ける<br>"
            f"④ 「この内容で続ける」を押す")
        steps.setOpenExternalLinks(True)
        copy = QPushButton("① プロンプトをコピー")
        copy.clicked.connect(self._copy)
        self.copied = QLabel("")
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("ここに Claude の返答を貼り付け（```json などが付いたままで大丈夫です）")
        bb = QDialogButtonBox()
        ok = bb.addButton("この内容で続ける", QDialogButtonBox.AcceptRole)
        if can_skip:
            sk = bb.addButton("この手順は飛ばす", QDialogButtonBox.DestructiveRole)
            sk.clicked.connect(self._skip)
        bb.addButton("やめる", QDialogButtonBox.RejectRole)
        ok.clicked.connect(self._ok)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(copy)
        row.addWidget(self.copied, 1)
        lay = QVBoxLayout(self)
        lay.addWidget(steps)
        lay.addLayout(row)
        lay.addWidget(self.answer, 1)
        lay.addWidget(bb)

    def _copy(self):
        QGuiApplication.clipboard().setText(self.prompt)
        self.copied.setText(f"コピーしました（{len(self.prompt):,}文字）")

    def _skip(self):
        self.skipped = True
        self.accept()

    def _ok(self):
        t = self.answer.toPlainText()
        a, b = t.find("{"), t.rfind("}")
        try:
            json.loads(t[a:b + 1])
        except Exception:
            QMessageBox.warning(self, "読み取れません", "JSONが読み取れませんでした。返答を最初から最後まで全部コピーしてください。")
            return
        self.accept()

    def text(self) -> str:
        t = self.answer.toPlainText()
        return t[t.find("{"): t.rfind("}") + 1]


# ============================================================
#  作成タブ
# ============================================================
class CreatePage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        st = win.settings

        # 素材
        self.files = FileList("ここに素材の動画をドラッグ＆ドロップ\n（カメラが分割したファイルは全部入れてください）")
        self.cam2 = FileList("2台目カメラ（スマホなど）があればここへ（任意）")
        self.cam2.setMaximumHeight(80)
        fbtn = QHBoxLayout()
        for text, fn in (("追加…", lambda: self._pick(self.files)), ("選択を削除", self.files.remove_selected),
                         ("2台目を追加…", lambda: self._pick(self.cam2)),
                         ("2台目を削除", self.cam2.remove_selected)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            fbtn.addWidget(b)
        fbtn.addStretch()
        g_src = QGroupBox("1. 素材")
        l = QVBoxLayout(g_src)
        l.addWidget(self.files, 2)
        l.addWidget(self.cam2, 1)
        l.addLayout(fbtn)

        # モード
        g_mode = QGroupBox("2. 撮影ジャンル")
        self.mode_group = QButtonGroup(self)
        ml = QHBoxLayout()
        self.mode_desc = QLabel()
        self.mode_desc.setWordWrap(True)
        self.mode_desc.setStyleSheet("color: #555;")
        for i, (key, label, desc) in enumerate(MODES):
            rb = QRadioButton(label)
            rb.setProperty("key", key)
            self.mode_group.addButton(rb, i)
            ml.addWidget(rb)
            rb.toggled.connect(lambda on, d=desc: on and self.mode_desc.setText(d))
        ml.addStretch()
        idx = [m[0] for m in MODES].index(st.value("mode", "talk"))
        self.mode_group.button(idx).setChecked(True)
        lm = QVBoxLayout(g_mode)
        lm.addLayout(ml)
        lm.addWidget(self.mode_desc)

        # 設定
        g_opt = QGroupBox("3. 設定")
        form = QFormLayout(g_opt)
        self.llm = QComboBox()
        for k, v in LLMS:
            self.llm.addItem(v, k)
        self.llm.setCurrentIndex(max(0, [k for k, _ in LLMS].index(st.value("llm", "api"))))
        self.key_label = QLabel()
        key_btn = QPushButton("APIキーを設定…")
        key_btn.clicked.connect(self._set_key)
        row = QHBoxLayout()
        row.addWidget(self.llm, 1)
        row.addWidget(self.key_label)
        row.addWidget(key_btn)
        form.addRow("AI", row)

        self.template = QComboBox()
        tp_btn = QPushButton("見た目を確認")
        tp_btn.clicked.connect(self._template_preview)
        tf_btn = QPushButton("フォルダを開く")
        tf_btn.clicked.connect(lambda: open_path(Path(self.template.currentData())))
        row = QHBoxLayout()
        row.addWidget(self.template, 1)
        row.addWidget(tp_btn)
        row.addWidget(tf_btn)
        form.addRow("見た目テンプレート", row)

        self.glossary = QLineEdit(st.value("glossary", ""))
        self.glossary.setPlaceholderText("例: マキタ、インパクトドライバー、Qpic（「、」区切り。字幕の精度が上がります）")
        form.addRow("固有名詞", self.glossary)

        self.adv = QToolButton()
        self.adv.setText("詳細設定 ▸")
        self.adv.setCheckable(True)
        self.adv.setStyleSheet("border: none; color: #2B6CB0;")
        adv_box = QWidget()
        al = QHBoxLayout(adv_box)
        al.setContentsMargins(0, 0, 0, 0)
        self.effort = QComboBox()
        for k, v in (("low", "速い"), ("medium", "ふつう"), ("high", "じっくり")):
            self.effort.addItem(v, k)
        self.effort.setCurrentIndex(["low", "medium", "high"].index(st.value("effort", "medium")))
        self.cap_chars = QSpinBox()
        self.cap_chars.setRange(10, 60)
        self.cap_chars.setValue(int(st.value("caption_chars", 24)))
        self.device = QComboBox()
        for k, v in (("auto", "自動"), ("cuda", "GPU"), ("cpu", "CPU")):
            self.device.addItem(v, k)
        for w, lab in ((self.effort, "AIの考える深さ"), (self.cap_chars, "字幕1枚の最大文字数"), (self.device, "文字起こし")):
            al.addWidget(QLabel(lab))
            al.addWidget(w)
        al.addStretch()
        adv_box.setVisible(False)
        self.adv.toggled.connect(lambda on: (adv_box.setVisible(on), self.adv.setText("詳細設定 ▾" if on else "詳細設定 ▸")))
        form.addRow(self.adv, adv_box)

        # 実行
        self.btn_rough = QPushButton("荒編集する")
        self.btn_rough.setToolTip("カット・字幕・Premiere用XMLまで作って、確認画面を開きます")
        self.btn_full = QPushButton("完成まで一気に作る")
        self.btn_full.setToolTip("荒編集 → 演出プラン → 完成動画の書き出しまで、止まらずに進めます")
        for b in (self.btn_rough, self.btn_full):
            b.setMinimumHeight(44)
            b.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.btn_full.setStyleSheet("QPushButton { font-size: 15px; font-weight: bold; background: #2B6CB0; color: white; }"
                                    + DISABLED_CSS)
        self.btn_rough.clicked.connect(lambda: self.start(full=False))
        self.btn_full.clicked.connect(lambda: self.start(full=True))
        br = QHBoxLayout()
        br.addWidget(self.btn_rough)
        br.addWidget(self.btn_full)

        lay = QVBoxLayout(self)
        lay.addWidget(g_src, 3)
        lay.addWidget(g_mode)
        lay.addWidget(g_opt)
        lay.addLayout(br)

        self.refresh_templates()
        self.refresh_key()

    # --- 補助 ---
    def _pick(self, lst: FileList):
        fs, _ = QFileDialog.getOpenFileNames(self, "動画を選ぶ", self.win.settings.value("last_dir", ""), VIDEO_EXT)
        if fs:
            self.win.settings.setValue("last_dir", str(Path(fs[0]).parent))
            lst.add(fs)

    def refresh_templates(self):
        cur = self.win.settings.value("template", "default")
        self.template.clear()
        for d in sorted((APP_DIR / "templates").glob("*/template.toml")):
            self.template.addItem(d.parent.name, str(d.parent))
        i = self.template.findText(cur)
        self.template.setCurrentIndex(max(i, 0))

    def refresh_key(self):
        has = bool(self.win.settings.value("api_key", "") or os.environ.get("ANTHROPIC_API_KEY"))
        self.key_label.setText("✔ キー設定済み" if has else "⚠ キー未設定")
        self.key_label.setStyleSheet("color: #2F855A;" if has else "color: #C05621;")

    def _set_key(self):
        key, ok = QInputDialog.getText(self, "Claude APIキー",
                                       "Claude Console で作った APIキー（sk-ant-…）を貼り付けてください。\n"
                                       "このPCのユーザー設定に保存されます。空にすると削除します。",
                                       QLineEdit.Password, self.win.settings.value("api_key", ""))
        if ok:
            self.win.settings.setValue("api_key", key.strip())
            self.refresh_key()

    def mode(self) -> str:
        return self.mode_group.checkedButton().property("key")

    def save_settings(self):
        st = self.win.settings
        st.setValue("mode", self.mode())
        st.setValue("llm", self.llm.currentData())
        st.setValue("template", self.template.currentText())
        st.setValue("glossary", self.glossary.text())
        st.setValue("effort", self.effort.currentData())
        st.setValue("caption_chars", self.cap_chars.value())

    def _template_preview(self):
        tdir = Path(self.template.currentData())
        args = ["--template-preview", "--template", str(tdir)]
        if self.files.count():
            args += ["--sample", str(self.files.paths()[0])]

        def done(ok):
            if ok:
                PreviewImages(self, tdir).exec()
        self.win.runner.start("finish.py", args, "テンプレートの見本を作成中", done)

    # --- 実行 ---
    def out_dir(self) -> Path:
        p = self.files.paths()[0]
        return p.parent / f"{p.stem}_roughcut"

    def start(self, full: bool):
        if not self.files.count():
            QMessageBox.information(self, "素材がありません", "素材の動画をドラッグ＆ドロップしてください。")
            return
        llm = self.llm.currentData()
        if not self.win.check_api_key(llm):
            return
        self.save_settings()
        out = self.out_dir()
        out.mkdir(parents=True, exist_ok=True)
        args = [*map(str, self.files.paths()), "--mode", self.mode(), "--out", str(out),
                "--effort", self.effort.currentData(), "--caption-chars", str(self.cap_chars.value()),
                "--device", self.device.currentData()]
        if self.cam2.count():
            args += ["--cam2", *map(str, self.cam2.paths())]
        words = [w.strip() for w in re.split(r"[、,，\n]", self.glossary.text()) if w.strip()]
        if words:
            gp = out / "glossary.txt"
            gp.write_text("\n".join(words), encoding="utf-8")
            args += ["--glossary", str(gp)]
        tdir = self.template.currentData()
        self.win.run_roughcut(out, args, llm, then_finish=full, template=tdir)


class PreviewImages(QDialog):
    def __init__(self, parent, tdir: Path):
        super().__init__(parent)
        self.setWindowTitle("テンプレートの見本")
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        for n in ("preview_1.png", "preview_2.png", "preview_3.png"):
            lab = QLabel()
            pm = QPixmap(str(tdir / n))
            if not pm.isNull():
                lab.setPixmap(pm.scaledToWidth(420, Qt.SmoothTransformation))
            row.addWidget(lab)
        lay.addLayout(row)
        bb = QHBoxLayout()
        play = QPushButton("動画で見る")
        play.clicked.connect(lambda: open_path(tdir / "template_preview.mp4"))
        edit = QPushButton("template.toml を開く（見た目を変える）")
        edit.clicked.connect(lambda: open_path(tdir / "template.toml"))
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        for b in (play, edit):
            bb.addWidget(b)
        bb.addStretch()
        bb.addWidget(close)
        lay.addLayout(bb)


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
        files = sorted(tdir.glob("thumb_*.jpg"))
        rows = sorted({f.stem.split("_")[1] for f in files})
        for r, no in enumerate(rows):
            meta = next((i for i in info if str(i.get("image")) == no), {})
            cap = QLabel(f"案{no}：「{meta.get('text', '').replace(chr(92) + 'n', ' ')}」  {meta.get('reason', '')}")
            cap.setStyleSheet("font-weight: bold; margin-top: 6px;")
            grid.addWidget(cap, r * 2, 0, 1, 3)
            for c, f in enumerate(sorted(tdir.glob(f"thumb_{no}_*.jpg"))):
                grid.addWidget(ClickLabel(f), r * 2 + 1, c)
        lay.addLayout(grid)
        lay.addStretch(1)
        row = QHBoxLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("自分で文字を決めて作り直す（例: 軽すぎ！　2行にするときは \\n で区切る）")
        redo = QPushButton("この文字で作り直す")
        redo.clicked.connect(self._redo)
        again = QPushButton("AIに選び直してもらう")
        again.clicked.connect(lambda: (self.accept(), self.page.thumbnails(text="")))
        if page.win.create.llm.currentData() != "api":
            again.setEnabled(False)
            again.setToolTip("「作成」タブの AI を「Claude APIで自動」にすると、AIが画像を見て選び直します。")
        folder = QPushButton("フォルダを開く")
        folder.clicked.connect(lambda: open_path(tdir))
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        row.addWidget(self.text, 1)
        for b in (redo, again, folder, close):
            row.addWidget(b)
        lay.addLayout(row)

    def _redo(self):
        t = self.text.text().strip()
        if t:
            self.accept()
            self.page.thumbnails(text=t)


class YouTubeDialog(QDialog):
    """YouTube への投稿：Studio で動画をアップ → ツールがタイトル等を設定"""

    def __init__(self, page: "ReviewPage"):
        super().__init__(page)
        import rc_youtube as Y
        self.Y = Y
        self.page = page
        self.out = page.out
        self.setWindowTitle("YouTubeへ投稿")
        self.resize(900, 780)
        self.meta = Y.default_meta(self.out)

        steps = QLabel(
            "<b>投稿の流れ</b>（Googleの審査なしで使える方法です）<br>"
            "① 「YouTube Studio を開く」→ 完成動画をドラッグ＆ドロップ（公開設定は<b>非公開のまま</b>でOK）<br>"
            "② アップロードが始まったら「アップした動画に設定を反映」→ タイトル・概要欄・タグ・サムネイルが自動で入ります<br>"
            "③ Studio で確認して、公開（または予約公開）する")
        steps.setWordWrap(True)

        self.conn = QLabel()
        b_prep = QPushButton("連携の準備（client_secret.json）…")
        b_prep.clicked.connect(self._pick_secret)
        b_login = QPushButton("Googleアカウントと連携")
        b_login.clicked.connect(self._login)
        conn_row = QHBoxLayout()
        conn_row.addWidget(self.conn, 1)
        conn_row.addWidget(b_prep)
        conn_row.addWidget(b_login)

        form = QFormLayout()
        self.title = QComboBox()
        self.title.setEditable(True)
        for t in [self.meta["title"]] + [t for t in self.meta.get("title_candidates", []) if t != self.meta["title"]]:
            self.title.addItem(t)
        self.title_len = QLabel()
        self.title.editTextChanged.connect(lambda t: self.title_len.setText(f"{len(t)}/100"))
        self.title_len.setText(f"{len(self.meta['title'])}/100")
        tr = QHBoxLayout()
        tr.addWidget(self.title, 1)
        tr.addWidget(self.title_len)
        form.addRow("タイトル（候補から選ぶか直接入力）", tr)
        self.desc = QPlainTextEdit(self.meta["description"])
        self.desc.setMinimumHeight(220)
        form.addRow("概要欄（チャプター付き）", self.desc)
        self.tags = QLineEdit("、".join(self.meta["tags"]))
        form.addRow("タグ（「、」区切り）", self.tags)
        self.cat = QComboBox()
        for k, v in Y.CATEGORIES.items():
            self.cat.addItem(v, k)
        self.cat.setCurrentIndex(max(0, self.cat.findData(self.meta["category_id"])))
        form.addRow("カテゴリ", self.cat)

        self.thumb = QComboBox()
        self.thumb_view = QLabel()
        thumbs = sorted((self.out / "thumbnails").glob("thumb_*.jpg"))
        self.thumb.addItem("（設定しない）", "")
        for t in thumbs:
            self.thumb.addItem(t.name, str(t))
        self.thumb.currentIndexChanged.connect(self._show_thumb)
        self.thumb.setCurrentIndex(max(0, self.thumb.findData(self.meta.get("thumbnail", ""))))
        thr = QHBoxLayout()
        thr.addWidget(self.thumb)
        thr.addWidget(self.thumb_view, 1)
        form.addRow("サムネイル", thr)
        self._show_thumb()
        self.caps = QCheckBox("字幕ファイル（SRT）も追加する（字幕は動画に焼き込み済みなので、通常は不要）")
        self.caps.setChecked(bool(self.meta.get("upload_captions")))
        form.addRow("", self.caps)
        self.loc = QCheckBox("英語字幕・英語タイトルも設定する（Claudeで翻訳します。訳は保存され、2回目からは変わった所だけ訳します）")
        self.loc.setChecked(bool(self.meta.get("localize")))
        form.addRow("", self.loc)
        self.vid = QLineEdit(self.meta.get("video_id", ""))
        self.vid.setPlaceholderText("空なら、ファイル名と同じタイトルの動画を自動で探します（例: dQw4w9WgXcQ）")
        self.vid.setToolTip("YouTube Studio の動画の URL「studio.youtube.com/video/○○○/edit」の○○○の部分です")
        form.addRow("動画ID（空なら自動で探す）", self.vid)

        copy_row = QHBoxLayout()
        for text, fn in (("タイトルをコピー", lambda: self._copy(self.title.currentText())),
                         ("概要欄をコピー", lambda: self._copy(self.desc.toPlainText())),
                         ("タグをコピー", lambda: self._copy(",".join(self._tags())))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            copy_row.addWidget(b)
        self.copied = QLabel("")
        copy_row.addWidget(self.copied, 1)

        b_studio = QPushButton("① YouTube Studio を開く")
        b_studio.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://studio.youtube.com/")))
        b_folder = QPushButton("完成動画のフォルダを開く")
        b_folder.clicked.connect(lambda: open_path(self.out))
        b_apply = QPushButton("② アップした動画に設定を反映")
        b_apply.setStyleSheet("background: #C53030; color: white; font-weight: bold;")
        b_apply.clicked.connect(self._apply)
        b_upload = QPushButton("APIで動画ごとアップロード（審査済みの人向け）")
        b_upload.clicked.connect(self._upload)
        act = QHBoxLayout()
        for b in (b_studio, b_folder):
            act.addWidget(b)
        act.addStretch()
        act.addWidget(b_upload)
        act.addWidget(b_apply)

        b_reset = QPushButton("AIの案に戻す")
        b_reset.setToolTip("画面で直した内容を捨てて、AIの案と今の完成動画のチャプターから作り直します")
        b_reset.clicked.connect(self._reset)
        copy_row.addWidget(b_reset)

        self.notice = QLabel(self.meta.get("notice", ""))
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("background: #FFF3C4; padding: 6px;")
        self.notice.setVisible(bool(self.meta.get("notice")))
        self.meta.pop("notice", None)

        lay = QVBoxLayout(self)
        lay.addWidget(steps)
        lay.addWidget(self.notice)
        lay.addLayout(conn_row)
        lay.addLayout(form)
        lay.addWidget(QLabel("Googleの連携をしない場合は、下のボタンでコピーして Studio に貼り付けてください。"))
        lay.addLayout(copy_row)
        lay.addLayout(act)
        self._refresh_conn()

    def _tags(self) -> list[str]:
        return [t.strip() for t in re.split(r"[、,，]", self.tags.text()) if t.strip()]

    def _show_thumb(self):
        p = self.thumb.currentData()
        pm = QPixmap(p) if p else QPixmap()
        self.thumb_view.setPixmap(pm.scaledToWidth(240, Qt.SmoothTransformation) if not pm.isNull() else QPixmap())

    def _copy(self, text: str):
        QGuiApplication.clipboard().setText(text)
        self.copied.setText("コピーしました")

    def _refresh_conn(self):
        if self.Y.token_path().exists():
            self.conn.setText("✔ Googleアカウントと連携済み")
            self.conn.setStyleSheet("color: #2F855A;")
        elif self.Y.client_secret_path().exists():
            self.conn.setText("準備OK：「Googleアカウントと連携」を押してください")
            self.conn.setStyleSheet("color: #C05621;")
        else:
            self.conn.setText("⚠ Googleとの連携は未設定（README「YouTube連携の準備」参照）")
            self.conn.setStyleSheet("color: #C05621;")

    def _pick_secret(self):
        f, _ = QFileDialog.getOpenFileName(self, "Google Cloud でダウンロードした client_secret_….json を選ぶ",
                                           str(Path.home() / "Downloads"), "JSON (*.json)")
        if f:
            import shutil
            shutil.copy(f, self.Y.client_secret_path())
            self._refresh_conn()

    def collect(self) -> dict:
        m = dict(self.meta)
        m.update(title=self.title.currentText().strip(), description=self.desc.toPlainText(), tags=self._tags(),
                 category_id=self.cat.currentData(), thumbnail=self.thumb.currentData() or "",
                 upload_captions=self.caps.isChecked(), video_id=self.vid.text().strip(),
                 localize=self.loc.isChecked())
        return m

    def _set_fields(self, m: dict):
        self.meta = m
        self.title.clear()
        for t in [m["title"]] + [t for t in m.get("title_candidates", []) if t != m["title"]]:
            self.title.addItem(t)
        self.desc.setPlainText(m["description"])
        self.tags.setText("、".join(m["tags"]))
        self.cat.setCurrentIndex(max(0, self.cat.findData(m["category_id"])))
        self.thumb.setCurrentIndex(max(0, self.thumb.findData(m.get("thumbnail", ""))))
        self.caps.setChecked(bool(m.get("upload_captions")))
        self.loc.setChecked(bool(m.get("localize")))

    def _run_yt(self, args: list[str], title: str, done, handle_error=None):
        """youtube.py を実行する。「英語字幕・英語タイトルも設定する」のときは、先に translate.py で翻訳する"""
        if not self.loc.isChecked():
            return self.page.win.runner.start("youtube.py", args, title, done, handle_error=handle_error)
        args = args + ["--localize"]

        def translated(ok):
            if ok:
                self.page.win.runner.start("youtube.py", args, title, done, handle_error=handle_error)
        self.page.win.runner.start("translate.py", [str(self.out), "--template", self.page._template()],
                                   "英語字幕・英語タイトルを翻訳中", translated)

    def _reset(self):
        if QMessageBox.question(self, "AIの案に戻す", "タイトル・概要欄・タグなど、画面で直した内容を捨てて、"
                                "AIの案から作り直します。よろしいですか？") != QMessageBox.Yes:
            return
        m = self.Y.default_meta(self.out, use_saved=False)
        m["video_id"] = self.vid.text().strip()
        self._set_fields(m)
        self.notice.setVisible(False)

    def _save(self) -> bool:
        m = self.collect()
        errs = self.Y.check_meta(m)
        if errs:
            QMessageBox.warning(self, "確認してください", "\n".join(errs))
            return False
        self.Y.save_meta(self.out, m)
        return True

    def _login(self):
        if not self.Y.client_secret_path().exists():
            QMessageBox.information(self, "準備が必要です", "先に「連携の準備（client_secret.json）…」で、"
                                    "Google Cloud からダウンロードしたファイルを選んでください。")
            return
        self.page.win.runner.start("youtube.py", ["--login"], "Googleアカウントと連携中（ブラウザを確認してください）",
                                   lambda ok: self._refresh_conn())

    def _apply(self):
        if not self.Y.token_path().exists():
            QMessageBox.information(self, "連携が必要です", "先に「Googleアカウントと連携」をしてください。\n"
                                    "連携しない場合は、コピーボタンで Studio に貼り付けてください。")
            return
        if not self._save():
            return

        cand = self.out / "youtube_candidates.json"
        cand.unlink(missing_ok=True)

        def done(ok):
            if ok:
                m = self.Y.default_meta(self.out)
                self.meta["video_id"] = m.get("video_id", "")
                self.vid.setText(m.get("video_id", ""))
                if m.get("video_id"):
                    QDesktopServices.openUrl(QUrl(f"https://studio.youtube.com/video/{m['video_id']}/edit"))

        def not_found(_err) -> bool:
            """ファイル名と同じタイトルの動画がなかったときは、最近の動画から選んでもらう"""
            ups = read_json(cand, None)
            if ups is None:
                return False
            QTimer.singleShot(0, lambda: self._choose_video(ups))
            return True
        args = [str(self.out), "--apply"]
        if self.vid.text().strip():
            args += ["--video-id", self.vid.text().strip()]
        self._run_yt(args, "YouTubeの動画に設定を反映中", done, handle_error=not_found)

    def _choose_video(self, uploads: list[dict]):
        if not uploads:
            QMessageBox.information(self, "動画が見つかりません",
                                    "このチャンネルに動画が見つかりませんでした。先に YouTube Studio で完成動画を"
                                    "アップロードしてから、もう一度押してください。")
            return
        labels = [f"{u['title']}（{u.get('privacy', '')}・ID {u['id']}）" for u in uploads]
        item, ok = QInputDialog.getItem(
            self, "どの動画に設定しますか？",
            "完成動画のファイル名と同じタイトルの動画が見つかりませんでした。\n"
            "Studio でタイトルを変えた場合などに起こります。設定する動画を選んでください。", labels, 0, False)
        if ok and item:
            self.vid.setText(uploads[labels.index(item)]["id"])
            self._apply()

    def _upload(self):
        if QMessageBox.question(
                self, "動画ごとアップロード",
                "Googleの審査（audit）を通していないプロジェクトからアップロードした動画は、"
                "YouTubeの仕様で「非公開」にロックされ、公開できません。\n\n審査済みの場合だけ使ってください。続けますか？") \
                != QMessageBox.Yes:
            return
        if not self.Y.token_path().exists() or not self._save():
            if not self.Y.token_path().exists():
                QMessageBox.information(self, "連携が必要です", "先に「Googleアカウントと連携」をしてください。")
            return
        def done(ok):
            if ok:   # アップロードで決まった動画IDを、画面にも反映する（閉じたときに空で上書きしない）
                vid = self.Y.default_meta(self.out).get("video_id", "")
                self.meta["video_id"] = vid
                self.vid.setText(vid)
        self._run_yt([str(self.out), "--upload"], "YouTubeにアップロード中", done)

    def done(self, r):
        if self.out:
            m = self.collect()
            if not self.Y.check_meta(m):
                self.Y.save_meta(self.out, m)
        super().done(r)


# ============================================================
#  確認・仕上げタブ
# ============================================================
class ReviewPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.out: Path | None = None
        self.sess: dict = {}
        self.chunks: list[dict] = []
        self.offsets: list[tuple[float, float, str]] = []   # (開始, 長さ, ファイル)
        self._loading = False

        # 上部：フォルダ
        self.folder = QLabel("荒編集が終わると、ここで結果を確認できます。")
        self.folder.setStyleSheet("font-weight: bold;")
        self.summary = QLabel("")
        self.summary.setStyleSheet("color: #555;")
        open_btn = QPushButton("別の結果を開く…")
        open_btn.clicked.connect(self._open_other)
        fol_btn = QPushButton("フォルダを開く")
        fol_btn.clicked.connect(lambda: self.out and open_path(self.out))
        top = QHBoxLayout()
        tl = QVBoxLayout()
        tl.addWidget(self.folder)
        tl.addWidget(self.summary)
        top.addLayout(tl, 1)
        top.addWidget(open_btn)
        top.addWidget(fol_btn)

        # 動画プレーヤー
        self.player = None
        pl = QVBoxLayout()
        if HAS_MEDIA:
            self.video = QVideoWidget()
            self.video.setMinimumSize(360, 202)
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.stop_at = None
            self.timer = QTimer(self)
            self.timer.setInterval(50)
            self.timer.timeout.connect(self._tick)
            pl.addWidget(self.video, 1)
            pr = QHBoxLayout()
            self.play_btn = QPushButton("▶ 選んだ場面を再生")
            self.play_btn.clicked.connect(self.play_selected)
            pause = QPushButton("■ 停止")
            pause.clicked.connect(self.player.pause)
            self.play_info = QLabel("")
            pr.addWidget(self.play_btn)
            pr.addWidget(pause)
            pr.addWidget(self.play_info, 1)
            pl.addLayout(pr)
        else:
            pl.addWidget(QLabel("（この環境では動画の再生部品が使えません）"))
        player_box = QWidget()
        player_box.setLayout(pl)

        # カット一覧
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["使う", "時刻", "判定", "理由・メモ", "発言（字幕）※ダブルクリックで修正"])
        hh = self.table.horizontalHeader()
        for i, mode in enumerate([QHeaderView.ResizeToContents] * 3 + [QHeaderView.Interactive, QHeaderView.Stretch]):
            hh.setSectionResizeMode(i, mode)
        self.table.setColumnWidth(3, 220)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(lambda r, c: c in (1, 2, 3) and self.play_selected())
        self.filter = QComboBox()
        self.filter.addItems(["カット・要確認・見どころだけ表示", "すべての発言を表示"])
        self.filter.currentIndexChanged.connect(self._apply_filter)
        legend = QLabel("<span style='background:#F3D6D6'>&nbsp;カット&nbsp;</span> "
                        "<span style='background:#FFF3C4'>&nbsp;要確認&nbsp;</span> "
                        "<span style='background:#D8F0DA'>&nbsp;見どころ&nbsp;</span>　"
                        "「使う」のチェックを外すとカット、付けると残します")
        fr = QHBoxLayout()
        fr.addWidget(self.filter)
        fr.addWidget(legend, 1)
        cut_box = QWidget()
        cl = QVBoxLayout(cut_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addLayout(fr)
        cl.addWidget(self.table)

        # 演出プラン
        self.plan_tabs = QTabWidget()
        self.t_telop = QTableWidget(0, 5)
        self.t_telop.setHorizontalHeaderLabels(["使う", "元の時刻", "テロップの文字（ダブルクリックで修正）", "種類", "効果音"])
        self.t_zoom = QTableWidget(0, 4)
        self.t_zoom.setHorizontalHeaderLabels(["使う", "元の時刻", "発言", "強さ"])
        self.t_bgm = QTableWidget(0, 3)
        self.t_bgm.setHorizontalHeaderLabels(["元の時刻", "発言", "雰囲気"])
        for t in (self.t_telop, self.t_zoom, self.t_bgm):
            t.verticalHeader().setVisible(False)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            t.horizontalHeader().setStretchLastSection(False)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.cellClicked.connect(lambda r, c, tt=t: self._play_plan_row(tt, r))
        self.t_telop.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.t_zoom.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.t_bgm.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.plan_note = QLabel("「演出プランを作る」を押すと、AIが決めたテロップ・ズーム・BGMがここに表示されます。")
        self.plan_note.setWordWrap(True)
        self.plan_tabs.addTab(self.t_telop, "テロップ")
        self.plan_tabs.addTab(self.t_zoom, "ズーム")
        self.plan_tabs.addTab(self.t_bgm, "BGM")
        plan_box = QWidget()
        pbl = QVBoxLayout(plan_box)
        pbl.setContentsMargins(0, 0, 0, 0)
        pbl.addWidget(self.plan_note)
        pbl.addWidget(self.plan_tabs)

        self.tabs = QTabWidget()
        self.tabs.addTab(cut_box, "① カットの確認")
        self.tabs.addTab(plan_box, "② 演出の確認")

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.tabs)
        split.addWidget(player_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        # ボタン
        self.b_apply = QPushButton("カットの変更を反映")
        self.b_apply.setToolTip("チェックや字幕の修正を反映して、荒編集（XML・字幕）を作り直します")
        self.b_xml = QPushButton("Premiere用XMLを開く")
        self.b_plan = QPushButton("演出プランを作る")
        self.b_prev = QPushButton("最初の60秒を試し書き出し")
        self.b_final = QPushButton("完成動画を書き出す")
        self.b_final.setStyleSheet("QPushButton { background: #2B6CB0; color: white; font-weight: bold; }" + DISABLED_CSS)
        self.b_play = QPushButton("完成動画を再生")
        self.b_thumb = QPushButton("サムネイル案")
        self.b_thumb.setToolTip("動画の中からサムネイルに向いた場面を選び、文字入りの案を9枚作ります")
        self.b_thumb.clicked.connect(self.thumbnails)
        self.b_yt = QPushButton("YouTubeへ")
        self.b_yt.setStyleSheet("QPushButton { background: #C53030; color: white; font-weight: bold; }" + DISABLED_CSS)
        self.b_yt.clicked.connect(lambda: YouTubeDialog(self).exec())
        self.b_apply.clicked.connect(self.apply_cuts)
        self.b_xml.clicked.connect(self._open_xml)
        self.b_plan.clicked.connect(lambda: self.make_plan())
        self.b_prev.clicked.connect(lambda: self.render(preview=True))
        self.b_final.clicked.connect(lambda: self.render(preview=False))
        self.b_play.clicked.connect(self._play_final)
        bl = QHBoxLayout()
        for b in (self.b_apply, self.b_xml):
            bl.addWidget(b)
        bl.addStretch()
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        bl.addWidget(sep)
        for b in (self.b_plan, self.b_prev, self.b_final, self.b_play, self.b_thumb, self.b_yt):
            bl.addWidget(b)

        # 拡張機能（gui_ext_*.py）が「確認・仕上げ」タブにボタンを追加する場所
        self.ext_bar = QHBoxLayout()
        self.ext_bar.addStretch()

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(self.ext_bar)
        lay.addLayout(bl)
        self._update_buttons()

    # ---------- 読み込み ----------
    def _open_other(self):
        d = QFileDialog.getExistingDirectory(self, "○○_roughcut フォルダを選ぶ", self.win.settings.value("last_dir", ""))
        if d:
            self.load(Path(d))

    def load(self, out: Path):
        sess = read_json(out / "session.json")
        if not sess:
            QMessageBox.warning(self, "読み込めません", f"{out} に session.json がありません。先に荒編集を実行してください。")
            return
        self.out = out
        self.sess = sess
        self.chunks = read_json(out / "chunks.json", [])
        self.decisions = read_json(out / "decisions_used.json", {}) or {}
        self.folder.setText(f"{sess['stem']}（{dict((m[0], m[1]) for m in MODES).get(sess['mode'], sess['mode'])}）")
        self._compute_offsets()
        self._fill_cuts()
        self._fill_plan()
        self._update_buttons()

    def _compute_offsets(self):
        self.offsets = []
        try:
            from rc_media import probe, require_ffmpeg
            require_ffmpeg()
            t = 0.0
            for p in self.sess["inputs"]:
                d = probe(Path(p)).duration
                self.offsets.append((t, d, p))
                t += d
        except SystemExit:
            pass

    def _fill_cuts(self):
        self._loading = True
        cut = set(self.sess.get("cut_ids", []))
        corr = {int(k): v for k, v in self.sess.get("corrections", {}).items()}
        info = {}
        for c in self.decisions.get("cuts", []):
            for i in c["ids"]:
                info[i] = (TYPE_JA.get(c.get("type"), c.get("type", "")), c.get("reason", ""))
        review = {r["id"]: r["note"] for r in self.decisions.get("review", [])}
        hil = {h["id"]: h["label"] for h in self.decisions.get("highlights", [])}
        self.table.setRowCount(len(self.chunks))
        n_cut = 0
        for r, c in enumerate(self.chunks):
            cid = c["id"]
            is_cut = cid in cut
            n_cut += is_cut
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Unchecked if is_cut else Qt.Checked)
            chk.setData(Qt.UserRole, cid)
            kind, reason = info.get(cid, ("", ""))
            if is_cut and not kind:
                kind, reason = ("フィラー", "自動判定") if is_filler_only(c["text"]) else ("手動", "")
            if cid in review:
                kind, reason = kind or "要確認", (reason + " / " if reason else "") + review[cid]
            if cid in hil:
                kind, reason = kind or "見どころ", (reason + " / " if reason else "") + "★" + hil[cid]
            cells = [chk, QTableWidgetItem(fmt(c["start"])), QTableWidgetItem(kind), QTableWidgetItem(reason),
                     QTableWidgetItem(corr.get(cid, c["text"]))]
            for col, it in enumerate(cells):
                if col in (1, 2, 3):
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(r, col, it)
            self.table.item(r, 4).setData(Qt.UserRole, c["text"])
            self.table.item(r, 0).setData(Qt.UserRole + 1, "review" if cid in review else "hil" if cid in hil else "")
            self._color_row(r)
        self._loading = False
        total = self.chunks[-1]["end"] if self.chunks else 0
        self.summary.setText(f"発言 {len(self.chunks)}個のうち {n_cut}個をカット　/　元の長さ {fmt(total)}　"
                             f"→ 結果は report.md・{self.sess.get('stem', '')}_荒編集.xml を参照")
        self._apply_filter()

    def _color_row(self, r: int):
        cut = self.table.item(r, 0).checkState() == Qt.Unchecked
        tag = self.table.item(r, 0).data(Qt.UserRole + 1)
        color = C_CUT if cut else C_REVIEW if tag == "review" else C_HILITE if tag == "hil" else None
        for col in range(5):
            it = self.table.item(r, col)
            it.setBackground(QBrush(color) if color else QBrush())
            f = it.font()
            f.setStrikeOut(cut and col == 4)
            it.setFont(f)

    def _item_changed(self, it: QTableWidgetItem):
        if self._loading:
            return
        if it.column() == 0:
            self._loading = True
            self._color_row(it.row())
            self._loading = False
        self.b_apply.setText("カットの変更を反映 ●")

    def _apply_filter(self):
        only = self.filter.currentIndex() == 0
        for r in range(self.table.rowCount()):
            show = True
            if only:
                cut = self.table.item(r, 0).checkState() == Qt.Unchecked
                show = cut or bool(self.table.item(r, 0).data(Qt.UserRole + 1)) or bool(self.table.item(r, 2).text())
            self.table.setRowHidden(r, not show)

    # ---------- 再生 ----------
    def _locate(self, t: float):
        for start, dur, path in self.offsets:
            if start <= t < start + dur:
                return path, t - start
        return (self.offsets[-1][2], t - self.offsets[-1][0]) if self.offsets else (None, 0)

    def play_range(self, s: float, e: float):
        if not self.player:
            return
        path, local = self._locate(max(0, s - 0.8))
        if not path:
            return
        if self.player.source() != QUrl.fromLocalFile(path):
            self.player.setSource(QUrl.fromLocalFile(path))
        self.player.setPosition(int(local * 1000))
        self.stop_at = (local + (e - s) + 1.6) * 1000
        self.player.play()
        self.timer.start()
        self.play_info.setText(f"{fmt(s)} 〜 {fmt(e)}")

    def _tick(self):
        if self.stop_at and self.player.position() >= self.stop_at:
            self.player.pause()
            self.timer.stop()

    def play_selected(self):
        r = self.table.currentRow()
        if 0 <= r < len(self.chunks):
            self.play_range(self.chunks[r]["start"], self.chunks[r]["end"])

    def _chunk(self, cid):
        return next((c for c in self.chunks if c["id"] == cid), None)

    def _play_plan_row(self, table: QTableWidget, r: int):
        it = table.item(r, 0)
        cid = it.data(Qt.UserRole) if it else None
        c = self._chunk(cid)
        if c:
            self.play_range(c["start"], c["end"])

    # ---------- カットの反映 ----------
    def _desired(self):
        cut, keep, corr = [], [], {}
        for r in range(self.table.rowCount()):
            cid = self.table.item(r, 0).data(Qt.UserRole)
            (cut if self.table.item(r, 0).checkState() == Qt.Unchecked else keep).append(cid)
            text = self.table.item(r, 4).text().strip()
            if text and text != self.table.item(r, 4).data(Qt.UserRole):
                corr[cid] = text
        return cut, keep, corr

    def _save_plan_edits(self) -> Path | None:
        """演出の表の今の内容を plan_edited.json に保存する（表が空なら何もしない）"""
        if not self.out or not (self.out / "plan_used.json").exists() or self._plan_rows() == 0:
            return None
        p = self.out / "plan_edited.json"
        p.write_text(json.dumps(self._collect_plan(), ensure_ascii=False, indent=1), encoding="utf-8")
        return p

    def _plan_rows(self) -> int:
        return self.t_telop.rowCount() + self.t_zoom.rowCount() + self.t_bgm.rowCount()

    def _discard_plan_edits(self):
        """演出プランを作り直したときは、前のプランへの手直しは使わない"""
        if self.out:
            (self.out / "plan_edited.json").unlink(missing_ok=True)

    def apply_cuts(self, then=None):
        if not self.out:
            return
        self._save_plan_edits()   # 読み込み直しで、演出の表の手直しが消えないように
        cut, keep, corr = self._desired()
        d = dict(self.decisions)
        d["manual"] = {"cut": cut, "keep": keep}
        base = {c["id"]: c["text"] for c in self.chunks}
        d["corrections"] = [{"id": k, "text": v} for k, v in corr.items() if base.get(k) != v]
        (self.out / "decisions_edited.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.out / "meta_edited.json").write_text(json.dumps(self.sess.get("meta") or {}, ensure_ascii=False),
                                                   encoding="utf-8")
        a = self.sess.get("args", {})
        args = [*self.sess["inputs"], "--mode", self.sess["mode"], "--out", str(self.out), "--llm", "none",
                "--decisions", str(self.out / "decisions_edited.json"),
                "--caption-chars", str(a.get("caption_chars", 24))]
        if self.sess.get("meta"):
            args += ["--meta", str(self.out / "meta_edited.json")]
        if a.get("cam2"):
            args += ["--cam2", *a["cam2"]]
        if a.get("glossary"):
            args += ["--glossary", a["glossary"]]
        if a.get("whisper_model"):
            args += ["--whisper-model", a["whisper_model"]]

        def done(ok):
            if ok:
                self.load(self.out)
                self.b_apply.setText("カットの変更を反映")
                if then:
                    then()
        self.win.runner.start("roughcut.py", args, "カットの変更を反映中", done)

    def _dirty(self) -> bool:
        return self.b_apply.text().endswith("●")

    # ---------- 演出プラン ----------
    def _template(self) -> str:
        return self.win.create.template.currentData()

    def make_plan(self, then=None):
        """演出プランを作る。then があれば、プランができたあとに呼ぶ（「完成まで一気に作る」用）"""
        if not self.out:
            return
        llm = self.win.create.llm.currentData()
        if not self.win.check_api_key(llm):
            return
        if self._dirty():
            self.apply_cuts(then=lambda: self.make_plan(then))
            return
        args = [str(self.out), "--template", self._template(), "--plan-only", "--llm", llm,
                "--effort", self.win.create.effort.currentData()]

        def done(ok):
            if not ok:
                return
            if llm == "manual" and (self.out / "prompt_plan.md").exists() and not (self.out / "plan_used.json").exists():
                self._manual_plan(then)
            else:
                self._discard_plan_edits()
                self._fill_plan()
                self.tabs.setCurrentIndex(1)
                if then:
                    then()
        if llm == "manual":
            (self.out / "plan_used.json").unlink(missing_ok=True)
        self.win.runner.start("finish.py", args, "演出プランを作成中", done)

    def _manual_plan(self, then=None):
        dlg = ManualDialog(self, self.out / "prompt_plan.md", "演出プランを作る")
        if dlg.exec():
            p = self.out / "claude_plan.json"
            p.write_text(dlg.text(), encoding="utf-8")
            args = [str(self.out), "--template", self._template(), "--plan-only", "--plan", str(p)]

            def done(ok):
                if not ok:
                    return
                self._discard_plan_edits()
                self._fill_plan()
                self.tabs.setCurrentIndex(1)
                if then:
                    then()
            self.win.runner.start("finish.py", args, "演出プランを読み込み中", done)

    def _combo(self, mapping: dict, value: str) -> QComboBox:
        cb = QComboBox()
        for k, v in mapping.items():
            cb.addItem(v, k)
        cb.setCurrentIndex(max(0, cb.findData(value)))
        return cb

    def _check(self, cid, on: bool = True) -> QTableWidgetItem:
        it = QTableWidgetItem()
        it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        it.setCheckState(Qt.Checked if on else Qt.Unchecked)
        it.setData(Qt.UserRole, cid)
        return it

    def _ro(self, text) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return it

    def _plan_source(self) -> Path | None:
        """表に出すプラン。手直し（plan_edited.json）があればそちらを優先する（rc_thumb と同じ順）"""
        if not self.out:
            return None
        used, edited = self.out / "plan_used.json", self.out / "plan_edited.json"
        if edited.exists() and used.exists():
            return edited
        return used if used.exists() else None

    def _fill_plan(self):
        src = self._plan_source()
        plan = read_json(src) if src else None
        for t in (self.t_telop, self.t_zoom, self.t_bgm):
            t.setRowCount(0)
        if not plan:
            self.plan_note.setVisible(True)
            self._update_buttons()
            return
        self.plan_note.setText("行をクリックするとその場面を再生します。チェックを外したものは使いません。"
                               "変更は「試し書き出し」「完成動画を書き出す」のときに反映されます。")
        moods = {m: MOOD_JA[m] for m in MOOD_JA}
        off = plan.get("_off") or {}   # 手直しでチェックを外したもの（書き出しには使わない）
        start = {c["id"]: c["start"] for c in self.chunks}

        def merged(key, idk):
            rows = [(x, True) for x in plan.get(key, [])] + [(x, False) for x in off.get(key, [])]
            return sorted(rows, key=lambda r: start.get(r[0].get(idk), 0))
        for t, on in merged("telops", "start_id"):
            c = self._chunk(t["start_id"]) or {"start": 0, "text": ""}
            r = self.t_telop.rowCount()
            self.t_telop.insertRow(r)
            chk = self._check(t["start_id"], on)
            chk.setData(Qt.UserRole + 1, t["end_id"])
            self.t_telop.setItem(r, 0, chk)
            self.t_telop.setItem(r, 1, self._ro(fmt(c["start"])))
            self.t_telop.setItem(r, 2, QTableWidgetItem(t["text"]))
            self.t_telop.setCellWidget(r, 3, self._combo(STYLE_JA, t.get("style", "emphasis")))
            self.t_telop.setCellWidget(r, 4, self._combo(SE_JA, t.get("se", "none")))
        for z, on in merged("zooms", "id"):
            c = self._chunk(z["id"]) or {"start": 0, "text": ""}
            r = self.t_zoom.rowCount()
            self.t_zoom.insertRow(r)
            self.t_zoom.setItem(r, 0, self._check(z["id"], on))
            self.t_zoom.setItem(r, 1, self._ro(fmt(c["start"])))
            self.t_zoom.setItem(r, 2, self._ro(c["text"]))
            self.t_zoom.setCellWidget(r, 3, self._combo({"small": "弱め", "medium": "強め"}, z.get("strength", "small")))
        for b in plan.get("bgm", []):
            c = self._chunk(b["start_id"]) or {"start": 0, "text": ""}
            r = self.t_bgm.rowCount()
            self.t_bgm.insertRow(r)
            it = self._ro(fmt(c["start"]))
            it.setData(Qt.UserRole, b["start_id"])
            self.t_bgm.setItem(r, 0, it)
            self.t_bgm.setItem(r, 1, self._ro(c["text"]))
            self.t_bgm.setCellWidget(r, 2, self._combo(moods, b.get("mood", "calm")))
        self.plan_tabs.setTabText(0, f"テロップ（{self.t_telop.rowCount()}）")
        self.plan_tabs.setTabText(1, f"ズーム（{self.t_zoom.rowCount()}）")
        self.plan_tabs.setTabText(2, f"BGM（{self.t_bgm.rowCount()}）")
        self._update_buttons()

    def _collect_plan(self) -> dict:
        """表の内容を演出プランにする。チェックを外したものは "_off" に入れておく（書き出しでは使わない）"""
        plan = {"telops": [], "zooms": [], "bgm": [], "_off": {"telops": [], "zooms": []}}
        for r in range(self.t_telop.rowCount()):
            it = self.t_telop.item(r, 0)
            row = {"start_id": it.data(Qt.UserRole), "end_id": it.data(Qt.UserRole + 1),
                   "text": self.t_telop.item(r, 2).text().strip(),
                   "style": self.t_telop.cellWidget(r, 3).currentData(),
                   "se": self.t_telop.cellWidget(r, 4).currentData()}
            if it.checkState() == Qt.Checked and row["text"]:
                plan["telops"].append(row)
            else:
                plan["_off"]["telops"].append(row)
        for r in range(self.t_zoom.rowCount()):
            it = self.t_zoom.item(r, 0)
            row = {"id": it.data(Qt.UserRole), "strength": self.t_zoom.cellWidget(r, 3).currentData()}
            (plan["zooms"] if it.checkState() == Qt.Checked else plan["_off"]["zooms"]).append(row)
        for r in range(self.t_bgm.rowCount()):
            plan["bgm"].append({"start_id": self.t_bgm.item(r, 0).data(Qt.UserRole),
                                "mood": self.t_bgm.cellWidget(r, 2).currentData()})
        return plan

    # ---------- 書き出し ----------
    def render(self, preview: bool):
        if not self.out:
            return
        if self._dirty():
            self.apply_cuts(then=lambda: self.render(preview))
            return
        if not (self.out / "plan_used.json").exists():
            QMessageBox.information(self, "演出プランがありません", "先に「演出プランを作る」を押してください。\n"
                                    "（「AIを使わない」を選んでいる場合は、字幕とBGMだけのプランが作られます）")
            return
        p = self.out / "plan_edited.json"
        p.write_text(json.dumps(self._collect_plan(), ensure_ascii=False, indent=1), encoding="utf-8")
        # --llm は書き出し後のサムネイル案で使う（「Claude APIで自動」なら AI が画像を見て選ぶ）
        llm = self.win.create.llm.currentData()
        if llm == "api" and not has_api_key(self.win.settings):
            llm = "none"
        args = [str(self.out), "--template", self._template(), "--plan", str(p), "--llm", llm,
                "--effort", self.win.create.effort.currentData()]
        if preview:
            args += ["--preview", "60"]
        name = f"{self.sess['stem']}_{'プレビュー' if preview else '完成'}.mp4"

        def done(ok):
            self._update_buttons()
            if ok and (self.out / name).exists():
                if QMessageBox.question(self, "書き出し完了", f"{name} ができました。再生しますか？") == QMessageBox.Yes:
                    open_path(self.out / name)
        self.win.runner.start("finish.py", args, "試し書き出し中（60秒）" if preview else "完成動画を書き出し中", done)

    def _final_path(self) -> Path | None:
        return self.out / f"{self.sess['stem']}_完成.mp4" if self.out else None

    def _play_final(self):
        p = self._final_path()
        if p and p.exists():
            open_path(p)

    def _open_xml(self):
        if self.out:
            xs = list(self.out.glob("*_荒編集.xml"))
            if xs:
                QMessageBox.information(self, "Premiereで開く",
                                        f"Premiere Pro の「ファイル → 読み込み」で次のファイルを選んでください。\n\n{xs[0]}\n\n"
                                        "（このあとフォルダを開きます）")
                open_path(self.out)

    def thumbnails(self, text: str | None = None):
        """サムネイル案があれば表示し、なければ作る（text 指定時は、その文字で作り直す）"""
        if not self.out:
            return
        tdir = self.out / "thumbnails"
        if text is None and list(tdir.glob("thumb_*.jpg")):
            ThumbDialog(self, tdir).exec()
            return
        if not text and not self.win.check_api_key(self.win.create.llm.currentData()):
            return
        args = [str(self.out), "--template", self._template(),
                "--llm", self.win.create.llm.currentData(), "--effort", self.win.create.effort.currentData()]
        if text:
            args += ["--text", text]

        def done(ok):
            self._update_buttons()
            if ok and list(tdir.glob("thumb_*.jpg")):
                ThumbDialog(self, tdir).exec()
        self.win.runner.start("thumbs.py", args, "サムネイル案を作成中", done)

    def _update_buttons(self):
        if self.win.runner.busy:   # 処理中は押せないようにする（押せない理由が見て分かるように）
            for b in (self.b_apply, self.b_xml, self.b_plan, self.b_prev, self.b_final, self.b_play, self.b_thumb,
                      self.b_yt):
                b.setEnabled(False)
            return
        has = self.out is not None
        has_plan = has and (self.out / "plan_used.json").exists()
        for b in (self.b_apply, self.b_xml, self.b_plan):
            b.setEnabled(has)
        self.b_prev.setEnabled(has_plan)
        self.b_final.setEnabled(has_plan)
        fp = self._final_path()
        self.b_play.setEnabled(bool(fp and fp.exists()))
        self.b_thumb.setEnabled(has)
        self.b_yt.setEnabled(bool(fp and fp.exists()))
        has_thumbs = has and bool(list((self.out / "thumbnails").glob("thumb_*.jpg")))
        self.b_thumb.setText("サムネイル案を見る" if has_thumbs else "サムネイル案を作る")


# ============================================================
#  メインウィンドウ
# ============================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI動画編集ツール")
        self.resize(1200, 820)
        self.settings = QSettings("ai-roughcut", "AutoVideoEditor")
        self.runner = Runner(self.settings)
        self.create = CreatePage(self)
        self.review = ReviewPage(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.create, "作成")
        self.tabs.addTab(self.review, "確認・仕上げ")
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(self.runner)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.tools_menu = self.menuBar().addMenu("ツール")
        self.help_menu = self.menuBar().addMenu("ヘルプ")
        self.runner.busy_changed.connect(self._busy_changed)
        self.load_extensions()
        # 拡張が何も追加しなかったメニューは、空のまま出さない
        for m in (self.tools_menu, self.help_menu):
            m.menuAction().setVisible(bool(m.actions()))

    def check_api_key(self, llm: str) -> bool:
        """AIを「Claude APIで自動」にしているのにキーがないときは、先に知らせて止める"""
        if llm == "api" and not has_api_key(self.settings):
            QMessageBox.information(self, "APIキーが未設定です",
                                    "「作成」タブの「APIキーを設定…」から設定するか、AIの欄で"
                                    "「claude.ai に貼り付けて使う」または「AIを使わない」を選んでください。")
            return False
        return True

    def _busy_changed(self, busy: bool):
        """処理中は、別の処理を始めるボタンを押せないようにする"""
        for b in (self.create.btn_rough, self.create.btn_full):
            b.setEnabled(not busy)
        self.review._update_buttons()

    def load_extensions(self):
        """gui_ext_*.py を読み込む。各ファイルは register(win) を持ち、メニュー・タブ・ボタンを追加する。

        追加先の例:
          win.tools_menu / win.help_menu      … メニュー（QMenu）
          win.tabs                            … タブ（QTabWidget）
          win.review.ext_bar                  … 「確認・仕上げ」タブのボタン列（QHBoxLayout）
          win.runner.start(script, args, title, on_done)  … 処理の実行（進み具合の表示つき）
        1つの拡張が壊れていても、ほかは動くようにする。
        """
        import importlib
        self.extensions = []
        failed = []
        for f in sorted(APP_DIR.glob("gui_ext_*.py")):
            try:
                mod = importlib.import_module(f.stem)
                mod.register(self)
                self.extensions.append(f.stem)
            except Exception as e:  # noqa: BLE001
                with open(APP_DIR / "gui_error.log", "a", encoding="utf-8") as fp:
                    fp.write(f"===== 拡張 {f.stem} =====\n{traceback.format_exc()}\n")
                print(f"拡張 {f.stem} を読み込めませんでした: {e}", file=sys.stderr)
                self.runner.log.appendPlainText(f"[注意] 拡張 {f.name} を読み込めませんでした: {e}")
                failed.append(f.name)
        if failed:
            # pythonw で起動していると stderr は見えないので、画面の下に出す
            self.statusBar().showMessage(f"拡張機能 {'・'.join(failed)} を読み込めませんでした"
                                         f"（詳細: gui_error.log）。ほかの機能はそのまま使えます。")

    # 荒編集の実行（手動モードの貼り付けにも対応）
    def run_roughcut(self, out: Path, args: list[str], llm: str, then_finish: bool, template: str,
                     decisions: Path | None = None, meta: Path | None = None):
        full = list(args) + ["--llm", llm]
        if decisions:
            full += ["--decisions", str(decisions)]
        if meta:
            full += ["--meta", str(meta)]

        def done(ok):
            if not ok:
                return
            if llm == "manual" and decisions is None:
                dlg = ManualDialog(self, out / "prompt_cuts.md", "カットを判定する")
                if not dlg.exec():
                    return
                p = out / "claude_cuts.json"
                p.write_text(dlg.text(), encoding="utf-8")
                self.run_roughcut(out, args, llm, then_finish, template, decisions=p)
                return
            if llm == "manual" and meta is None and (out / "prompt_youtube.md").exists():
                dlg = ManualDialog(self, out / "prompt_youtube.md", "タイトル・概要欄・チャプターを作る", can_skip=True)
                if dlg.exec() and not dlg.skipped:
                    p = out / "claude_meta.json"
                    p.write_text(dlg.text(), encoding="utf-8")
                    self.run_roughcut(out, args, llm, then_finish, template, decisions=decisions, meta=p)
                    return
            self.review.load(out)
            self.tabs.setCurrentWidget(self.review)
            if then_finish:
                self.review.make_plan_and_render()

        self.runner.start("roughcut.py", full, "荒編集を実行中", done)

    def closeEvent(self, e):
        if self.runner.busy and QMessageBox.question(
                self, "実行中です", "処理を中止して終了しますか？") != QMessageBox.Yes:
            e.ignore()
            return
        self.runner.stop(wait=True)
        self.create.save_settings()
        e.accept()


def _make_plan_and_render(self: ReviewPage):
    """「完成まで一気に作る」：プラン作成 → そのまま書き出し"""
    llm = self.win.create.llm.currentData()
    if not self.win.check_api_key(llm):
        return
    if llm == "manual":
        # 貼り付け画面で演出プランを受け取ったら、そのまま完成動画の書き出しまで進める
        self.make_plan(then=lambda: self.render(preview=False))
        return
    args = [str(self.out), "--template", self._template(), "--llm", llm,
            "--effort", self.win.create.effort.currentData()]

    def done(ok):
        self._fill_plan()
        self._update_buttons()
        if ok and self._final_path().exists():
            if QMessageBox.question(self, "完成しました", f"{self._final_path().name} ができました。再生しますか？") \
                    == QMessageBox.Yes:
                open_path(self._final_path())
    self.win.runner.start("finish.py", args, "演出プラン → 完成動画を書き出し中", done)


ReviewPage.make_plan_and_render = _make_plan_and_render


def main():
    def excepthook(t, v, tb):
        msg = "".join(traceback.format_exception(t, v, tb))
        (APP_DIR / "gui_error.log").write_text(msg, encoding="utf-8")
        QMessageBox.critical(None, "エラー", f"予期しないエラーが起きました。\n{v}\n\n詳細: gui_error.log")
    sys.excepthook = excepthook
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    f = app.font()
    f.setFamilies(["Yu Gothic UI", "Meiryo UI", f.family()])
    f.setPointSize(10)
    app.setFont(f)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
