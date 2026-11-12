"""「まとめて処理」タブ：複数の動画を順番待ちに入れて、寝ている間に1本ずつ処理する。

- 「作成」タブの「順番待ちに入れる ▾」で、今の素材と設定のまま順番待ちに追加（どこまで作るかを選ぶ）
- このタブに動画をドラッグ＆ドロップすると、1本ずつ別の動画として追加（設定は「作成」タブのもの）
- 中身の処理は batch.py（コマンドでも同じことができます）
"""
from __future__ import annotations

import re
from pathlib import Path

import rc_theme
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

import rc_ops
from rc_ops_queue import GOALS, MODES_JA, Queue, fmt_sec, item_name, lock_path, status_text

COLORS = {"done": "#D8F0DA", "error": "#F3D6D6", "running": "#FFF3C4"}
VIDEO_SUFFIX = {".mp4", ".mov", ".mts", ".m2ts", ".mkv", ".avi"}


def _open(p: Path):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


class QueuePage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.q = Queue()
        self.setAcceptDrops(True)

        info = QLabel("動画を順番待ちに入れて、まとめて処理します。<b>「順番に処理を始める」を押してから寝れば、朝には完成しています。</b><br>"
                      "処理中はPCがスリープしません（ノートPCは電源につないでおいてください）。途中で止まっても、"
                      "もう一度押せば続きから進みます。<br>"
                      "<i>追加するときの設定（ジャンル・AI・テンプレート・固有名詞など）は「作成」タブのものを使います。"
                      "まとめて処理では「claude.ai に貼り付けて使う」は使えません。</i>")
        info.setWordWrap(True)

        self.goal = QComboBox()
        for k, v in GOALS.items():
            self.goal.addItem(v, k)
        self.goal.setCurrentIndex(1)
        b_from_create = QPushButton("「作成」タブの素材を追加")
        b_from_create.setToolTip("「作成」タブに入れた素材（分割ファイル・2台目カメラも）を、1本の動画として追加します")
        b_from_create.clicked.connect(lambda: self.add_from_create(self.goal.currentData()))
        b_files = QPushButton("動画を追加…")
        b_files.setToolTip("選んだ動画を、1本ずつ別の動画として追加します")
        b_files.clicked.connect(self._pick)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("どこまで作るか"))
        row1.addWidget(self.goal)
        row1.addWidget(b_from_create)
        row1.addWidget(b_files)
        row1.addStretch()

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["動画", "どこまで", "ジャンル", "状態", "かかった時間", "結果・メッセージ"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda _i: self.open_result())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.empty = QLabel("ここに動画をドラッグ＆ドロップすると、順番待ちに入ります")
        self.empty.setAlignment(Qt.AlignCenter)
        rc_theme.style(self.empty, role="sub")
        self.empty.setContentsMargins(20, 20, 20, 20)

        self.b_up = QPushButton("↑ 上へ")
        self.b_down = QPushButton("↓ 下へ")
        self.b_retry = QPushButton("もう一度やる")
        self.b_remove = QPushButton("一覧から消す")
        self.b_clear = QPushButton("終わったものを片付ける")
        self.b_open = QPushButton("結果を開く（確認・仕上げ）")
        self.b_folder = QPushButton("フォルダを開く")
        self.b_play = QPushButton("完成動画を再生")
        self.b_up.clicked.connect(lambda: self._move(-1))
        self.b_down.clicked.connect(lambda: self._move(1))
        self.b_retry.clicked.connect(self._retry)
        self.b_remove.clicked.connect(self._remove)
        self.b_clear.clicked.connect(lambda: (self.q.clear_done(), self.refresh()))
        self.b_open.clicked.connect(self.open_result)
        self.b_folder.clicked.connect(lambda: (it := self.selected()) and _open(Path(it["out"])))
        self.b_play.clicked.connect(self._play)
        row2 = QHBoxLayout()
        for b in (self.b_up, self.b_down, self.b_retry, self.b_remove, self.b_clear):
            row2.addWidget(b)
        row2.addStretch()
        for b in (self.b_open, self.b_folder, self.b_play):
            row2.addWidget(b)

        self.shutdown = QCheckBox("全部終わったらPCの電源を切る")
        self.shutdown.setToolTip("最後の動画が終わった2分後に電源を切ります（取り消すときは Windowsキー＋R で「shutdown /a」）")
        self.b_run = QPushButton("順番に処理を始める")
        self.b_run.setMinimumHeight(44)
        rc_theme.style(self.b_run, kind="primary", big=True)
        self.b_run.clicked.connect(self.run)
        self.count = QLabel("")
        row3 = QHBoxLayout()
        row3.addWidget(self.count, 1)
        row3.addWidget(self.shutdown)
        row3.addWidget(self.b_run, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(row1)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.empty)
        lay.addLayout(row2)
        lay.addLayout(row3)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.running = False
        win.runner.busy_changed.connect(self._busy)
        self.refresh()

    # --- 追加 ---
    def _settings(self) -> dict:
        c = self.win.create
        words = [w.strip() for w in re.split(r"[、,，\n]", c.glossary.text()) if w.strip()]
        base = c.out_base()   # 「作成」タブの保存先（None なら素材と同じ場所）
        return dict(mode=c.mode(), llm=c.llm.currentData(), template=c.template.currentData(), glossary=words,
                    effort=c.effort.currentData(), caption_chars=c.cap_chars.value(), device=c.device.currentData(),
                    out_base=str(base) if base else None)

    def _check_llm(self, llm) -> bool:
        if llm == "manual":
            QMessageBox.information(self, "AIの設定を変えてください",
                                    "まとめて処理では「claude.ai に貼り付けて使う」は使えません（貼り付ける人がいないため）。\n"
                                    "「作成」タブのAIの欄で「Claude APIで自動」か「AIを使わない」を選んでから、もう一度追加してください。")
            return False
        return True

    def add_files(self, paths: list[Path], goal: str | None = None) -> int:
        st = self._settings()
        if not self._check_llm(st["llm"]) or not self.win.create.check_out_base():
            return 0
        n = 0
        for p in paths:
            try:
                self.q.add([str(p)], goal=goal or self.goal.currentData(), **st)
                n += 1
            except ValueError as e:
                QMessageBox.warning(self, "追加できません", str(e))
        self.refresh()
        return n

    def add_from_create(self, goal: str) -> bool:
        c = self.win.create
        if not c.files.count():
            QMessageBox.information(self, "素材がありません", "「作成」タブに素材の動画を入れてから押してください。")
            return False
        st = self._settings()
        if not self._check_llm(st["llm"]) or not c.check_out_base():
            return False
        try:
            it = self.q.add([str(p) for p in c.files.paths()], goal=goal, cam2=[str(p) for p in c.cam2.paths()], **st)
        except ValueError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return False
        c.save_settings()
        c.files.clear()   # 次の動画をすぐ入れられるように
        c.cam2.clear()
        c.files.viewport().update()
        c.cam2.viewport().update()
        self.refresh()
        self.win.statusBar().showMessage(
            f"順番待ちに入れました：{item_name(it)}（{GOALS[goal]}）。「まとめて処理」タブで始められます。", 10000)
        return True

    def _pick(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "動画を選ぶ（1本ずつ別の動画として追加）",
                                             self.win.settings.value("last_dir", ""),
                                             "動画ファイル (*.mp4 *.MP4 *.mov *.MOV *.mts *.MTS *.m2ts *.mkv *.avi)")
        if fs:
            self.add_files([Path(f) for f in fs])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = sorted((Path(u.toLocalFile()) for u in e.mimeData().urls()), key=lambda p: p.name)
        vids = [p for p in paths if p.suffix.lower() in VIDEO_SUFFIX]
        if vids:
            self.add_files(vids)

    # --- 一覧 ---
    def batch_running(self) -> bool:
        return self.running or rc_ops.is_locked(lock_path(self.q.path))

    def refresh(self):
        items = self.q.load()
        sel = self.selected()
        running = self.batch_running()
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            res = it.get("result") or {}
            if it.get("status") == "error":
                msg = (it.get("message") or "").replace("[エラー]", "").strip().splitlines()
                msg = msg[0] if msg else "エラー"
            elif res.get("video"):
                msg = f"完成 {Path(res['video']).name}（{fmt_sec(res.get('final_sec'))}）"
                if res.get("thumbnails"):
                    msg += f"・サムネイル案 {res['thumbnails']}枚"
            elif res.get("xml"):
                msg = f"Premiere用 {Path(res['xml']).name}"
            else:
                msg = ""
            if res.get("cost_usd"):
                msg += f"（Claude 約${res['cost_usd']:.2f}）"
            if res.get("title") and it.get("status") == "done":
                msg += f"　タイトル案：{res['title']}"
            cells = [item_name(it), GOALS.get(it["goal"], it["goal"]), MODES_JA.get(it["mode"], it["mode"]),
                     status_text(it, running), fmt_sec(it.get("elapsed")) if it.get("elapsed") else "", msg]
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, it["id"])
                if c == 5 and it.get("message"):
                    cell.setToolTip(it["message"])
                col = COLORS.get(it.get("status"))
                if col and c == 3:
                    cell.setBackground(QBrush(QColor(col)))
                self.table.setItem(r, c, cell)
            if sel and it["id"] == sel["id"]:
                self.table.selectRow(r)
        self.empty.setVisible(not items)
        waiting = sum(it["status"] in ("waiting", "running") for it in items)
        done = sum(it["status"] == "done" for it in items)
        err = sum(it["status"] == "error" for it in items)
        self.count.setText(f"順番待ち {waiting}本 / 完了 {done}本" + (f" / エラー {err}本" if err else ""))
        self._update_buttons()

    def selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        return self.q.get(it.data(Qt.UserRole)) if it else None

    def _update_buttons(self):
        it = self.selected()
        busy = self.win.runner.busy
        has = it is not None
        self.b_run.setEnabled(not busy)
        for b in (self.b_up, self.b_down, self.b_remove):
            b.setEnabled(has and not (self.running and it.get("status") == "running"))
        self.b_clear.setEnabled(not self.running)
        self.b_retry.setEnabled(has and it.get("status") in ("done", "error"))
        out = Path(it["out"]) if has else None
        self.b_open.setEnabled(bool(out and (out / "session.json").exists()))
        self.b_folder.setEnabled(bool(out and out.exists()))
        self.b_play.setEnabled(bool(has and (it.get("result") or {}).get("video")
                                    and Path(it["result"]["video"]).exists()))
        self.b_run.setText("処理中…（下の「中止」で止められます）" if self.running else "順番に処理を始める")

    def _move(self, d):
        it = self.selected()
        if it:
            self.q.move(it["id"], d)
            self.refresh()

    def _retry(self):
        it = self.selected()
        if not it:
            return
        from_start = False
        if it.get("status") == "done" or it.get("steps_done"):
            r = QMessageBox.question(self, "もう一度やる",
                                     "荒編集（文字起こし・AIの判定）からやり直しますか？\n"
                                     "「いいえ」なら、終わっている工程は飛ばして続きから進めます。",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            from_start = r == QMessageBox.Yes
        self.q.retry(it["id"], from_start)
        self.refresh()

    def _remove(self):
        it = self.selected()
        if it:
            self.q.remove([it["id"]])
            self.refresh()

    def open_result(self):
        it = self.selected()
        if not it or not (Path(it["out"]) / "session.json").exists():
            return
        if self.win.review.load(Path(it["out"])) is False:   # 処理中で開けなかった
            return
        self.win.tabs.setCurrentWidget(self.win.review)

    def _play(self):
        it = self.selected()
        v = (it or {}).get("result", {}).get("video")
        if v:
            _open(Path(v))

    # --- 実行 ---
    def run(self) -> bool:
        pend = self.q.pending()
        if not pend:
            QMessageBox.information(self, "順番待ちがありません", "先に動画を追加してください。")
            return False
        if any(it["llm"] == "api" for it in pend) and not self.win.check_api_key(
                "api", "順番待ちには、追加したときのAIの設定（Claude APIで自動）が残っています。APIキーを使わないときは、"
                "その動画を一覧から削除して、「作成」タブのAIの欄を「AIを使わない」にしてから追加し直してください。"):
            return False
        args = ["--queue", str(self.q.path), "run"] + (["--shutdown"] if self.shutdown.isChecked() else [])

        def done(ok):
            self.running = False
            self.timer.stop()
            self.refresh()
            items = self.q.load()
            ng = [it for it in items if it["status"] == "error"]
            fin = [it for it in items if it["status"] == "done"]
            if ok:
                body = f"まとめて処理が終わりました。\n完了 {len(fin)}本" + (f" / エラー {len(ng)}本" if ng else "")
                if ng:
                    body += "\n\nエラーになった動画は、一覧で選んで「もう一度やる」を押すと続きから進めます。"
                QMessageBox.information(self, "まとめて処理", body)
        if self.win.runner.start("batch.py", args, "まとめて処理を実行中", done):
            self.running = True
            self.timer.start()
            self.refresh()
            return True
        return False

    def _busy(self, _busy: bool):
        self._update_buttons()


def register(win):
    page = QueuePage(win)
    win.queue_page = page
    win.tabs.addTab(page, "まとめて処理")
    # 「作成」タブの実行ボタンの横に「順番待ちに入れる ▾」を足す
    b = QToolButton()
    b.setText("順番待ちに入れる")   # ▾ はメニューの矢印が自動で付く（2つ出ないように）
    b.setToolTip("今の素材と設定のまま「まとめて処理」の順番待ちに入れます（寝ている間に処理できます）")
    b.setMinimumHeight(44)
    b.setPopupMode(QToolButton.InstantPopup)
    m = QMenu(b)
    for k, v in GOALS.items():
        act = m.addAction(v)
        act.triggered.connect(lambda _=False, k=k: page.add_from_create(k))
    b.setMenu(m)
    win.create_queue_btn = b
    lay = win.create.layout()
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if sub is not None and sub.indexOf(win.create.btn_full) >= 0:
            sub.addWidget(b)
            break
    else:
        lay.addWidget(b)
