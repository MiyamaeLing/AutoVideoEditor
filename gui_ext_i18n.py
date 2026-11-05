"""画面の拡張：「確認・仕上げ」タブに「英語字幕を作る」ボタンを足す（translate.py を実行する）。

- AIの欄が「Claude APIで自動」なら API で、「claude.ai に貼り付けて使う」なら貼り付け画面で翻訳する
- できあがったら、英語の字幕とタイトルを一覧で確認できる
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

import rc_i18n as I


def _final_srt(out: Path | None) -> Path | None:
    if not out:
        return None
    fi = I.load_json(out / "final_info.json", {})
    p = Path(fi["srt"]) if fi.get("srt") else None
    return p if p and p.exists() else None


class TranslationView(QDialog):
    """翻訳した字幕とタイトルの確認画面"""

    def __init__(self, parent, out: Path):
        super().__init__(parent)
        self.setWindowTitle("翻訳の確認")
        self.resize(860, 620)
        tr = I.load_json(out / I.TRANSLATIONS, {})
        src = _final_srt(out)
        ja = I.read_srt(src) if src else []
        lay = QVBoxLayout(self)
        for code, e in tr.items():
            if code.startswith("_"):
                continue
            name = I.LANGS.get(code, (code,))[0]
            head = QLabel(f"<b>{name}</b>　タイトル：{e.get('title', '（まだ訳していません）')}")
            head.setWordWrap(True)
            lay.addWidget(head)
            if e.get("description"):
                desc = QPlainTextEdit(e["description"])
                desc.setReadOnly(True)
                desc.setMaximumHeight(110)
                lay.addWidget(desc)
            srt = I.srt_path(out, e)
            if not srt or not srt.is_file():
                continue
            cues = I.read_srt(srt, join="\n")
            t = QTableWidget(len(cues), 3)
            t.setHorizontalHeaderLabels(["時刻", "日本語（その時に出ている字幕）", name])
            for r, (s, en_e, txt) in enumerate(cues):
                jtxt = "／".join(x for a, b, x in ja if a < en_e and b > s)
                for c, v in enumerate((f"{int(s // 60):02d}:{s % 60:04.1f}", jtxt, txt)):
                    t.setItem(r, c, QTableWidgetItem(v))
            t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
            t.resizeRowsToContents()
            lay.addWidget(t, 1)
        note = QLabel("YouTube に設定するときは「YouTubeへ」画面で「英語字幕・英語タイトルも設定する」に"
                      "チェックを入れて「② アップした動画に設定を反映」を押してください。")
        note.setWordWrap(True)
        lay.addWidget(note)
        bb = QDialogButtonBox()
        b_open = bb.addButton("フォルダを開く", QDialogButtonBox.ActionRole)
        bb.addButton("閉じる", QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        import gui
        b_open.clicked.connect(lambda: gui.open_path(out))
        lay.addWidget(bb)


def register(win):
    import gui
    rv = win.review
    btn = QPushButton("英語字幕を作る")
    btn.setToolTip("完成動画の字幕を Claude で英語に翻訳して「○○_完成.en.srt」を作ります。"
                   "YouTube の英語タイトル・概要欄もあわせて訳します（作る言語はテンプレートの [translate] で変えられます）")
    rv.ext_bar.insertWidget(0, btn)
    win.i18n_button = btn

    orig_update = rv._update_buttons

    def update_buttons():
        orig_update()
        btn.setEnabled(not win.runner.busy and _final_srt(rv.out) is not None)
        has = rv.out is not None and (rv.out / I.TRANSLATIONS).exists()
        btn.setText("英語字幕を作り直す" if has else "英語字幕を作る")
    rv._update_buttons = update_buttons
    update_buttons()
    # 処理の開始・終了の知らせでも切り替える（ほかのボタンと同じく、処理中は押せない）
    win.runner.busy_changed.connect(lambda busy: btn.setEnabled(not busy and _final_srt(rv.out) is not None))

    def finished(out: Path):
        rv._update_buttons()
        TranslationView(rv, out).exec()

    def run(llm: str):
        out = rv.out
        if not out:
            return
        args = [str(out), "--template", rv._template(), "--llm", llm]
        if llm == "api":
            args += ["--effort", win.create.effort.currentData()]

        def done(ok):
            if not ok:
                rv._update_buttons()
                return
            pending = I.load_json(out / "translate_pending.json", [])
            if not pending:
                finished(out)
                return
            for p in pending:   # claude.ai に貼り付けて訳してもらう
                dlg = gui.ManualDialog(rv, Path(p["prompt"]), f"{p['name']}に翻訳")
                if dlg.exec() != QDialog.Accepted:
                    rv._update_buttons()
                    return
                Path(p["reply"]).write_text(dlg.text(), encoding="utf-8")
            run("manual")   # 返答を読み込んで仕上げる
        win.runner.start("translate.py", args, "英語字幕・英語タイトルを翻訳中", done)

    def clicked():
        llm = win.create.llm.currentData()
        if llm != "manual":
            if gui.has_api_key(win.settings):
                llm = "api"
            elif QMessageBox.question(rv, "APIキーが未設定です",
                                      "Claude の APIキーが設定されていません。\n"
                                      "claude.ai に貼り付けて翻訳しますか？（API料金はかかりません）") == QMessageBox.Yes:
                llm = "manual"
            else:
                return
        run(llm)
    btn.clicked.connect(clicked)
