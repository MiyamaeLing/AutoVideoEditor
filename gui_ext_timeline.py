"""「確認・仕上げ」タブのタイムラインと、画面全体の見た目（ダークモード・文字の大きさ・アイコン・初めての案内）。

gui.py が起動時に自動で読み込みます（register(win)）。

タイムライン:
  - 音の波形の上に「残す／カット／要確認／見どころ／章」を色の帯で表示
  - クリックでその場所を右のプレーヤーで再生し、一覧（表）の行も選ぶ。表の行を選ぶとタイムラインも動く
  - ダブルクリック（または右クリック）で、その発言の「残す／カット」を切り替える
  - マウスのホイールで拡大・縮小、ドラッグや下のスクロールバーで移動
  - 波形は FFmpeg で低い解像度で取り出し、出力フォルダの waveform_cache/ に保存（2回目からは一瞬）
"""
from __future__ import annotations

import bisect
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollBar, QSizePolicy,
    QToolButton, QToolTip, QVBoxLayout, QWidget,
)

import rc_theme
import rc_waveform

RULER_H = 18     # 目盛り
CHAP_H = 18      # 章
MARK_H = 9       # 要確認・見どころ
CANVAS_H = 104
MIN_SPP = 0.005   # いちばん拡大したとき（1ピクセルあたりの秒数）

MK_NONE, MK_REVIEW, MK_HIL = 0, 1, 2

# 押しボタンの文字 → アイコン（gui.py のボタンを書き換えずに、アイコンだけ付ける）
TEXT_ICONS = {
    "追加…": "add", "2台目を追加…": "add", "選択を削除": "trash", "2台目を削除": "trash",
    "APIキーを設定…": "key", "見た目を確認": "eye", "フォルダを開く": "folder", "別の結果を開く…": "open",
    "荒編集する": "scissors", "完成まで一気に作る": "spark", "停止": "stop", "選んだ場面を再生": "play",
}
# 下のボタン列は数が多いので、流れの節目になる4つだけにアイコンを付ける（横幅を広げすぎない）
REVIEW_ICONS = {"b_apply": "scissors", "b_plan": "spark", "b_final": "export", "b_yt": "youtube"}


def _fmt(t: float) -> str:
    t = max(0, t)
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _repolish(w: QWidget):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


# ============================================================
#  波形を別スレッドで読み込む
# ============================================================
class WaveLoader(QObject):
    """素材ごとに波形を作る（またはキャッシュから読む）。画面を止めないように別スレッドで動かす"""

    part = Signal(int, int, object, bool)   # 世代, 素材の番号, 波形, 完成したか
    finished = Signal(int, str)             # 世代, 問題があればその説明

    def __init__(self):
        super().__init__()
        self.gen = 0
        self._stop = threading.Event()

    def start(self, out: Path, files: list[tuple[float, float, str]]):
        self.stop()
        self.gen += 1
        self._stop = threading.Event()
        th = threading.Thread(target=self._run, args=(self.gen, Path(out), list(files), self._stop), daemon=True)
        th.start()

    def stop(self):
        self._stop.set()

    def _run(self, gen, out, files, stop):
        problem = ""
        for i, (_start, dur, path) in enumerate(files):
            if stop.is_set():
                return
            last = [0.0]

            def prog(a, i=i):
                now = time.monotonic()
                if now - last[0] > 0.4:
                    last[0] = now
                    self._emit(self.part, gen, i, a.copy(), False)
            try:
                a = rc_waveform.get(out, Path(path), dur, prog, stop.is_set)
            except FileNotFoundError:
                problem = "FFmpeg が見つからないため、波形は表示できません"
                break
            except Exception as e:  # noqa: BLE001
                problem = f"波形を読み込めませんでした（{e}）"
                continue
            if stop.is_set():
                return
            if a is not None:
                self._emit(self.part, gen, i, a, True)
        self._emit(self.finished, gen, problem)

    @staticmethod
    def _emit(sig, *args):
        try:
            sig.emit(*args)
        except RuntimeError:   # 画面を閉じたあと（部品が消えたあと）に届いた場合
            pass


