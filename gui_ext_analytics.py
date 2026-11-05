"""「分析」タブ：公開した動画の視聴維持率・コメント・告知文・チャンネルの傾向・API審査の申請。

中身の処理は analytics.py（rc_analytics.py）を呼び出しています。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

import rc_analytics as A

APP_DIR = Path(__file__).resolve().parent
AUDIT_DOC = APP_DIR / "docs" / "youtube_audit_draft.md"
AUDIT_FORM = "https://support.google.com/youtube/contact/yt_api_form"
TASK_JA = {"analytics": "視聴維持率を分析する", "comments": "コメントを要約する", "promo": "告知文を作る",
           "trends": "チャンネルの傾向をまとめる"}
BTN_CSS = "QPushButton { background: #2B6CB0; color: white; font-weight: bold; padding: 4px 10px; }" \
          " QPushButton:disabled { background: #CBD5E0; color: #F7FAFC; }"


def _ro(text) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    it.setToolTip(str(text))
    return it


class FitImage(QLabel):
    """画像を、欄の大きさに合わせて縦横比のまま縮める（元より大きくはしない）"""

    def __init__(self, height: int):
        super().__init__()
        self._pm = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_image(self, path) -> bool:
        self._pm = QPixmap(str(path)) if path else QPixmap()
        self._fit()
        return not self._pm.isNull()

    def _fit(self):
        if self._pm.isNull():
            self.clear()
            return
        w = min(self.width() - 8, self._pm.width())
        h = min(self.height() - 4, self._pm.height())
        self.setPixmap(self._pm.scaled(max(w, 50), max(h, 30), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


def _wrap_label(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


class AnalyticsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.G = sys.modules[type(win).__module__]   # gui.py（ManualDialog などを使う）
        self.out: Path | None = None
        self.buttons: list[QPushButton] = []

        # ---- 上部：対象の動画と連携 ----
        self.folder = QLabel("「確認・仕上げ」で結果を開くと、その動画を分析できます。")
        self.folder.setStyleSheet("font-weight: bold;")
        b_open = QPushButton("別の結果を開く…")
        b_open.clicked.connect(self._open_other)
        self.vid = QLineEdit()
        self.vid.setPlaceholderText("空なら「YouTubeへ」で設定した動画")
        self.vid.setToolTip("YouTube Studio の動画のURL「studio.youtube.com/video/○○○/edit」の○○○の部分です")
        self.vid.setMinimumWidth(300)
        self.vid.setMaximumWidth(420)
        self.link = QLabel()
        self.b_relink = QPushButton("Googleと連携し直す")
        self.b_relink.setToolTip("視聴維持率を読む許可を追加します。ブラウザが開くので、すべての項目を許可してください")
        self.b_relink.clicked.connect(self._relink)
        top = QGridLayout()
        top.addWidget(self.folder, 0, 0, 1, 3)
        top.addWidget(b_open, 0, 3)
        top.addWidget(QLabel("動画ID"), 1, 0)
        top.addWidget(self.vid, 1, 1)
        top.addWidget(self.link, 1, 2)
        top.addWidget(self.b_relink, 1, 3)
        top.setColumnStretch(2, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._retention_tab(), "① 視聴維持率")
        self.tabs.addTab(self._comments_tab(), "② コメント")
        self.tabs.addTab(self._promo_tab(), "③ 告知文（X・Instagram・ショート）")
        self.tabs.addTab(self._trends_tab(), "④ チャンネルの傾向")
        self.tabs.addTab(self._audit_tab(), "⑤ API審査の申請")

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        win.runner.busy_changed.connect(self._busy)
        self.refresh_link()
        self.load_trends()

    # ================================================================ 各タブ
    def _retention_tab(self) -> QWidget:
        w = QWidget()
        self.b_ret = QPushButton("視聴維持率を分析")
        self.b_ret.setStyleSheet(BTN_CSS)
        self.b_ret.setToolTip("YouTube から視聴維持率を取り、視聴者が減った場面の原因と改善案を AI が考えます")
        self.b_ret.clicked.connect(lambda: self.run_task("analytics", ["--retention"]))
        b_rep = QPushButton("レポートを開く")
        b_rep.clicked.connect(lambda: self._open_file("report.md"))
        row = QHBoxLayout()
        row.addWidget(self.b_ret)
        row.addWidget(b_rep)
        row.addWidget(_wrap_label("公開してから2〜3日たつと、データが出ます。"), 1)
        self.ret_summary = _wrap_label("まだ分析していません。")
        self.ret_summary.setProperty("role", "summary")   # 色はテーマに合わせる（ダークでも読めるように）
        self.chart = FitImage(250)
        legend = QLabel("<span style='color:#E34948'>●</span> 番号＝視聴者がいつもより多く離れた場面　"
                        "<span style='color:#1BAF7A'>●</span> ＋＝見返された場面　（横＝完成動画の時刻・縦＝見ている人の割合）")
        legend.setProperty("role", "sub")
        self.t_scene = QTableWidget(0, 6)
        self.t_scene.setHorizontalHeaderLabels(["", "時刻", "見ている人", "その場面の発言・テロップ", "考えられる原因", "改善案"])
        hh = self.t_scene.horizontalHeader()
        for i in range(3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        for i in (3, 4, 5):
            hh.setSectionResizeMode(i, QHeaderView.Stretch)
        self.t_scene.verticalHeader().setVisible(False)
        self.t_scene.setWordWrap(True)
        self.t_scene.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_scene.currentCellChanged.connect(lambda r, *_: self._show_frame(r))
        self.frame = QLabel("場面を選ぶと\n画面が出ます")
        self.frame.setAlignment(Qt.AlignCenter)
        self.frame.setFixedWidth(250)
        self.frame.setProperty("role", "sub")
        self.frame.setStyleSheet("border: 1px solid palette(mid);")
        mid = QHBoxLayout()
        mid.addWidget(self.t_scene, 1)
        mid.addWidget(self.frame)
        self.hints = _wrap_label("")
        lay = QVBoxLayout(w)
        lay.addLayout(row)
        lay.addWidget(self.ret_summary)
        lay.addWidget(self.chart)
        lay.addWidget(legend)
        lay.addLayout(mid, 1)
        lay.addWidget(self.hints)
        self.buttons.append(self.b_ret)
        self._frames: list[str] = []
        return w

    def _comments_tab(self) -> QWidget:
        w = QWidget()
        self.b_com = QPushButton("コメントを要約して返信案を作る")
        self.b_com.setStyleSheet(BTN_CSS)
        self.b_com.clicked.connect(lambda: self.run_task("comments", ["--comments"]))
        b_rep = QPushButton("まとめを開く")
        b_rep.clicked.connect(lambda: self._open_file("comments.md"))
        row = QHBoxLayout()
        row.addWidget(self.b_com)
        row.addWidget(b_rep)
        row.addStretch()
        self.com_summary = _wrap_label("まだ要約していません。")
        self.com_summary.setProperty("role", "summary")
        self.t_reply = QTableWidget(0, 4)
        self.t_reply.setHorizontalHeaderLabels(["投稿した人", "コメント", "返信の案（ダブルクリックで直せます）", "状態"])
        hh = self.t_reply.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.t_reply.verticalHeader().setVisible(False)
        self.t_reply.setWordWrap(True)
        self.t_reply.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_reply.setEditTriggers(QAbstractItemView.DoubleClicked)
        b_copy = QPushButton("選んだ返信をコピー")
        b_copy.clicked.connect(self._copy_reply)
        self.b_post = QPushButton("選んだ返信を投稿…")
        self.b_post.setToolTip("確認してから、YouTube にこの返信を投稿します")
        self.b_post.clicked.connect(self._post_reply)
        b_studio = QPushButton("Studio のコメント欄を開く")
        b_studio.clicked.connect(self._open_studio_comments)
        self.copied = QLabel("")
        br = QHBoxLayout()
        for b in (b_copy, self.b_post, b_studio):
            br.addWidget(b)
        br.addWidget(self.copied, 1)
        lay = QVBoxLayout(w)
        lay.addLayout(row)
        lay.addWidget(self.com_summary)
        lay.addWidget(self.t_reply, 1)
        lay.addLayout(br)
        lay.addWidget(_wrap_label(f"コメントのデータは、YouTube の規約に合わせて{A.KEEP_DAYS}日たつと自動で消します。"))
        self.buttons += [self.b_com, self.b_post]
        self._replies: list[dict] = []
        return w

    def _promo_tab(self) -> QWidget:
        w = QWidget()
        self.b_promo = QPushButton("告知文を作る")
        self.b_promo.setStyleSheet(BTN_CSS)
        self.b_promo.clicked.connect(lambda: self.run_task("promo", ["--promo"]))
        b_rep = QPushButton("まとめを開く")
        b_rep.clicked.connect(lambda: self._open_file("promo.md"))
        row = QHBoxLayout()
        row.addWidget(self.b_promo)
        row.addWidget(b_rep)
        row.addWidget(_wrap_label("動画IDが分かっていれば、URL も入ります。"), 1)
        self.promo_box = QWidget()
        self.promo_lay = QVBoxLayout(self.promo_box)
        self.promo_lay.addWidget(QLabel("まだ作っていません。"))
        self.promo_lay.addStretch()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.promo_box)
        sc.setFrameShape(QFrame.NoFrame)
        lay = QVBoxLayout(w)
        lay.addLayout(row)
        lay.addWidget(sc, 1)
        self.buttons.append(self.b_promo)
        return w

    def _trends_tab(self) -> QWidget:
        w = QWidget()
        self.b_trend = QPushButton("傾向をまとめ直す")
        self.b_trend.setStyleSheet(BTN_CSS)
        self.b_trend.setToolTip("分析した動画すべてから、くり返し出てくる傾向を AI がまとめます")
        self.b_trend.clicked.connect(lambda: self.run_task("trends", ["--trends"], need_folder=False))
        self.use = QCheckBox("この傾向を、次の荒編集（カット判定）と演出プランの AI に伝える")
        self.use.toggled.connect(self._toggle_use)
        row = QHBoxLayout()
        row.addWidget(self.b_trend)
        row.addWidget(self.use, 1)
        self.trend_summary = _wrap_label("")
        self.trend_summary.setProperty("role", "summary")
        self.trend_hints = _wrap_label("")
        self.t_videos = QTableWidget(0, 5)
        self.t_videos.setHorizontalHeaderLabels(["動画", "ジャンル", "30秒時点", "平均の視聴割合", "分析した日"])
        hh = self.t_videos.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.t_videos.verticalHeader().setVisible(False)
        lay = QVBoxLayout(w)
        lay.addLayout(row)
        lay.addWidget(self.trend_summary)
        lay.addWidget(self.trend_hints)
        lay.addWidget(QLabel("分析した動画"))
        lay.addWidget(self.t_videos, 1)
        self.buttons.append(self.b_trend)
        return w

    def _audit_tab(self) -> QWidget:
        w = QWidget()
        text = _wrap_label(
            "<b>YouTube API の審査（audit）とは</b><br>"
            "Google の審査を通していないプロジェクトから API でアップロードした動画は「非公開」にロックされます。"
            "また、API を1日に使える量にも上限があります。審査を通すと、この制限がなくなります。<br><br>"
            "<b>申請の流れ</b><br>"
            "① 下の「申請の下書きを開く」で、申請フォームに書く内容（英語の文例つき）を確認する<br>"
            "② 自分のチャンネル名・Google Cloud のプロジェクト番号などを書き込む<br>"
            "③ ツールを使っている様子を画面録画する（Windows は「Win + Alt + R」）<br>"
            "④ 「申請フォームを開く」から送信する（返事まで数週間かかることがあります）<br><br>"
            "※ 審査をしなくても、「YouTube Studio にアップ → このツールで設定を反映」の流れなら、今までどおり使えます。")
        text.setTextFormat(Qt.RichText)
        b_doc = QPushButton("申請の下書きを開く")
        b_doc.setStyleSheet(BTN_CSS)
        b_doc.clicked.connect(lambda: self.G.open_path(AUDIT_DOC))
        b_form = QPushButton("申請フォームを開く")
        b_form.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AUDIT_FORM)))
        b_gc = QPushButton("Google Cloud のコンソールを開く")
        b_gc.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/apis/dashboard")))
        row = QHBoxLayout()
        for b in (b_doc, b_form, b_gc):
            row.addWidget(b)
        row.addStretch()
        lay = QVBoxLayout(w)
        lay.addWidget(text)
        lay.addLayout(row)
        lay.addStretch()
        return w

    # ================================================================ 読み込み
    def set_folder(self, out: Path | None):
        self.out = Path(out) if out else None
        if not self.out:
            return
        sess = A.read_json(self.out / "session.json", {}) or {}
        self.folder.setText(f"{sess.get('stem', self.out.name)}（{self.out}）")
        if not self.vid.text().strip():
            self.vid.setText(A.guess_video_id(self.out))
        self.load_results()

    def showEvent(self, e):
        rv = getattr(self.win, "review", None)
        # 「確認・仕上げ」で開いた結果が変わったときだけ、それに合わせる
        # （このタブの「別の結果を開く…」で選んだ動画と動画IDを、タブを切り替えただけで戻さないように）
        if rv is not None and rv.out and Path(rv.out) != getattr(self, "_synced_rv", None):
            self._synced_rv = Path(rv.out)
            if self.out is None or Path(rv.out) != self.out:
                self.vid.clear()
                self.set_folder(rv.out)
        self.refresh_link()
        self.load_trends()
        super().showEvent(e)

    def adir(self) -> Path | None:
        return self.out / "analytics" if self.out else None

    def load_results(self):
        self.load_retention()
        self.load_comments()
        self.load_promo()

    def load_retention(self):
        d = A.read_json(self.adir() / "analysis.json", None) if self.adir() else None
        self.t_scene.setRowCount(0)
        self.chart.set_image(None)
        self._frames = []
        if not d:
            self.ret_summary.setText("まだ分析していません。「視聴維持率を分析」を押してください。")
            self.hints.setText("")
            return
        ai, nums, v = d.get("ai", {}), d.get("numbers", {}), d.get("video", {})
        head = [f"<b>{v.get('title', '')}</b>"]
        if nums.get("views") is not None:
            head.append(f"再生 {int(nums['views']):,}回")
        if nums.get("averageViewPercentage") is not None:
            head.append(f"平均の視聴割合 {float(nums['averageViewPercentage']):.0f}%")
        if nums.get("retention_30s") is not None:
            head.append(f"30秒時点 {nums['retention_30s']:.0%}")
        self.ret_summary.setText("　".join(head) + "<br>" + ai.get("summary", "").replace("\n", "<br>"))
        self.chart.set_image(self.adir() / "retention.png")
        scenes = {s.get("no"): s for s in ai.get("scenes", [])}
        ctxs = d.get("contexts", [])
        self._frames = d.get("frames", [])
        for n, sc in enumerate(d.get("drops", []), 1):
            r = self.t_scene.rowCount()
            self.t_scene.insertRow(r)
            c = ctxs[n - 1] if n <= len(ctxs) else {}
            near = [ln for ln in c.get("lines", []) if ln["t"] >= sc["a"] - 8][:3] or c.get("lines", [])[-2:]
            said = " / ".join(ln["text"] for ln in near)
            if c.get("telops"):
                said += "\nテロップ: " + "、".join(f"「{t['text']}」" for t in c["telops"][-2:])
            s = scenes.get(n, {})
            for col, val in enumerate([n, f"{A.fmt_t(sc['a'])}〜{A.fmt_t(sc['b'])}", f"{sc['from']:.0%}→{sc['to']:.0%}",
                                       said, s.get("cause", ""), s.get("fix", "")]):
                self.t_scene.setItem(r, col, _ro(val))
        self.t_scene.resizeRowsToContents()
        parts = []
        for key, label in (("cut_hints", "次のカット判定に伝えること"), ("plan_hints", "次の演出プランに伝えること"),
                           ("shoot_hints", "撮影・構成の工夫")):
            if ai.get(key):
                parts.append(f"<b>{label}</b>：" + " ／ ".join(ai[key]))
        self.hints.setText("<br>".join(parts))

    def _show_frame(self, r: int):
        p = self._frames[r] if 0 <= r < len(self._frames) else ""
        pm = QPixmap(p) if p else QPixmap()
        if pm.isNull():
            self.frame.setText("（画面はありません）")
        else:
            self.frame.setPixmap(pm.scaledToWidth(246, Qt.SmoothTransformation))

    def load_comments(self):
        d = A.read_json(self.adir() / "comments_ai.json", None) if self.adir() else None
        self.t_reply.setRowCount(0)
        self._replies = []
        if not d:
            self.com_summary.setText("まだ要約していません。")
            return
        parts = [f"<b>コメント {d.get('count', 0)}件</b>　" + d.get("summary", "").replace("\n", "<br>")]
        for key, label in (("praise", "好評"), ("requests", "要望"), ("questions", "質問"), ("concerns", "不満・指摘")):
            if d.get(key):
                parts.append(f"<b>{label}</b>：" + " ／ ".join(d[key][:6]))
        self.com_summary.setText("<br>".join(parts))
        self._replies = d.get("replies", [])
        for rr in self._replies:
            r = self.t_reply.rowCount()
            self.t_reply.insertRow(r)
            self.t_reply.setItem(r, 0, _ro(rr.get("author", "")))
            self.t_reply.setItem(r, 1, _ro(rr.get("text", "")))
            it = QTableWidgetItem(rr.get("reply", ""))
            it.setToolTip(rr.get("why", ""))
            self.t_reply.setItem(r, 2, it)
            self.t_reply.setItem(r, 3, _ro("投稿済み" if rr.get("posted") else ""))
        self.t_reply.resizeRowsToContents()

    def load_promo(self):
        while self.promo_lay.count():
            it = self.promo_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        d = A.read_json(self.adir() / "promo.json", None) if self.adir() else None
        if not d:
            self.promo_lay.addWidget(QLabel("まだ作っていません。"))
            self.promo_lay.addStretch()
            return
        xb = QGroupBox("X（旧Twitter）　※ 日本語は1文字を2として数えます（上限280＝日本語140字）")
        xl = QVBoxLayout(xb)
        for x in d.get("x_posts", []):
            xl.addLayout(self._text_block(x["text"], counter=True))
        ib = QGroupBox("Instagram")
        il = QVBoxLayout(ib)
        il.addLayout(self._text_block(d.get("instagram_text", "")))
        sb = QGroupBox("ショートの説明（タイトル・説明文）")
        sl = QVBoxLayout(sb)
        for s in d.get("shorts", []):
            where = f"本編 {A.fmt_t(s['t'])}「{s.get('label', '')}」から" if s.get("t") is not None else ""
            sl.addWidget(QLabel(f"<b>{s.get('title', '')}</b>　<i>{where}</i>"))
            sl.addLayout(self._text_block(s.get("title", ""), height=34))
            sl.addLayout(self._text_block(s.get("description", ""), height=130))
        for b in (xb, ib, sb):
            self.promo_lay.addWidget(b)
        self.promo_lay.addStretch()

    def _text_block(self, text: str, counter: bool = False, height: int = 92) -> QHBoxLayout:
        ed = QPlainTextEdit(text)
        ed.setFixedHeight(height)
        side = QVBoxLayout()
        b = QPushButton("コピー")
        b.clicked.connect(lambda: (QGuiApplication.clipboard().setText(ed.toPlainText()), b.setText("コピーしました")))
        side.addWidget(b)
        if counter:
            cnt = QLabel()

            def upd():
                n = A.x_length(ed.toPlainText())
                cnt.setText(f"{n}/280")
                cnt.setStyleSheet("color: #C53030; font-weight: bold;" if n > 280 else "")
            ed.textChanged.connect(upd)
            upd()
            side.addWidget(cnt)
        side.addStretch()
        row = QHBoxLayout()
        row.addWidget(ed, 1)
        row.addLayout(side)
        return row

    def load_trends(self):
        d = A.load_insights()
        self.use.blockSignals(True)
        self.use.setChecked(bool(d.get("use_in_prompts", True)))
        self.use.blockSignals(False)
        vids = d.get("videos", {})
        tr = d.get("trends", {}) or {}
        if not vids:
            self.trend_summary.setText("まだ分析した動画がありません。「① 視聴維持率」で分析すると、ここに傾向がたまっていきます。")
        else:
            note = tr.get("summary", "")
            if tr.get("by_ai") and tr.get("count", 0) < len(vids):
                note += f"<br><span style='color:#C05621'>このまとめのあとに分析した動画が{len(vids) - tr.get('count', 0)}本"\
                        "あります。「傾向をまとめ直す」で反映できます。</span>"
            self.trend_summary.setText(note.replace("\n", "<br>"))
        parts = []
        for key, label in (("cut_hints", "カット判定に伝えること"), ("plan_hints", "演出プランに伝えること")):
            hs = tr.get(key) or []
            if hs:
                parts.append(f"<b>{label}</b><br>" + "<br>".join(
                    f"・{h['text']}（{A.MODES_JA.get(h.get('mode'), 'すべて')}）" if isinstance(h, dict) else f"・{h}"
                    for h in hs))
        self.trend_hints.setText("<br>".join(parts))
        self.t_videos.setRowCount(0)
        for vid, v in sorted(vids.items(), key=lambda kv: kv[1].get("analyzed_at", ""), reverse=True):
            n = v.get("numbers", {})
            r = self.t_videos.rowCount()
            self.t_videos.insertRow(r)
            vals = [v.get("title", vid), A.MODES_JA.get(v.get("mode"), "－"),
                    f"{n['retention_30s']:.0%}" if n.get("retention_30s") is not None else "－",
                    f"{float(n['averageViewPercentage']):.0f}%" if n.get("averageViewPercentage") is not None else "－",
                    v.get("analyzed_at", "")[:10]]
            for c, val in enumerate(vals):
                self.t_videos.setItem(r, c, _ro(val))

    def _toggle_use(self, on: bool):
        d = A.load_insights()
        d["use_in_prompts"] = bool(on)
        A.save_insights(d)

    def refresh_link(self):
        st = A.link_state()
        if st == "ok":
            self.link.setText("✔ Googleと連携済み（分析も使えます）")
            self.link.setStyleSheet("color: #2F855A;")
        elif st == "old":
            self.link.setText("⚠ 分析の許可がありません。「Googleと連携し直す」を押してください")
            self.link.setStyleSheet("color: #C05621; font-weight: bold;")
        else:
            self.link.setText("⚠ Googleと未連携（「確認・仕上げ」→「YouTubeへ」で準備してから）")
            self.link.setStyleSheet("color: #C05621;")

    # ================================================================ 実行
    def _busy(self, busy: bool):
        for b in self.buttons:
            b.setEnabled(not busy)
        self.b_relink.setEnabled(not busy)

    def _open_other(self):
        d = QFileDialog.getExistingDirectory(self, "○○_roughcut フォルダを選ぶ", self.win.settings.value("last_dir", ""))
        if d:
            self.vid.clear()
            self.set_folder(Path(d))

    def _open_file(self, name: str):
        p = self.adir() / name if self.adir() else None
        if p and p.exists():
            self.G.open_path(p)
        else:
            QMessageBox.information(self, "まだありません", "先に上のボタンで作ってください。")

    def _llm(self) -> tuple[str, str]:
        c = self.win.create
        return c.llm.currentData() or "api", c.effort.currentData() or "medium"

    def _relink(self):
        import rc_youtube
        if not rc_youtube.client_secret_path().exists():
            QMessageBox.information(self, "準備が必要です", "先に「確認・仕上げ」→「YouTubeへ」→「連携の準備（client_secret.json）…」"
                                    "で、Google Cloud からダウンロードしたファイルを選んでください。")
            return
        self.win.runner.start("analytics.py", ["--login"], "Googleと連携し直しています（ブラウザを確認してください）",
                              lambda ok: self.refresh_link())

    def _base_args(self, need_folder: bool = True) -> list[str] | None:
        if need_folder and not self.out:
            QMessageBox.information(self, "動画を選んでください", "「確認・仕上げ」で結果を開くか、「別の結果を開く…」で"
                                    "○○_roughcut フォルダを選んでください。")
            return None
        args = [str(self.out)] if (self.out and need_folder) else []
        if self.vid.text().strip() and need_folder:
            args += ["--video-id", self.vid.text().strip()]
        return args

    def run_task(self, task: str, flags: list[str], need_folder: bool = True, answer: Path | None = None):
        base = self._base_args(need_folder)
        if base is None:
            return
        llm, effort = self._llm()
        if not self.win.check_api_key(llm):
            return
        if task != "promo" and task != "trends" and not answer and A.link_state() != "ok":
            st = A.link_state()
            if st == "old":
                if QMessageBox.question(self, "連携し直してください",
                                        "視聴維持率やコメントを読むには、Googleアカウントとの連携をやり直して、"
                                        "分析の許可を追加する必要があります。今から連携し直しますか？") == QMessageBox.Yes:
                    self._relink()
            else:
                QMessageBox.information(self, "Googleとの連携が必要です",
                                        "「確認・仕上げ」→「YouTubeへ」で連携の準備をしてから、"
                                        "「Googleと連携し直す」を押してください。")
            return
        args = base + flags + ["--llm", llm, "--effort", effort]
        if answer:
            args += ["--answer", str(answer)]
        wdir = (self.adir() if need_folder else A.config_dir())
        prompt = wdir / f"prompt_{task}.md" if wdir else None
        if prompt and llm == "manual" and not answer:
            prompt.unlink(missing_ok=True)

        def done(ok):
            self.refresh_link()
            if ok and llm == "manual" and not answer and prompt and prompt.exists():
                dlg = self.G.ManualDialog(self, prompt, TASK_JA.get(task, "分析する"))
                if dlg.exec():
                    p = wdir / f"claude_{task}.json"
                    p.write_text(dlg.text(), encoding="utf-8")
                    QTimer.singleShot(0, lambda: self.run_task(task, flags, need_folder, answer=p))
                return
            if self.out:
                vid = A.guess_video_id(self.out)
                if vid and not self.vid.text().strip():
                    self.vid.setText(vid)
                self.load_results()
            self.load_trends()

        def on_error(err: str) -> bool:
            if "連携をやり直す" in err:
                QTimer.singleShot(0, self.refresh_link)
                QTimer.singleShot(0, lambda: QMessageBox.warning(
                    self, "連携し直してください", "分析を使うには、Googleアカウントとの連携をやり直す必要があります。\n"
                    "上の「Googleと連携し直す」を押して、ブラウザですべての項目を許可してください。"))
                return True
            if "動画IDが分かりませんでした" in err:
                QTimer.singleShot(0, lambda: QMessageBox.information(
                    self, "動画IDを入れてください", "この動画の YouTube の動画IDが分かりませんでした。\n"
                    "上の「動画ID」欄に、YouTube Studio の動画のURL「studio.youtube.com/video/○○○/edit」の"
                    "○○○を入れて、もう一度押してください。"))
                self.vid.setFocus()
                return True
            return False
        titles = {"analytics": "視聴維持率を分析中", "comments": "コメントを要約中", "promo": "告知文を作成中",
                  "trends": "チャンネルの傾向をまとめています"}
        self.win.runner.start("analytics.py", args, titles[task], done, handle_error=on_error)

    # ---------------- コメントの返信
    def _selected_reply(self) -> tuple[dict, str] | None:
        r = self.t_reply.currentRow()
        if r < 0 or r >= len(self._replies):
            QMessageBox.information(self, "返信を選んでください", "表から返信の案を1つ選んでください。")
            return None
        return self._replies[r], self.t_reply.item(r, 2).text()

    def _copy_reply(self):
        s = self._selected_reply()
        if s:
            QGuiApplication.clipboard().setText(s[1])
            self.copied.setText("コピーしました")

    def _post_reply(self):
        s = self._selected_reply()
        if not s:
            return
        rr, text = s
        if rr.get("posted") and QMessageBox.question(self, "投稿済みです", "このコメントには、もう返信を投稿しています。"
                                                     "もう一度投稿しますか？") != QMessageBox.Yes:
            return
        if QMessageBox.question(self, "返信を投稿", f"{rr.get('author', '')} さんのコメントに、次の返信を投稿します。\n\n"
                                f"{text}\n\nよろしいですか？（YouTube で公開されます）") != QMessageBox.Yes:
            return
        args = [str(self.out), "--reply", rr["comment_id"], "--text", text]
        if self.vid.text().strip():
            args += ["--video-id", self.vid.text().strip()]
        self.win.runner.start("analytics.py", args, "返信を投稿中", lambda ok: ok and self.load_comments())

    def _open_studio_comments(self):
        vid = self.vid.text().strip() or (A.guess_video_id(self.out) if self.out else "")
        url = f"https://studio.youtube.com/video/{vid}/comments" if vid else "https://studio.youtube.com/"
        QDesktopServices.openUrl(QUrl(url))


def register(win):
    try:
        A.prune_all()   # 取得してから30日たったコメントのデータを消す（YouTube の規約）
    except Exception:  # noqa: BLE001
        pass
    page = AnalyticsPage(win)
    win.analytics = page
    win.tabs.addTab(page, "分析")
    b = QPushButton("公開後の分析")
    b.setToolTip("YouTube に公開した後の視聴維持率・コメント・告知文")

    def go():
        if win.review.out:
            page.vid.clear()
            page.set_folder(win.review.out)
        win.tabs.setCurrentWidget(page)
    b.clicked.connect(go)
    win.review.ext_bar.insertWidget(0, b)
