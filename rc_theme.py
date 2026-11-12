"""画面の見た目（色・余白・文字の大きさ・アイコン）とダークモード。

使い方（gui_ext_timeline.py から呼んでいます）:
    import rc_theme
    rc_theme.apply(app, "dark")          # "light" / "dark" / "auto"（Windowsの設定に合わせる）
    rc_theme.tokens()["accent"]          # 今の色
    rc_theme.icon("play")                # 今の文字色で描いたアイコン

ボタンの強弱は、ボタンに property を付けると変わります:
    b.setProperty("kind", "primary")     # いちばん大事な操作（青）
    b.setProperty("kind", "danger")      # YouTube など（赤）
    b.setProperty("kind", "subtle")      # 控えめ（枠なし）
    b.setProperty("big", True)           # 大きなボタン
ラベルは property "role" = "title" / "sub" / "link" / "hint" / "ok" / "warn" / "error" / "notice"
色は直接書かず、rc_theme.style(部品, kind=..., role=...) で役割を付ける（ライト・ダークのどちらでも合う）:
    rc_theme.style(b, kind="primary", big=True)
    rc_theme.style(label, role="warn")
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap

LIGHT = {
    "name": "light",
    "bg": "#EEF1F5", "surface": "#FFFFFF", "surface2": "#F5F7FA", "input": "#FFFFFF",
    "border": "#D5DBE3", "border_strong": "#B8C2CF",
    "text": "#1A202C", "subtext": "#5A6474", "disabled_text": "#9AA3AF", "disabled_bg": "#E4E8EE",
    "accent": "#2B6CB0", "accent_hover": "#2C5282", "accent_soft": "#D6E6F7", "on_accent": "#FFFFFF",
    "danger": "#C53030", "danger_hover": "#9B2C2C", "ok": "#2F855A", "warn": "#C05621",
    "notice_bg": "#FFF3C4", "notice_text": "#1A202C",
    # 表の行の色（gui.py の C_CUT / C_REVIEW / C_HILITE と同じ）
    "row_cut": "#F3D6D6", "row_review": "#FFF3C4", "row_hil": "#D8F0DA",
    # タイムライン
    "tl_bg": "#FFFFFF", "tl_ruler": "#F5F7FA", "tl_keep": "#FFFFFF", "tl_cut": "#F6DADA",
    "tl_wave": "#2B6CB0", "tl_wave_cut": "#C9A9A9", "tl_review": "#E9B323", "tl_hil": "#38A169",
    "tl_chapter": "#805AD5", "tl_playhead": "#E53E3E", "tl_select": "#2B6CB0", "tl_grid": "#E2E8F0",
}
DARK = {
    "name": "dark",
    "bg": "#16191E", "surface": "#1F2329", "surface2": "#272C33", "input": "#15181C",
    "border": "#363C45", "border_strong": "#4A525E",
    "text": "#E6E9EE", "subtext": "#A0A9B6", "disabled_text": "#6B7380", "disabled_bg": "#2A2F36",
    "accent": "#4C8FD6", "accent_hover": "#63A4EA", "accent_soft": "#22406A", "on_accent": "#FFFFFF",
    "danger": "#D64545", "danger_hover": "#E25C5C", "ok": "#5CC48A", "warn": "#F0A05A",
    "notice_bg": "#4A3F12", "notice_text": "#F7EBC0",
    "row_cut": "#4E2A2D", "row_review": "#4A4116", "row_hil": "#1F4229",
    "tl_bg": "#15181C", "tl_ruler": "#1F2329", "tl_keep": "#15181C", "tl_cut": "#3D2226",
    "tl_wave": "#63B3ED", "tl_wave_cut": "#7D5E61", "tl_review": "#E9B323", "tl_hil": "#48BB78",
    "tl_chapter": "#B794F4", "tl_playhead": "#FC5C5C", "tl_select": "#63B3ED", "tl_grid": "#2A2F36",
}
FONT_SIZES = {"normal": 10, "large": 11, "xlarge": 12}
FONT_FAMILIES = ["Yu Gothic UI", "Meiryo UI"]

_current = dict(LIGHT)


def tokens() -> dict:
    return _current


def system_is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001  古い Qt
        return False


def resolve(mode: str) -> dict:
    if mode == "dark" or (mode == "auto" and system_is_dark()):
        return DARK
    return LIGHT


def palette(t: dict) -> QPalette:
    p = QPalette()
    c = QColor
    for role, key in ((QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "input"),
                      (QPalette.AlternateBase, "surface2"), (QPalette.ToolTipBase, "surface"),
                      (QPalette.ToolTipText, "text"), (QPalette.Text, "text"), (QPalette.Button, "surface2"),
                      (QPalette.ButtonText, "text"), (QPalette.BrightText, "danger"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "on_accent"), (QPalette.Link, "accent"),
                      (QPalette.PlaceholderText, "disabled_text"), (QPalette.Mid, "border"),
                      (QPalette.Dark, "border_strong"), (QPalette.Light, "surface")):
        p.setColor(role, c(t[key]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c(t["disabled_text"]))
    return p


def stylesheet(t: dict) -> str:
    return f"""
