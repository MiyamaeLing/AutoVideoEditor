"""「ヘルプ」メニューの拡張：困ったときの情報をコピー・最新版に更新・起動時の新しい版のお知らせ。"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

import rc_ops
import rc_ops_update

APP_DIR = Path(__file__).resolve().parent


class _Bridge(QObject):
    """裏のスレッドの結果を、画面のスレッドで受け取るための中継"""
    done = Signal(object)


def _in_background(win, fn, then):
    """fn() を裏で動かして、終わったら then(結果) を画面のスレッドで呼ぶ（例外は結果として渡す）"""
    br = _Bridge(win)
    br.done.connect(then)
    keep = getattr(win, "_ops_bridges", [])
    keep.append(br)
    win._ops_bridges = keep

    def work():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            res = e
        br.done.emit(res)
    threading.Thread(target=work, daemon=True).start()
    return br


def _bool(v, default=True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes")


# ============================================================
#  困ったときの情報
# ============================================================
class SupportDialog(QDialog):
    def __init__(self, parent, text: str, zip_path: Path):
        super().__init__(parent)
        self.setWindowTitle("困ったときの情報")
        self.resize(820, 620)
        self.text = text
        head = QLabel("<b>クリップボードにコピーしました。</b> 相談するときは、メッセージ欄に貼り付けてください（Ctrl+V）。<br>"
                      f"同じ内容とログ全体を zip にも保存しました：{zip_path.name}<br>"
                      "<span style='color:#2F855A'>APIキー・Googleのトークン・Windowsのユーザー名は伏せてあります。</span>")
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 9))
        b_copy = QPushButton("もう一度コピー")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text))
        b_zip = QPushButton("zip の場所を開く")
        b_zip.clicked.connect(lambda: _open(zip_path.parent))
        b_close = QPushButton("閉じる")
        b_close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(b_copy)
        row.addWidget(b_zip)
        row.addStretch()
        row.addWidget(b_close)
        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(view, 1)
        lay.addLayout(row)


def _open(p: Path):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def copy_support_info(win, show_dialog: bool = True):
    st = win.settings
    settings = {k: st.value(k) for k in st.allKeys()}
    gui_log = win.runner.log.toPlainText()
    folder = getattr(win.review, "out", None)
    extra = {}
    if win.runner.last_error:
        extra["最後のエラー"] = win.runner.last_error
    ext = getattr(win, "extensions", [])
    extra["読み込んだ拡張機能"] = "、".join(ext) if ext else "なし"
    win.statusBar().showMessage("困ったときの情報を集めています…（数秒かかります）")
    QApplication.setOverrideCursor(Qt.WaitCursor)

    def work():
        text, files = rc_ops.collect(APP_DIR, gui_log, settings, folder, extra)
        return text, rc_ops.save_zip(files)

    def done(res):
        QApplication.restoreOverrideCursor()
        if isinstance(res, Exception):
            win.statusBar().showMessage("")
            QMessageBox.warning(win, "まとめられませんでした", f"困ったときの情報をまとめられませんでした。\n{res}")
            return
        text, zp = res
        QGuiApplication.clipboard().setText(text)
        win.statusBar().showMessage(f"困ったときの情報をクリップボードにコピーしました（zip: {zp}）", 15000)
        win._ops_last_support = (text, zp)
        if show_dialog:
            dlg = SupportDialog(win, text, zp)
            win._ops_support_dialog = dlg
            dlg.show()
    return _in_background(win, work, done)


# ============================================================
#  最新版に更新
# ============================================================
class UpdateBanner(QWidget):
    """画面の下に出す「新しい版があります」のお知らせ"""

    def __init__(self, win, remote: dict):
        super().__init__()
        lab = QLabel(f"新しい版があります（{rc_ops_update.version_text(remote)} {remote.get('message', '')[:40]}）")
        lab.setStyleSheet("background: #FFF3C4; color: #744210; border-radius: 4px; padding: 2px 8px; font-weight: bold;")
        b = QPushButton("更新する")
        b.clicked.connect(lambda: start_update(win))
        x = QPushButton("あとで")
        x.clicked.connect(self.hide)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.addWidget(lab)
        lay.addWidget(b)
        lay.addWidget(x)


def check_on_startup(win):
    if os.environ.get("AVE_UPDATE_CHECK", "1") == "0":
        return None

    def done(res):
        if isinstance(res, Exception) or not res:
            return   # ネットにつながっていないときなどは、何も言わない
        newer, _loc, rem = res
        win._ops_update_info = res
        if newer and not getattr(win, "_ops_banner", None):
            win._ops_banner = UpdateBanner(win, rem)
            win.statusBar().addPermanentWidget(win._ops_banner)
    return _in_background(win, lambda: rc_ops_update.check(APP_DIR, timeout=8), done)


def restart(win):
    QProcess.startDetached(sys.executable, [str(APP_DIR / "gui.py")], str(APP_DIR))
    win.close()


def start_update(win):
    if win.runner.busy:
        QMessageBox.information(win, "実行中", "ほかの処理が実行中です。終わってから更新してください。")
        return
    if (APP_DIR / ".git").exists():
        QMessageBox.information(win, "git で管理されています",
                                "このフォルダは git で管理されているので、自動更新は使いません。\n"
                                "コマンドで `git pull` を実行して更新してください。")
        return
    win.statusBar().showMessage("最新の版を確認しています…")
    QApplication.setOverrideCursor(Qt.WaitCursor)

    def checked(res):
        QApplication.restoreOverrideCursor()
        win.statusBar().showMessage("")
        if isinstance(res, Exception):
            QMessageBox.warning(win, "確認できませんでした", str(res))
            return
        newer, loc, rem = res
        if newer:
            body = (f"新しい版があります。\n\n今の版： {rc_ops_update.version_text(loc)}\n"
                    f"最新の版：{rc_ops_update.version_text(rem)}\n　　{rem.get('message', '')}\n\n"
                    "更新しますか？\n・自分で直したテンプレート、画面の設定、順番待ちの一覧はそのまま残ります\n"
                    "・上書きするファイルはバックアップします（元に戻せます）\n"
                    "・必要なライブラリが増えたときは、自動で追加します（数分かかることがあります）")
            args = ["--yes"]
        else:
            body = (f"すでに最新の版です（{rc_ops_update.version_text(rem)}）。\n\n"
                    "ファイルが壊れたなどで入れ直したいときだけ「はい」を押してください。")
            args = ["--yes", "--force"]
        if QMessageBox.question(win, "最新版に更新", body) != QMessageBox.Yes:
            return
        before = rc_ops_update.local_version(APP_DIR)

        def done(ok):
            after = rc_ops_update.local_version(APP_DIR)
            if not ok:
                return
            if (after.get("sha") and after.get("sha") != before.get("sha")) or not newer:
                if getattr(win, "_ops_banner", None):
                    win._ops_banner.hide()
                if QMessageBox.question(win, "更新しました",
                                        "更新が終わりました。開き直すと新しい版になります。\n今すぐ開き直しますか？") \
                        == QMessageBox.Yes:
                    restart(win)
        win.runner.start("update.py", args, "最新版に更新中", done)
    return _in_background(win, lambda: rc_ops_update.check(APP_DIR, timeout=15), checked)


def register(win):
    st = win.settings
    a = win.help_menu.addAction("困ったときの情報をコピー")
    a.setToolTip("ログ・PCの環境・設定（APIキーは伏せる）をまとめて、クリップボードと zip に入れます")
    a.triggered.connect(lambda: copy_support_info(win))
    win.help_menu.addSeparator()
    u = win.help_menu.addAction("最新版に更新…")
    u.triggered.connect(lambda: start_update(win))
    c = win.help_menu.addAction("起動時に新しい版を確認する")
    c.setCheckable(True)
    c.setChecked(_bool(st.value("update_check_on_start"), True))
    c.toggled.connect(lambda on: st.setValue("update_check_on_start", on))
    v = win.help_menu.addAction(f"このツールの版：{rc_ops_update.version_text(rc_ops_update.local_version(APP_DIR))}")
    v.setEnabled(False)
    win.ops_actions = {"support": a, "update": u, "check": c, "version": v}
    if c.isChecked():
        QTimer.singleShot(3000, lambda: check_on_startup(win))
