"""画面（GUI）の拡張：完成動画の区間を書き出す

「確認・仕上げ」タブに「区間を書き出す…」ボタンを足す。押すと完成動画を再生しながら開始・終了を決める画面が開き、
その区間だけを 1本の動画（と字幕）にする。中身の処理は clip.py（rc_clip.py）をそのまま呼び出している。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSlider,
    QVBoxLayout,
)

import rc_clip

TIP = "完成動画を再生しながら開始と終了を決めて、その区間だけを1本の動画に書き出します（SNS用の切り抜きなど）"
FIRST_LEN = 30.0   # はじめて開いたときの区間の長さ（秒）


def _gui(win):
    """gui.py のモジュール（python gui.py で起動したときは __main__）"""
    return sys.modules[type(win).__module__]


def _set_role(w, role: str):
    """色はテーマ（rc_theme）の役割で決める（ダークモードでも読める）"""
    if w.property("role") != role:
        w.setProperty("role", role)
        w.style().unpolish(w)
        w.style().polish(w)


class ClipDialog(QDialog):
    def __init__(self, ext: "ClipExt", video: Path, out_dir: Path):
        super().__init__(ext.win)
        import rc_media
        self.ext, self.win, self.video, self.out_dir = ext, ext.win, video, out_dir
        g = _gui(self.win)
        try:
            self.total = rc_media.probe(video).duration
        except Exception:  # noqa: BLE001  読めなくても、再生部品が長さを教えてくれる
            self.total = 0.0
        self.stop_at = None
        self.made: Path | None = None
        self.setWindowTitle(f"区間を書き出す（{video.name}）")
        self.resize(940, 760)
        lay = QVBoxLayout(self)
        note = QLabel("再生しながら「ここを開始に」「ここを終了に」で区間を決めて、「書き出す」を押します。"
                      "時刻は直接入力もできます（例: 1:23.5）。できた動画は結果フォルダの clips に入ります。")
        note.setWordWrap(True)
        _set_role(note, "sub")
        lay.addWidget(note)

        # プレーヤー
        self.player = None
        if g.HAS_MEDIA:
            self.view = g.QVideoWidget()
            self.view.setMinimumSize(480, 270)
            self.player = g.QMediaPlayer(self)
            self.audio = g.QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.view)
            self.player.positionChanged.connect(self._pos_changed)
            self.player.durationChanged.connect(self._dur_changed)
            self.player.playbackStateChanged.connect(lambda *_: self._update_play_btn())
            self.player.setSource(QUrl.fromLocalFile(str(video)))
            lay.addWidget(self.view, 1)
        else:
            lay.addWidget(QLabel("（この環境では動画の再生部品が使えません。時刻を入力して書き出してください）"))
        self.play_btn = QPushButton("再生")
        self.play_btn.clicked.connect(self._toggle)
        back = QPushButton("◀ 1秒")
        back.clicked.connect(lambda: self._nudge(-1.0))
        fwd = QPushButton("1秒 ▶")
        fwd.clicked.connect(lambda: self._nudge(1.0))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, int(self.total * 1000))
        self.slider.sliderMoved.connect(lambda ms: self.player and self.player.setPosition(ms))
        self.pos_label = QLabel(f"0:00.0 / {rc_clip.fmt_time(self.total)}")
        row = QHBoxLayout()
        for w in (self.play_btn, back, fwd):
            row.addWidget(w)
            w.setEnabled(self.player is not None)
        row.addWidget(self.slider, 1)
        row.addWidget(self.pos_label)
        self.slider.setEnabled(self.player is not None)
        lay.addLayout(row)

        # 区間
        start, end = ext.last.get(str(video), (0.0, min(FIRST_LEN, self.total) if self.total else FIRST_LEN))
        self.start_edit = QLineEdit(rc_clip.fmt_time(start))
        self.end_edit = QLineEdit(rc_clip.fmt_time(end))
        self.b_in = QPushButton("ここを開始に")
        self.b_in.setToolTip("いま再生している位置を、区間の開始にします")
        self.b_in.clicked.connect(self._mark_in)
        self.b_out = QPushButton("ここを終了に")
        self.b_out.setToolTip("いま再生している位置を、区間の終了にします")
        self.b_out.clicked.connect(self._mark_out)
        for b in (self.b_in, self.b_out):
            b.setEnabled(self.player is not None)
        self.b_range = QPushButton("区間を再生")
        self.b_range.clicked.connect(self._play_range)
        self.b_range.setEnabled(self.player is not None)
        self.len_label = QLabel()
        grid = QGridLayout()
        grid.addWidget(QLabel("開始"), 0, 0)
        grid.addWidget(self.start_edit, 0, 1)
        grid.addWidget(self.b_in, 0, 2)
        grid.addWidget(QLabel("終了"), 1, 0)
        grid.addWidget(self.end_edit, 1, 1)
        grid.addWidget(self.b_out, 1, 2)
        grid.addWidget(self.len_label, 2, 1)
        grid.addWidget(self.b_range, 2, 2)
        self.chapter = QComboBox()
        self.chapter.addItem("章から選ぶ…", None)
        for c in rc_clip.chapters(out_dir, self.total or None):
            self.chapter.addItem(f"{rc_clip.fmt_time(c['start'])}〜{rc_clip.fmt_time(c['end'])}  {c['title']}",
                                 (c["start"], c["end"]))
        self.chapter.setEnabled(self.chapter.count() > 1)
        self.chapter.activated.connect(self._pick_chapter)
        grid.addWidget(QLabel("章"), 3, 0)
        grid.addWidget(self.chapter, 3, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.srt_cb = QCheckBox("字幕ファイル（SRT）も作る（YouTube や SNS に字幕を付けるとき用）")
        has_srt = video.with_suffix(".srt").is_file()
        self.srt_cb.setChecked(has_srt)
        self.srt_cb.setEnabled(has_srt)
        lay.addWidget(self.srt_cb)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.b_export = QPushButton("書き出す")
        self.b_export.setProperty("kind", "primary")
        self.b_export.setMinimumHeight(36)
        self.b_export.clicked.connect(self.export)
        self.b_play_made = QPushButton("できた動画を再生")
        self.b_play_made.clicked.connect(lambda: self.made and g.open_path(self.made))
        self.b_play_made.setEnabled(False)
        b_folder = QPushButton("フォルダを開く")
        b_folder.clicked.connect(self._open_folder)
        b_close = QPushButton("閉じる")
        b_close.clicked.connect(self.reject)
        br = QHBoxLayout()
        br.addWidget(self.b_export)
        br.addWidget(self.b_play_made)
        br.addWidget(b_folder)
        br.addStretch()
        br.addWidget(b_close)
        lay.addLayout(br)

        for e in (self.start_edit, self.end_edit):
            e.textChanged.connect(lambda *_: self._refresh())
        self.win.runner.busy_changed.connect(self._busy_changed)
        self._refresh()

    # --- 区間 ---
    def range(self) -> tuple[float, float]:
        """入力された区間。おかしいときは ValueError（理由つき）"""
        s = rc_clip.parse_time(self.start_edit.text())
        e = rc_clip.parse_time(self.end_edit.text())
        if self.total and s >= self.total:
            raise ValueError(f"開始が動画の長さ（{rc_clip.fmt_time(self.total)}）を超えています")
        if self.total:
            e = min(e, self.total)
        if e - s < rc_clip.MIN_SEC:
            raise ValueError(f"終了は開始より {rc_clip.MIN_SEC}秒以上あとにしてください")
        return s, e

    def _refresh(self):
        try:
            busy = self.win.runner.busy
            try:
                s, e = self.range()
            except ValueError as ex:
                self.len_label.setText("")
                self._say(str(ex), "notice")
                self.b_export.setEnabled(False)
                return
            self.len_label.setText(f"長さ {rc_clip.fmt_time(e - s)}")
            if self.status.property("role") == "notice":
                self._say("")
            self.b_export.setEnabled(not busy)
            self.b_export.setToolTip("ほかの処理が終わるまで待ってください" if busy else "")
        except RuntimeError:   # 画面を閉じたあと（部品がもう無い）
            pass

    def _busy_changed(self, *_):
        self._refresh()

    def _say(self, text: str, role: str = "sub"):
        self.status.setText(text)
        _set_role(self.status, role)

    def _now(self) -> float:
        return self.player.position() / 1000 if self.player else 0.0

    def _mark_in(self):
        t = self._now()
        self.start_edit.setText(rc_clip.fmt_time(t))
        try:
            end = rc_clip.parse_time(self.end_edit.text())
        except ValueError:
            end = -1
        if end - t < rc_clip.MIN_SEC:   # 終了が開始より前になったら、とりあえず後ろにずらす
            self.end_edit.setText(rc_clip.fmt_time(min(t + FIRST_LEN, self.total or t + FIRST_LEN)))

    def _mark_out(self):
        self.end_edit.setText(rc_clip.fmt_time(self._now()))

    def _pick_chapter(self, i: int):
        r = self.chapter.itemData(i)
        if not r:
            return
        self.start_edit.setText(rc_clip.fmt_time(r[0]))
        self.end_edit.setText(rc_clip.fmt_time(r[1]))
        if self.player:
            self.player.setPosition(int(r[0] * 1000))

    # --- 再生 ---
    def _toggle(self):
        if not self.player:
            return
        self.stop_at = None
        if self.player.playbackState() == self.player.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _update_play_btn(self):
        if self.player:
            playing = self.player.playbackState() == self.player.PlaybackState.PlayingState
            self.play_btn.setText("一時停止" if playing else "再生")

    def _nudge(self, sec: float):
        if self.player:
            self.player.setPosition(max(0, int((self._now() + sec) * 1000)))

    def _play_range(self):
        try:
            s, e = self.range()
        except ValueError as ex:
            self._say(str(ex), "notice")
            return
        self.player.setPosition(int(s * 1000))
        self.stop_at = e
        self.player.play()

    def _pos_changed(self, ms: int):
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.pos_label.setText(f"{rc_clip.fmt_time(ms / 1000)} / {rc_clip.fmt_time(self.total)}")
        if self.stop_at is not None and ms >= self.stop_at * 1000:
            self.stop_at = None
            self.player.pause()

    def _dur_changed(self, ms: int):
        if ms > 0 and not self.total:
            self.total = ms / 1000
        self.slider.setRange(0, int(max(self.total * 1000, ms)))
        self._refresh()

    # --- 書き出し ---
    def export(self):
        try:
            s, e = self.range()
        except ValueError as ex:
            self._say(str(ex), "notice")
            return
        path = rc_clip.clip_path(self.video, s, e)
        if path.exists() and QMessageBox.question(
                self, "上書きしますか？", f"同じ区間の動画がもうあります。上書きしますか？\n\n{path.name}") != QMessageBox.Yes:
            return
        if self.player:
            self.player.pause()
        self.ext.last[str(self.video)] = (s, e)
        args = [str(self.video), "--start", f"{s:.3f}", "--end", f"{e:.3f}", "--out", str(path),
                "--template", self.win.review._template()]
        if not self.srt_cb.isChecked():
            args.append("--no-srt")
        self._say(f"書き出しています: {rc_clip.fmt_time(s)}〜{rc_clip.fmt_time(e)}（下の帯に進み具合が出ます）")

        def done(ok):
            made = path if ok and path.exists() else None
            try:
                if made:
                    self.made = made
                    self.b_play_made.setEnabled(True)
                    self._say(f"できました: {made.name}（{rc_clip.fmt_time(e - s)}）")
                elif ok:
                    self._say("書き出せませんでした。下の「ログを表示」で確認してください。", "notice")
            except RuntimeError:   # 書き出しの途中で画面を閉じた
                if made:
                    self.win.statusBar().showMessage(f"区間を書き出しました: {made}", 15000)
        self.win.runner.start("clip.py", args, "区間を書き出し中", done)

    def _open_folder(self):
        d = self.out_dir / rc_clip.CLIP_DIR
        _gui(self.win).open_path(d if d.is_dir() else self.out_dir)

    def done(self, r):   # 閉じるとき（「閉じる」・×・Esc のどれでも）
        try:
            s, e = self.range()
            self.ext.last[str(self.video)] = (s, e)
        except ValueError:
            pass
        try:
            self.win.runner.busy_changed.disconnect(self._busy_changed)
        except (RuntimeError, TypeError):
            pass
        if self.player:
            # 動画ファイルを手放す（Windows では開いたままだと、完成動画を書き出し直すときに上書きできない）
            self.player.stop()
            self.player.setSource(QUrl())
        super().done(r)


class ClipExt:
    def __init__(self, win):
        self.win = win
        self.last: dict[str, tuple[float, float]] = {}   # 動画ごとに、前に選んだ区間（ツールを閉じるまで）
        self.dlg = None
        rv = win.review
        self.btn = QPushButton("区間を書き出す…")
        self.btn.setToolTip(TIP)
        self.btn.clicked.connect(self.open)
        rv.ext_bar.insertWidget(rv.ext_bar.count() - 1, self.btn)
        # 「確認・仕上げ」タブのボタンの状態が変わるとき（読み込み・処理の開始と終了）に、こちらも合わせる
        orig = rv._update_buttons

        def update():
            orig()
            self.refresh()
        rv._update_buttons = update
        self.refresh()

    def final(self) -> Path | None:
        rv = self.win.review
        p = rv._final_path() if rv.out else None
        return p if p and p.exists() else None

    def refresh(self):
        ok = self.final() is not None
        self.btn.setEnabled(ok and not self.win.runner.busy)
        self.btn.setToolTip(TIP if ok else TIP + "\n（先に「完成動画を書き出す」を押してください）")

    def open(self):
        rv = self.win.review
        try:
            video, out_dir = rc_clip.final_video(rv.out)
        except (ValueError, TypeError) as e:
            QMessageBox.information(self.win, "区間を書き出す", str(e))
            return
        if rv.player:
            rv.player.pause()
        self.dlg = ClipDialog(self, video, out_dir)
        try:
            self.dlg.exec()
        finally:
            self.dlg = None


def register(win):
    win.clip_ext = ClipExt(win)