QMainWindow, QDialog {{ background: {t['bg']}; }}
QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: 8px; background: {t['surface']}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {t['subtext']}; padding: 7px 18px; margin-right: 2px;
               border: 1px solid transparent; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabBar::tab:hover {{ color: {t['text']}; background: {t['surface2']}; }}
QTabBar::tab:selected {{ color: {t['accent']}; background: {t['surface']}; border: 1px solid {t['border']};
                        border-bottom: 2px solid {t['accent']}; }}
QGroupBox {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 10px;
            margin-top: 13px; padding: 6px 8px 5px 8px; font-weight: bold; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 4px;
                   color: {t['text']}; }}
QPushButton {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border_strong']};
              border-radius: 6px; padding: 4px 7px; min-height: 20px; }}
QPushButton:hover {{ border-color: {t['accent']}; background: {t['surface2']}; }}
QPushButton:pressed {{ background: {t['accent_soft']}; }}
QPushButton:disabled {{ color: {t['disabled_text']}; background: {t['disabled_bg']}; border-color: {t['border']}; }}
QPushButton[kind="primary"] {{ background: {t['accent']}; color: {t['on_accent']}; border: 1px solid {t['accent']};
                              font-weight: bold; }}
QPushButton[kind="primary"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton[kind="danger"] {{ background: {t['danger']}; color: #FFFFFF; border: 1px solid {t['danger']};
                             font-weight: bold; }}
QPushButton[kind="danger"]:hover {{ background: {t['danger_hover']}; border-color: {t['danger_hover']}; }}
QPushButton[kind="primary"]:disabled, QPushButton[kind="danger"]:disabled {{
    background: {t['disabled_bg']}; color: {t['disabled_text']}; border-color: {t['border']}; }}
QPushButton[kind="subtle"] {{ background: transparent; border: 1px solid transparent; color: {t['accent']}; }}
QPushButton[kind="subtle"]:hover {{ background: {t['accent_soft']}; }}
QPushButton[kind="subtle"]:disabled {{ background: transparent; color: {t['disabled_text']}; }}
QPushButton[big="true"] {{ font-size: 15px; font-weight: bold; padding: 6px 18px; border-radius: 8px; min-height: 28px; }}
QPushButton[attention="true"] {{ border: 2px solid {t['warn']}; font-weight: bold; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 3px 6px; color: {t['text']}; }}
QToolButton:hover {{ background: {t['surface2']}; border-color: {t['border']}; }}
QToolButton:checked {{ background: {t['accent_soft']}; }}
QToolButton[role="link"] {{ color: {t['accent']}; }}
QToolButton[role="menu"] {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border_strong']};
                           border-radius: 6px; padding: 4px 7px; min-height: 20px; }}
QToolButton[role="menu"]:hover {{ border-color: {t['accent']}; background: {t['surface2']}; }}
QToolButton[role="menu"]:disabled {{ color: {t['disabled_text']}; background: {t['disabled_bg']};
                                    border-color: {t['border']}; }}