# ============================================================
#  タイムラインの絵
# ============================================================
class TimelineCanvas(QWidget):
    clicked = Signal(float, int)       # 時刻, 発言の番号（-1 は発言の外）
    toggle_requested = Signal(int)     # 残す/カットを切り替える発言の番号
    view_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(CANVAS_H)
        self.setMaximumHeight(CANVAS_H)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.clear()

    # ---------- データ ----------
    def clear(self):
        self.duration = 0.0
        self.t0 = 0.0
        self.spp = 1.0
        self.starts = np.zeros(0)
        self.ends = np.zeros(0)
        self.texts: list[str] = []
        self.kinds: list[str] = []
        self.cut = np.zeros(0, dtype=bool)
        self.mark = np.zeros(0, dtype=np.int8)
        self.keep: list[tuple[float, float]] = []
        self.keep_s = np.zeros(0)
        self.keep_e = np.zeros(0)
        self.chapters: list[tuple[float, str]] = []
        self.wave = np.zeros(0, dtype=np.uint8)
        self.loaded: list[tuple[float, float]] = []
        self.gain = 1.0
        self.sel: tuple[float, float] | None = None
        self.playhead: float | None = None
        self.hover: float | None = None
        self.message = "荒編集が終わると、ここに音の波形と編集の様子（残す・カット・要確認・見どころ・章）が表示されます"
        self._press = None
        self._dragging = False
        self.update()

    def set_data(self, duration, starts, ends, texts, kinds, cut, mark, keep, chapters):
        self.duration = max(float(duration), 1.0)
        self.starts, self.ends = np.asarray(starts, float), np.asarray(ends, float)
        self.texts, self.kinds = texts, kinds
        self.cut = np.asarray(cut, bool)
        self.mark = np.asarray(mark, np.int8)
        self.set_keep(keep)
        self.chapters = sorted(chapters)
        self.wave = np.zeros(int(math.ceil(self.duration * rc_waveform.RATE)) + rc_waveform.RATE, dtype=np.uint8)
        self.loaded = []
        self.sel = None
        self.playhead = None
        self.message = ""
        self.fit()

    def set_keep(self, keep):
        self.keep = list(keep)
        self.keep_s = np.array([s for s, _ in self.keep], float)
        self.keep_e = np.array([e for _, e in self.keep], float)
        self.update()

    def put_wave(self, start: float, arr: np.ndarray, done: bool):
        i0 = int(round(start * rc_waveform.RATE))
        if i0 >= self.wave.size:
            return
        n = min(arr.size, self.wave.size - i0)
        self.wave[i0:i0 + n] = arr[:n]
        rng = (start, start + n / rc_waveform.RATE)
        self.loaded = [r for r in self.loaded if r[0] != start] + [rng]
        if done or self.gain == 1.0:
            v = self.wave[self.wave > 8]
            hi = float(np.percentile(v, 99.5)) if v.size > 50 else 255.0
            self.gain = 255.0 / max(hi, 60.0)
        self.update()

    # ---------- 表示範囲 ----------
    @property
    def view_len(self) -> float:
        return self.spp * max(self.width(), 1)

    def fit(self):
        self.t0 = 0.0
        self.spp = self.duration / max(self.width(), 100)
        self.view_changed.emit()
        self.update()

    def _clamp(self):
        w = max(self.width(), 1)
        self.spp = min(max(self.spp, MIN_SPP), max(self.duration / w, MIN_SPP))
        self.t0 = min(max(self.t0, 0.0), max(self.duration - self.view_len, 0.0))

    def zoom(self, factor: float, at_x: float | None = None):
        if at_x is None:
            at_x = self.width() / 2
        t = self.x2t(at_x)
        self.spp *= factor
        self._clamp()
        self.t0 = t - at_x * self.spp
        self._clamp()
        self.view_changed.emit()
        self.update()

    def set_start(self, t0: float):
        self.t0 = t0
        self._clamp()
        self.update()

    def ensure_visible(self, s: float, e: float):
        if s < self.t0 or e > self.t0 + self.view_len:
            self.t0 = s - self.view_len * 0.25
            self._clamp()
            self.view_changed.emit()
        self.update()

    def resizeEvent(self, e):
        if self.duration and e.oldSize().width() > 0:
            # 全体表示だったら、幅が変わっても全体表示のまま
            was_fit = abs(self.spp * e.oldSize().width() - self.duration) < 1e-6 * max(self.duration, 1) + self.spp
            if was_fit:
                self.spp = self.duration / max(self.width(), 1)
        self._clamp()
        self.view_changed.emit()
        super().resizeEvent(e)

    def x2t(self, x: float) -> float:
        return self.t0 + x * self.spp

    def t2x(self, t: float) -> float:
        return (t - self.t0) / self.spp

    def chunk_at(self, t: float) -> int:
        i = bisect.bisect_right(self.starts, t) - 1   # numpy 配列でも動く
        if 0 <= i < len(self.ends) and t <= self.ends[i]:
            return int(i)
        return -1

    # ---------- 描画 ----------
    def paintEvent(self, _e):
        tk = rc_theme.tokens()
        p = QPainter(self)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, QColor(tk["tl_bg"]))
        f = QFont(self.font())
        f.setPointSizeF(max(f.pointSizeF() - 1.5, 7))
        p.setFont(f)
        if not self.duration:
            p.setPen(QColor(tk["subtext"]))
            p.drawText(QRectF(12, 0, W - 24, H), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        y_wave = RULER_H + CHAP_H
        h_wave = H - y_wave - MARK_H - 3
        y_mark = y_wave + h_wave + 1
        t1 = self.x2t(W)

        # 目盛り
        p.fillRect(0, 0, W, RULER_H, QColor(tk["tl_ruler"]))
        step = rc_waveform.tick_step(self.spp)
        grid = QPen(QColor(tk["tl_grid"]))
        tick = QPen(QColor(tk["subtext"]))
        t = math.ceil(self.t0 / step) * step
        while t <= t1:
            x = int(self.t2x(t))
            p.setPen(grid)
            p.drawLine(x, RULER_H, x, H)
            p.setPen(tick)
            p.drawLine(x, RULER_H - 5, x, RULER_H - 1)
            p.drawText(x + 3, RULER_H - 5, _fmt(t))
            t += step

        # 残す・カットの帯
        xs = np.arange(W)
        tc = self.t0 + (xs + 0.5) * self.spp
        if self.keep_s.size:
            k = np.searchsorted(self.keep_s, tc, side="right") - 1
            keep_px = (k >= 0) & (tc < self.keep_e[np.clip(k, 0, None)])
        else:
            keep_px = np.ones(W, bool)
        inside = tc < self.duration
        keep_px &= inside
        p.fillRect(0, y_wave, W, h_wave, QColor(tk["tl_cut"]))
        for a, b in _runs(keep_px):
            p.fillRect(a, y_wave, b - a, h_wave, QColor(tk["tl_keep"]))
        out_x = int(max(self.t2x(self.duration), 0))
        if out_x < W:
            p.fillRect(out_x, y_wave, W - out_x, h_wave, QColor(tk["tl_ruler"]))
        # カットの帯には斜線（色が見分けにくい人にも分かるように）
        p.save()
        p.setPen(QPen(QColor(tk["tl_wave_cut"]), 1))
        p.setOpacity(0.35)
        for a, b in _runs(~keep_px & inside):
            if b - a < 3:
                continue
            p.setClipRect(a, y_wave, b - a, h_wave)
            for x in range(a - h_wave, b, 7):
                p.drawLine(x, y_wave + h_wave, x + h_wave, y_wave)
        p.restore()

        # 波形
        mid = y_wave + h_wave / 2
        amp = h_wave / 2 - 2
        pk = rc_waveform.peaks_for_view(self.wave, self.t0, self.spp, W).astype(np.float32)
        hh = np.minimum(pk * self.gain / 255.0, 1.0) * amp
        have = np.zeros(W, bool)
        for s, e in self.loaded:
            have |= (tc >= s) & (tc < e)
        have &= inside
        for color, mask in ((tk["tl_wave"], have & keep_px), (tk["tl_wave_cut"], have & ~keep_px)):
            p.setPen(QPen(QColor(color), 1))
            idx = np.flatnonzero(mask & (hh >= 0.5))
            lines = [(int(x), mid - float(hh[x]), mid + float(hh[x])) for x in idx]
            for x, a, b in lines:
                p.drawLine(QPointF(x + 0.5, a), QPointF(x + 0.5, b))
        if not have.any() and inside.any():
            p.setPen(QColor(tk["subtext"]))
            p.drawText(QRectF(0, y_wave, W, h_wave), Qt.AlignCenter, "音の波形を読み込み中…")

        # 要確認・見どころ
        i0 = int(np.searchsorted(self.ends, self.t0))
        i1 = int(np.searchsorted(self.starts, t1, side="right"))
        cols = {MK_REVIEW: QColor(tk["tl_review"]), MK_HIL: QColor(tk["tl_hil"])}
        for i in range(i0, min(i1, len(self.starts))):
            m = int(self.mark[i])
            if m:
                xa, xb = self.t2x(self.starts[i]), self.t2x(self.ends[i])
                p.fillRect(QRectF(xa, y_mark, max(xb - xa, 3), MARK_H), cols[m])
                p.fillRect(QRectF(xa, y_wave, max(xb - xa, 2), 3), cols[m])

        # 章
        cc = QColor(tk["tl_chapter"])
        for n, (ct, title) in enumerate(self.chapters):
            if ct > t1:
                break
            nxt = self.chapters[n + 1][0] if n + 1 < len(self.chapters) else self.duration
            if nxt < self.t0:
                continue
            x = self.t2x(ct)
            p.setPen(QPen(cc, 1, Qt.DashLine))
            p.drawLine(QPointF(x, RULER_H + 2), QPointF(x, y_wave + h_wave))
            w = max(min(self.t2x(nxt), W) - max(x, 0) - 4, 0)
            if w > 14:
                r = QRectF(max(x, 0), RULER_H + 2, min(w, p.fontMetrics().horizontalAdvance(title) + 12), CHAP_H - 4)
                p.fillRect(r, cc)
                p.setPen(QColor("#FFFFFF"))
                p.drawText(r.adjusted(5, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           p.fontMetrics().elidedText(title, Qt.ElideRight, int(r.width() - 8)))
            else:
                p.fillRect(QRectF(x, RULER_H + 2, 3, CHAP_H - 4), cc)

        # 選んだ発言
        if self.sel:
            xa, xb = self.t2x(self.sel[0]), self.t2x(self.sel[1])
            sc = QColor(tk["tl_select"])
            fill = QColor(sc)
            fill.setAlpha(45)
            r = QRectF(xa, RULER_H + 1, max(xb - xa, 2), H - RULER_H - 2)
            p.fillRect(r, fill)
            p.setPen(QPen(sc, 2))
            p.drawRect(r)
        # マウスの位置
        if self.hover is not None and not self._dragging:
            x = self.t2x(self.hover)
            p.setPen(QPen(QColor(tk["subtext"]), 1, Qt.DotLine))
            p.drawLine(QPointF(x, RULER_H), QPointF(x, H))
        # 再生位置
        if self.playhead is not None and self.t0 <= self.playhead <= t1:
            x = self.t2x(self.playhead)
            p.setPen(QPen(QColor(tk["tl_playhead"]), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, H))
            p.setBrush(QBrush(QColor(tk["tl_playhead"])))
            p.drawPolygon([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 6)])
        p.setPen(QPen(QColor(tk["border"]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, W - 1, H - 1)

    # ---------- 操作 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration:
            self._press = (e.position().x(), self.t0)
            self._dragging = False

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._press and e.buttons() & Qt.LeftButton:
            dx = x - self._press[0]
            if abs(dx) > 4:
                self._dragging = True
                self.setCursor(Qt.ClosedHandCursor)
                self.t0 = self._press[1] - dx * self.spp
                self._clamp()
                self.view_changed.emit()
        self.hover = self.x2t(x) if self.duration else None
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press:
            if not self._dragging:
                t = min(max(self.x2t(e.position().x()), 0.0), self.duration)
                self.clicked.emit(t, self.chunk_at(t))
            self._press = None
            self._dragging = False
            self.unsetCursor()
            self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration:
            i = self.chunk_at(self.x2t(e.position().x()))
            if i >= 0:
                self.toggle_requested.emit(i)

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    def wheelEvent(self, e):
        if not self.duration:
            return
        d = e.angleDelta()
        if d.x() and abs(d.x()) > abs(d.y()) or e.modifiers() & Qt.ShiftModifier:
            amount = d.x() or d.y()
            self.t0 -= amount / 120 * self.view_len * 0.1
            self._clamp()
            self.view_changed.emit()
            self.update()
        elif d.y():
            self.zoom(0.8 ** (d.y() / 120), e.position().x())
        e.accept()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom(0.7)
        elif k == Qt.Key_Minus:
            self.zoom(1 / 0.7)
        elif k in (Qt.Key_Left, Qt.Key_Right):
            self.t0 += (-1 if k == Qt.Key_Left else 1) * self.view_len * 0.15
            self._clamp()
            self.view_changed.emit()
            self.update()
        else:
            super().keyPressEvent(e)

    def event(self, e):
        if e.type() == QEvent.ToolTip and self.duration:
            t = self.x2t(e.position().x() if hasattr(e, "position") else e.pos().x())
            QToolTip.showText(e.globalPos(), self.describe(t), self)
            return True
        return super().event(e)

    def describe(self, t: float) -> str:
        i = self.chunk_at(t)
        lines = [_fmt(t)]
        ch = [c for c in self.chapters if c[0] <= t]
        if ch:
            lines[0] += f"　章「{ch[-1][1]}」"
        if i < 0:
            keep = self.keep_s.size == 0 or any(s <= t < e for s, e in self.keep)
            lines.append("（発言のない所）" + ("" if keep else "　カット"))
            return "\n".join(lines)
        state = "カット" if self.cut[i] else "残す"
        mark = {MK_REVIEW: "　要確認", MK_HIL: "　★見どころ"}.get(int(self.mark[i]), "")
        kind = f"（{self.kinds[i]}）" if i < len(self.kinds) and self.kinds[i] else ""
        lines.append(f"{state}{mark}{kind}")
        text = self.texts[i] if i < len(self.texts) else ""
        lines.append(text if len(text) <= 60 else text[:60] + "…")
        lines.append("クリックで再生／ダブルクリックで残す・カットを切り替え")
        return "\n".join(lines)

    def contextMenuEvent(self, e):
        if not self.duration:
            return
        t = self.x2t(e.pos().x())
        i = self.chunk_at(t)
        m = QMenu(self)
        a_play = m.addAction(rc_theme.icon("play"), "ここから再生")
        a_tog = None
        if i >= 0:
            a_tog = m.addAction(rc_theme.icon("scissors"), "この発言を残す" if self.cut[i] else "この発言をカットする")
        m.addSeparator()
        a_fit = m.addAction(rc_theme.icon("fit"), "全体を表示")
        got = m.exec(e.globalPos())
        if got is a_play:
            self.clicked.emit(t, i)
        elif a_tog is not None and got is a_tog:
            self.toggle_requested.emit(i)
        elif got is a_fit:
            self.fit()


def _runs(mask: np.ndarray):
    """True が続く範囲 [(開始, 終わり)]"""
    if mask.size == 0:
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


# ============================================================
#  タイムラインの枠（見出し・凡例・拡大縮小・スクロール）
# ============================================================
class TimelinePanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setProperty("role", "card")
        self.canvas = TimelineCanvas()
        self.title = QLabel("タイムライン")
        self.title.setStyleSheet("font-weight: bold;")
        self.legend = QLabel()
        self.status = QLabel("")
        self.status.setProperty("role", "hint")
        self.hint = QLabel("クリックで再生・ダブルクリックで残す/カット切り替え・ホイールで拡大縮小")
        self.hint.setProperty("role", "hint")
        self.hint.setToolTip("ドラッグや下のスクロールバーで左右に移動、右クリックでメニューが出ます")
        self.b_out = self._tool("zoomout", "縮小（−）", lambda: self.canvas.zoom(1 / 0.6))
        self.b_in = self._tool("zoomin", "拡大（＋）", lambda: self.canvas.zoom(0.6))
        self.b_fit = self._tool("fit", "全体を表示", self.canvas.fit)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.title)
        head.addSpacing(8)
        head.addWidget(self.legend)
        head.addStretch(1)
        head.addWidget(self.status)
        head.addSpacing(6)
        head.addWidget(self.hint)
        head.addSpacing(6)
        head.addWidget(self.b_out)
        head.addWidget(self.b_in)
        head.addWidget(self.b_fit)
        self.scroll = QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._scrolled)
        self.canvas.view_changed.connect(self._sync_scroll)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 6)
        lay.setSpacing(3)
        lay.addLayout(head)
        lay.addWidget(self.canvas)
        lay.addWidget(self.scroll)
        self.refresh_theme()

    def _tool(self, icon, tip, fn):
        b = QToolButton()
        b.setProperty("icon_name", icon)
        b.setToolTip(tip)
        b.setIconSize(QSize(18, 18))
        b.clicked.connect(fn)
        return b

    def refresh_theme(self):
        tk = rc_theme.tokens()
        for b in (self.b_out, self.b_in, self.b_fit):
            b.setIcon(rc_theme.icon(b.property("icon_name")))

        def chip(color, label, hatch=False):
            border = f"border:1px solid {tk['border_strong']};" if hatch else ""
            return (f"<span style='background:{color};{border}'>&nbsp;&nbsp;&nbsp;</span>"
                    f"<span style='color:{tk['subtext']}'>&nbsp;{label}&nbsp;&nbsp;</span>")
        self.legend.setText(chip(tk["tl_wave"], "残す") + chip(tk["tl_cut"], "カット", True)
                            + chip(tk["tl_review"], "要確認") + chip(tk["tl_hil"], "見どころ")
                            + chip(tk["tl_chapter"], "章"))
        self.canvas.update()

    def _scrolled(self, v):
        self.canvas.set_start(v / 10.0)

    def _sync_scroll(self):
        c = self.canvas
        self.scroll.blockSignals(True)
        page = int(c.view_len * 10)
        self.scroll.setRange(0, max(int((c.duration - c.view_len) * 10), 0))
        self.scroll.setPageStep(max(page, 1))
        self.scroll.setSingleStep(max(page // 10, 1))
        self.scroll.setValue(int(c.t0 * 10))
        self.scroll.blockSignals(False)


# ============================================================
#  「確認・仕上げ」タブとのつなぎ
# ============================================================
class TimelineController(QObject):
    def __init__(self, win, panel: TimelinePanel):
        super().__init__(win)
        self.win = win
        self.rv = win.review
        self.panel = panel
        self.canvas = panel.canvas
        self.loader = WaveLoader()
        self.loader.part.connect(self._wave_part)
        self.loader.finished.connect(self._wave_done)
        self.files: list[tuple[float, float, str]] = []
        self.base_cut: set = set()
        self.id_index: dict = {}
        self._wave_parts: dict[int, float] = {}
        rv = self.rv
        rv.loaded.connect(lambda _out: self.reload())
        rv.table.itemChanged.connect(self._item_changed)
        rv.table.currentCellChanged.connect(lambda r, *_: self._row_selected(r))
        for t in (rv.t_telop, rv.t_zoom, rv.t_bgm):
            t.currentCellChanged.connect(lambda r, *_a, tt=t: self._plan_row_selected(tt, r))
        self.canvas.clicked.connect(self._clicked)
        self.canvas.toggle_requested.connect(self._toggle)
        if getattr(rv, "player", None) is not None:
            rv.player.positionChanged.connect(self._position)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.loader.stop)

    # ---------- 読み込み ----------
    def reload(self):
        rv = self.rv
        if not rv.out or not rv.chunks:
            self.loader.stop()
            self.canvas.clear()
            return
        chunks = rv.chunks
        n = len(chunks)
        sess = rv.sess or {}
        self.files = list(rv.offsets)
        self._src_key = None   # 再生中の素材は調べ直す
        dur = sum(d for _s, d, _p in self.files) if self.files else 0.0
        dur = max(dur, chunks[-1]["end"], max((e for _s, e in sess.get("keep", [])), default=0))
        self.base_cut = set(sess.get("cut_ids", []))
        dec = getattr(rv, "decisions", {}) or {}
        cand = {i for c in dec.get("cuts", []) for i in c.get("ids", [])} - self.base_cut  # 屋外モードのカット候補
        self.id_index = {c["id"]: i for i, c in enumerate(chunks)}
        cut, mark, kinds = [], [], []
        for r in range(n):
            it = rv.table.item(r, 0)
            cut.append(it is not None and it.checkState() == Qt.Unchecked)
            tag = it.data(Qt.UserRole + 1) if it is not None else ""
            cid = chunks[r]["id"]
            mark.append(MK_HIL if tag == "hil" else MK_REVIEW if (tag == "review" or cid in cand) else MK_NONE)
            k = rv.table.item(r, 2)
            kinds.append(k.text() if k is not None else "")
        texts = []
        for r, c in enumerate(chunks):
            it = rv.table.item(r, 4)
            texts.append(it.text() if it is not None else c["text"])
        chapters = []
        for c in sess.get("chapters", []) or []:
            if "src" in c:
                chapters.append((float(c["src"]), str(c.get("title", ""))))
            elif c.get("id") in self.id_index:
                chapters.append((float(chunks[self.id_index[c["id"]]]["start"]), str(c.get("title", ""))))
        self.canvas.set_data(dur, [c["start"] for c in chunks], [c["end"] for c in chunks], texts, kinds,
                             cut, mark, self._keep(cut), chapters)
        self.panel._sync_scroll()
        self._wave_parts = {}
        if self.files:
            self.panel.status.setText("音の波形を読み込み中…")
            self.loader.start(rv.out, self.files)
        else:
            self.panel.status.setText("")
        QTimer.singleShot(0, self._attention)

    def _keep(self, cut) -> list[tuple[float, float]]:
        chunks = self.rv.chunks
        sess = self.rv.sess or {}
        base = sess.get("keep")
        if base is None:
            base = [(0.0, self.canvas.duration or chunks[-1]["end"])]
        cut_now = [(c["start"], c["end"]) for c, x in zip(chunks, cut) if x and c["id"] not in self.base_cut]
        keep_now = [(c["start"], c["end"]) for c, x in zip(chunks, cut) if not x and c["id"] in self.base_cut]
        return rc_waveform.keep_intervals(base, cut_now, keep_now)

    def _wave_part(self, gen, i, arr, done):
        if gen != self.loader.gen or i >= len(self.files):
            return
        start, dur, _p = self.files[i]
        self.canvas.put_wave(start, arr, done)
        total = sum(d for _s, d, _p in self.files) or 1
        got = arr.size / rc_waveform.RATE
        self._wave_parts[i] = dur if done else min(got, dur)
        pct = int(sum(self._wave_parts.values()) / total * 100)
        if pct < 100:
            self.panel.status.setText(f"音の波形を読み込み中… {pct}%")

    def _wave_done(self, gen, problem):
        if gen == self.loader.gen:
            self.panel.status.setText(problem)

    # ---------- 表とのつながり ----------
    def _item_changed(self, it):
        rv = self.rv
        if rv._loading or it.column() != 0 or not self.canvas.duration:
            return
        r = it.row()
        if 0 <= r < self.canvas.cut.size:
            self.canvas.cut[r] = it.checkState() == Qt.Unchecked
            self.canvas.set_keep(self._keep(self.canvas.cut.tolist()))
        self._attention()

    def _attention(self):
        b = self.rv.b_apply
        on = b.text().endswith("●")
        if bool(b.property("attention")) != on:
            b.setProperty("attention", on)
            _repolish(b)

    def _row_selected(self, r: int):
        chunks = self.rv.chunks
        if 0 <= r < len(chunks):
            c = chunks[r]
            self.canvas.sel = (c["start"], c["end"])
            self.canvas.ensure_visible(c["start"], c["end"])

    def _plan_row_selected(self, table, r: int):
        it = table.item(r, 0) if r >= 0 else None
        cid = it.data(Qt.UserRole) if it else None
        if cid in self.id_index:
            self._row_selected(self.id_index[cid])

    def _clicked(self, t: float, i: int):
        rv = self.rv
        if i >= 0:
            c = rv.chunks[i]
            self.canvas.sel = (c["start"], c["end"])
            if not rv.table.isRowHidden(i):
                rv.table.setCurrentCell(i, 1)
                rv.table.scrollToItem(rv.table.item(i, 1))
            end = max(c["end"], t + 3.0)
        else:
            end = t + 5.0
        self.canvas.update()
        self.play_at(t, end)

    def play_at(self, t: float, end: float):
        """その場所から再生する（表の行の再生と違い、少し前からではなく、クリックした所から）"""
        rv = self.rv
        if getattr(rv, "player", None) is None:
            return
        path, local = rv._locate(t)
        if not path:
            return
        from PySide6.QtCore import QUrl
        url = QUrl.fromLocalFile(path)
        if rv.player.source() != url:
            rv.player.setSource(url)
        rv.player.setPosition(int(local * 1000))
        rv.stop_at = (local + (end - t)) * 1000
        rv.player.play()
        rv.timer.start()
        rv.play_info.setText(f"{_fmt(t)} 〜 {_fmt(end)}")
        self.canvas.playhead = t
        self.canvas.update()

    def _toggle(self, i: int):
        it = self.rv.table.item(i, 0)
        if it is not None:
            it.setCheckState(Qt.Checked if it.checkState() == Qt.Unchecked else Qt.Unchecked)

    def _position(self, ms: int):
        rv = self.rv
        if not self.files or rv.player is None:
            return
        src = rv.player.source().toLocalFile()
        if not src:
            return
        # 再生中は何度も呼ばれるので、「どの素材を再生しているか」は素材が変わったときだけ調べる
        if getattr(self, "_src_key", None) != src:
            self._src_key = src
            self._src_start = next((s for s, _d, p in self.files if _same(p, src)), None)
        if self._src_start is not None:
            self.canvas.playhead = self._src_start + ms / 1000.0
            self.canvas.update()


def _same(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


# ============================================================
#  初めて使う人への案内
# ============================================================
GUIDE_STEPS = [
    ("素材を入れる", "動画を下の枠にドラッグ＆ドロップ（分かれたファイルは全部）"),
    ("ジャンルを選ぶ", "選んだジャンルで、カットの仕方が変わります"),
    ("「荒編集する」を押す", "終わると「確認・仕上げ」タブが開きます"),
    ("確認して書き出す", "色の帯をクリックで再生。よければ「完成動画を書き出す」"),
]


class GuideCard(QFrame):
    """作成タブの上に出す、小さな案内（画面の高さをなるべく取らないように2行にまとめる）"""

    def __init__(self, on_done, on_close):
        super().__init__()
        self.setProperty("role", "guide")
        title = QLabel("はじめての方へ：4つのステップで動画ができます")
        title.setStyleSheet("font-weight: bold;")
        note = QLabel("（あとから「表示」メニューの「使い方の案内」でも見られます）")
        note.setProperty("role", "hint")
        self.ok_btn = QPushButton("わかった（次から表示しない）")
        self.ok_btn.setProperty("kind", "primary")
        self.ok_btn.clicked.connect(on_done)
        self.close_btn = QToolButton()
        self.close_btn.setToolTip("今回は閉じる")
        self.close_btn.clicked.connect(on_close)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addWidget(note, 1)
        head.addWidget(self.ok_btn)
        head.addWidget(self.close_btn)
        steps = QHBoxLayout()
        steps.setSpacing(10)
        for i, (name, desc) in enumerate(GUIDE_STEPS):
            num = QLabel(str(i + 1))
            num.setProperty("role", "step")
            num.setAlignment(Qt.AlignCenter)
            txt = QLabel(f"<b>{name}</b>　{desc}")
            txt.setWordWrap(True)
            txt.setProperty("role", "sub")
            steps.addWidget(num, 0, Qt.AlignTop)
            steps.addWidget(txt, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 8)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addLayout(steps)
        self.refresh_theme()

    def refresh_theme(self):
        self.close_btn.setIcon(rc_theme.icon("close"))


# ============================================================
#  まとめ：見た目の設定・メニュー・登録
# ============================================================
class Appearance(QObject):
    """ダークモード・文字の大きさ・アイコン・ボタンの強弱をまとめて扱う"""

    def __init__(self, win, panel: TimelinePanel):
        super().__init__(win)
        self.win = win
        self.G = sys.modules[type(win).__module__]   # gui.py（python gui.py で起動したときは __main__）
        self.panel = panel
        self.guide: GuideCard | None = None
        st = win.settings
        self.mode = str(st.value("ui/theme", "auto"))
        if self.mode not in ("auto", "light", "dark"):
            self.mode = "auto"
        self.font_size = str(st.value("ui/font_size", "normal"))
        if self.font_size not in rc_theme.FONT_SIZES:
            self.font_size = "normal"
        self._restyle()
        self._corner()
        self._menu()
        self._guide()
        self.apply()
        try:   # Windowsのダークモード設定が変わったら、自動のときだけ追いかける
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                lambda *_: self.mode == "auto" and self.apply())
        except Exception:  # noqa: BLE001
            pass

    # ---------- ボタンの強弱・ラベルの役割（gui.py の個別の色指定を、テーマの色に置き換える）----------
    def _restyle(self):
        w, c, rv = self.win, self.win.create, self.win.review
        # 個別の色指定（setStyleSheet）を外した部品。外しても文字の大きさが古いまま残るので、あとで合わせ直す
        self._cleared = [c.btn_full, c.btn_rough, rv.b_final, rv.b_yt, c.mode_desc, rv.summary, rv.folder, c.adv]
        for b, kind in ((c.btn_full, "primary"), (rv.b_final, "primary"), (rv.b_yt, "danger")):
            b.setStyleSheet("")
            b.setProperty("kind", kind)
        for b in (c.btn_rough, c.btn_full):
            b.setStyleSheet("")
            b.setProperty("big", True)
        for lab, role in ((c.mode_desc, "sub"), (rv.summary, "sub"), (rv.folder, "title")):
            lab.setStyleSheet("")
            lab.setProperty("role", role)
        c.adv.setStyleSheet("")
        c.adv.setProperty("role", "link")
        if hasattr(rv, "plan_note"):
            rv.plan_note.setProperty("role", "sub")
        for b in w.findChildren(QPushButton):
            txt = b.text()
            for sym in ("▶ ", "■ "):
                if txt.startswith(sym):
                    b.setText(txt[len(sym):])
        for page in (c, rv):
            lay = page.layout()
            if lay is not None:
                lay.setContentsMargins(12, 10, 12, 8)
                lay.setSpacing(7)
                # 横に並んだボタンの間は詰めておく（ノートPCの狭い画面でも、横にはみ出さないように）
                for i in range(lay.count()):
                    sub = lay.itemAt(i).layout()
                    if sub is not None and sub.spacing() > 6:
                        sub.setSpacing(6)

    def _icons(self):
        tk = rc_theme.tokens()
        rv = self.win.review
        named = {getattr(rv, k): v for k, v in REVIEW_ICONS.items() if hasattr(rv, k)}
        for b in self.win.findChildren(QPushButton):
            name = named.get(b) or TEXT_ICONS.get(b.text())
            if not name:
                continue
            kind = b.property("kind")
            color = tk["on_accent"] if kind in ("primary", "danger") else None
            b.setIcon(rc_theme.icon(name, color))
            b.setIconSize(QSize(20, 20) if b.property("big") else QSize(15, 15))

    def _legend(self):
        rv = self.win.review
        if not hasattr(rv, "legend"):
            return
        tk = rc_theme.tokens()

        def span(color, text):
            return f"<span style='background:{color}; color:{tk['text']}'>&nbsp;{text}&nbsp;</span>"
        rv.legend.setText(f"{span(tk['row_cut'], 'カット')} {span(tk['row_review'], '要確認')} "
                          f"{span(tk['row_hil'], '見どころ')}　「使う」のチェックを外すとカット、付けると残します")

    def _rows(self):
        """表の行の色を、今のテーマの色に合わせる（gui.py の C_CUT などを差し替える）"""
        tk = rc_theme.tokens()
        G = self.G
        for name, key in (("C_CUT", "row_cut"), ("C_REVIEW", "row_review"), ("C_HILITE", "row_hil")):
            if hasattr(G, name):
                setattr(G, name, QColor(tk[key]))
        rv = self.win.review
        was, rv._loading = rv._loading, True
        try:
            for r in range(rv.table.rowCount()):
                if rv.table.item(r, 0) is not None:
                    rv._color_row(r)
        finally:
            rv._loading = was

    # ---------- 切り替え ----------
    def apply(self):
        app = QApplication.instance()
        rc_theme.apply(app, self.mode, self.font_size)
        for wdg in self._cleared:
            wdg.setFont(app.font())
            _repolish(wdg)   # 役割（title など）の文字の大きさ・太さを付け直す
        self._icons()
        self._legend()
        self._rows()
        self.panel.refresh_theme()
        if self.guide:
            self.guide.refresh_theme()
        dark = rc_theme.tokens()["name"] == "dark"
        self.theme_btn.setIcon(rc_theme.icon("sun" if dark else "moon"))
        self.theme_btn.setText("ライト" if dark else "ダーク")
        self.theme_btn.setToolTip("明るい画面にする" if dark else "暗い画面（ダークモード）にする")
        self.help_btn.setIcon(rc_theme.icon("help"))
        for a in self.theme_actions.actions():
            a.setChecked(a.data() == self.mode)
        for a in self.font_actions.actions():
            a.setChecked(a.data() == self.font_size)

    def set_mode(self, mode: str):
        self.mode = mode
        self.win.settings.setValue("ui/theme", mode)
        self.apply()

    def toggle_dark(self):
        self.set_mode("light" if rc_theme.tokens()["name"] == "dark" else "dark")

    def set_font(self, size: str):
        self.font_size = size
        self.win.settings.setValue("ui/font_size", size)
        self.apply()

    # ---------- 右上のボタンとメニュー ----------
    def _corner(self):
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 6, 2)
        lay.setSpacing(4)
        self.theme_btn = QToolButton()
        self.theme_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.theme_btn.clicked.connect(self.toggle_dark)
        self.help_btn = QToolButton()
        self.help_btn.setText("使い方")
        self.help_btn.setToolTip("はじめての方への案内を表示します")
        self.help_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.help_btn.clicked.connect(self.show_guide)
        lay.addWidget(self.theme_btn)
        lay.addWidget(self.help_btn)
        self.win.tabs.setCornerWidget(box, Qt.TopRightCorner)

    def _menu(self):
        mb = self.win.menuBar()
        m = QMenu("表示", mb)
        mb.insertMenu(self.win.help_menu.menuAction(), m)
        sub = m.addMenu("画面の明るさ")
        self.theme_actions = QActionGroup(m)
        for key, label in (("auto", "Windowsの設定に合わせる"), ("light", "ライト（明るい）"), ("dark", "ダーク（暗い）")):
            a = QAction(label, m, checkable=True)
            a.setData(key)
            a.triggered.connect(lambda _c=False, k=key: self.set_mode(k))
            self.theme_actions.addAction(a)
            sub.addAction(a)
        sub = m.addMenu("文字の大きさ")
        self.font_actions = QActionGroup(m)
        for key, label in (("normal", "標準"), ("large", "大きめ"), ("xlarge", "特大")):
            a = QAction(label, m, checkable=True)
            a.setData(key)
            a.triggered.connect(lambda _c=False, k=key: self.set_font(k))
            self.font_actions.addAction(a)
            sub.addAction(a)
        m.addSeparator()
        self.tl_action = QAction("タイムラインを表示", m, checkable=True)
        self.tl_action.setChecked(str(self.win.settings.value("ui/timeline", "1")) != "0")
        self.tl_action.toggled.connect(self._show_timeline)
        self.panel.setVisible(self.tl_action.isChecked())
        m.addAction(self.tl_action)
        m.addAction("使い方の案内", self.show_guide)
        self.view_menu = m

    def _show_timeline(self, on: bool):
        self.panel.setVisible(on)
        self.win.settings.setValue("ui/timeline", "1" if on else "0")

    # ---------- 案内 ----------
    def _guide(self):
        if str(self.win.settings.value("ui/guide_done", "0")) != "1":
            self.show_guide(switch=False)

    def show_guide(self, switch: bool = True):
        c = self.win.create
        if self.guide is None:
            self.guide = GuideCard(self._guide_done, self._guide_close)
            c.layout().insertWidget(0, self.guide)
            if hasattr(self, "theme_btn"):
                self.guide.refresh_theme()
        self.guide.setVisible(True)
        if switch:
            self.win.tabs.setCurrentWidget(c)

    def _guide_close(self):
        if self.guide:
            self.guide.setVisible(False)

    def _guide_done(self):
        self.win.settings.setValue("ui/guide_done", "1")
        self._guide_close()


def register(win):
    rv = win.review
    panel = TimelinePanel()
    lay = rv.layout()
    lay.insertWidget(1, panel)   # 上の「フォルダ」行のすぐ下
    win.timeline = TimelineController(win, panel)
    win.appearance = Appearance(win, panel)
    if rv.out:   # 拡張を読み込む前に結果が開かれていた場合
        win.timeline.reload()