QToolButton[role="menu"]::menu-indicator {{ subcontrol-position: right center; right: 5px; }}
QToolButton[role="tile"] {{ border: 3px solid transparent; border-radius: 8px; padding: 2px; }}
QToolButton[role="tile"]:hover {{ background: {t['surface2']}; border-color: {t['border']}; }}
QToolButton[role="tile"]:checked {{ border: 3px solid {t['accent']}; background: {t['accent_soft']}; font-weight: bold; }}
QLineEdit, QPlainTextEdit, QListWidget {{ background: {t['input']}; color: {t['text']};
    border: 1px solid {t['border_strong']}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus {{ border-color: {t['accent']}; }}
QTableWidget {{ background: {t['input']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 6px;
               gridline-color: {t['border']}; selection-background-color: {t['accent_soft']};
               selection-color: {t['text']}; }}
QHeaderView::section {{ background: {t['surface2']}; color: {t['subtext']}; padding: 5px 6px; border: none;
                       border-bottom: 1px solid {t['border']}; border-right: 1px solid {t['border']};
                       font-weight: bold; }}
QProgressBar {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 6px;
               text-align: center; color: {t['text']}; min-height: 16px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 5px; }}
QMenuBar {{ background: {t['bg']}; color: {t['text']}; }}
QMenuBar::item:selected {{ background: {t['surface2']}; }}
QMenu {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border_strong']}; padding: 4px; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {t['accent_soft']}; }}
QStatusBar {{ color: {t['subtext']}; }}
QLabel[role="title"] {{ font-size: 15px; font-weight: bold; }}
QLabel[role="sub"] {{ color: {t['subtext']}; }}
QLabel[role="hint"] {{ color: {t['subtext']}; font-size: 11px; }}
QLabel[role="group"] {{ color: {t['subtext']}; font-size: 11px; font-weight: bold; }}
QLabel[role="ok"] {{ color: {t['ok']}; }}
QLabel[role="warn"] {{ color: {t['warn']}; }}
QLabel[role="error"] {{ color: {t['danger']}; font-weight: bold; }}
QLabel[role="strong"] {{ font-weight: bold; }}
QFrame[role="vsep"] {{ color: {t['border']}; }}
QLabel[role="summary"] {{ background: {t['surface2']}; color: {t['text']}; border: 1px solid {t['border']};
                        border-radius: 6px; padding: 6px; }}
QLabel[role="notice"] {{ background: {t['notice_bg']}; color: {t['notice_text']}; border-radius: 6px; padding: 6px; }}
QFrame[role="card"] {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame[role="guide"] {{ background: {t['accent_soft']}; border: 1px solid {t['accent']}; border-radius: 10px; }}
QFrame[role="guide"] QLabel {{ background: transparent; }}
QLabel[role="step"], QFrame[role="guide"] QLabel[role="step"] {{ background: {t['accent']}; color: {t['on_accent']}; border-radius: 12px; font-weight: bold;
                      min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; }}
"""


def apply(app, mode: str = "auto", font_size: str = "normal") -> dict:
    """アプリ全体に見た目を設定する。戻り値は使った色の一覧"""
    global _current
    t = resolve(mode)
    _current = dict(t)
    app.setStyle("Fusion")
    f = app.font()
    f.setFamilies([*FONT_FAMILIES, *[x for x in f.families() if x not in FONT_FAMILIES]])
    f.setPointSize(FONT_SIZES.get(font_size, 10))
    app.setFont(f)
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t))
    for fn in list(_hooks):   # 拡張の画面の、自分で色を付けた所（表の行など）を描き直す
        try:
            fn()
        except RuntimeError:   # 閉じた画面（Qt の部品が消えている）
            _hooks.remove(fn)
        except Exception:  # noqa: BLE001  1つの描き直しの失敗で、テーマの切り替えを止めない
            pass
    return _current


def style(w, kind: str | None = None, role: str | None = None, big: bool | None = None):
    """部品の見た目を役割で決める（個別の色指定は外す）。あとから役割を変えても、すぐ見た目に反映する"""
    w.setStyleSheet("")
    for name, v in (("kind", kind), ("role", role), ("big", big)):
        if v is not None:
            w.setProperty(name, v)
    st = w.style()
    st.unpolish(w)
    st.polish(w)
    return w


_hooks: list = []


def on_change(fn) -> None:
    """テーマ（ライト／ダーク）が変わったときに呼ぶ関数を登録する"""
    _hooks.append(fn)


def row_colors() -> dict:
    """表の行の色（今のテーマ）。bad=赤み / warn=黄色 / ok=緑 / fg=その上の文字の色"""
    t = _current
    return {"bad": QColor(t["row_cut"]), "warn": QColor(t["row_review"]), "ok": QColor(t["row_hil"]),
            "fg": QColor(t["text"])}


# ------------------------------------------------------------ アイコン（線で描いたシンプルなもの）
_ICONS = {
    "play": '<path d="M7 4.5v15l12-7.5z" fill="C" stroke="none"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="C" stroke="none"/>',
    "folder": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2h8.5A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>',
    "open": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2h7.5A1.5 1.5 0 0 1 20 9.5V11"/>'
            '<path d="M3 18l2.6-6.2A1.5 1.5 0 0 1 7 11h13.5l-2.7 7H3z"/>',
    "scissors": '<circle cx="6" cy="6.5" r="2.6"/><circle cx="6" cy="17.5" r="2.6"/>'
                '<path d="M8.2 8L20 19M8.2 16L20 5"/>',
    "doc": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
           '<path d="M9 13h6M9 17h4"/>',
    "spark": '<path d="M11 3l1.8 4.9L17.7 9.7l-4.9 1.8L11 16.4 9.2 11.5 4.3 9.7l4.9-1.8z"/>'
             '<path d="M18 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "export": '<path d="M12 3.5v11M7.5 10l4.5 4.5 4.5-4.5"/><path d="M4.5 16.5v2A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5v-2"/>',
    "image": '<rect x="3" y="4.5" width="18" height="15" rx="2"/><circle cx="8.5" cy="9.5" r="1.6"/>'
             '<path d="M20.5 15.5l-4.5-4.5-8.5 8.5"/>',
    "youtube": '<rect x="2.5" y="5.5" width="19" height="13" rx="3.5"/><path d="M10 9v6l5-3z" fill="C"/>',
    "playcircle": '<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l5.5-3.5z" fill="C"/>',
    "moon": '<path d="M20.5 14.2A8.5 8.5 0 1 1 9.8 3.5a6.8 6.8 0 0 0 10.7 10.7z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4'
           'M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.3 9.3a2.8 2.8 0 0 1 5.4 1c0 1.9-2.7 2.4-2.7 4"/>'
            '<circle cx="12" cy="17.3" r=".6" fill="C"/>',
    "zoomin": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 10.5h6M10.5 7.5v6"/>',
    "zoomout": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 10.5h6"/>',
    "fit": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "add": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l2.5 2.5M18.5 4.5L21 7"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "wave": '<path d="M3 12h2M7 8v8M11 5v14M15 9v6M19 7v10M21 12h0"/>',
}


def icon(name: str, color: str | None = None, size: int = 48) -> QIcon:
    """名前のアイコンを、指定の色（省略時は今の文字色）で作る"""
    body = _ICONS.get(name)
    if not body:
        return QIcon()
    col = color or _current["text"]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="C" stroke-width="1.9" '
           'stroke-linecap="round" stroke-linejoin="round">' + body + "</svg>").replace('"C"', f'"{col}"')
    try:
        from PySide6.QtSvg import QSvgRenderer
    except Exception:  # noqa: BLE001  QtSvg がない環境ではアイコンなし
        return QIcon()
    r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, size, size))
    p.end()
    ic = QIcon(pm)
    # 押せないときは薄い色のアイコンにする
    dis = QPixmap(size, size)
    dis.fill(Qt.transparent)
    p = QPainter(dis)
    p.setOpacity(0.35)
    p.drawPixmap(0, 0, pm)
    p.end()
    ic.addPixmap(dis, QIcon.Disabled)
    return ic
