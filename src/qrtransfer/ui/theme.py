"""アプリ全体の見た目（色・角丸・余白・フォント）。Windows のライト／ダーク設定に合わせて切り替える。

各画面は部品に役割を付けるだけにして、見た目はここでまとめて決める。
    QPushButton   variant = "primary"（主な操作）/ "ghost"（戻るなど目立たせない操作）/ "tile"（起動画面の大ボタン）
    QFrame        role = "card"（白い面のまとまり）
    QLabel        role = "title" / "muted" / "section"
    QFrame/QLabel tone = "warning" / "success" / "danger"（お知らせの帯）
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

FONT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Yu Gothic UI", "Meiryo UI", "Noto Sans CJK JP", "sans-serif"]
FONT_POINT_SIZE = 10


@dataclass(frozen=True)
class Tokens:
    dark: bool
    bg: str
    surface: str
    surface2: str
    hover: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    on_accent: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    preview_bg: str
    chunk_done: str
    chunk_partial: str
    chunk_none: str


LIGHT = Tokens(
    dark=False, bg="#f3f4f7", surface="#ffffff", surface2="#f6f7f9", hover="#eceef2", border="#e1e4ea",
    text="#1b1f27", muted="#6b7280", accent="#2563eb", accent_hover="#1d4ed8", accent_pressed="#1e40af",
    accent_soft="#e6eeff", on_accent="#ffffff", success="#15803d", success_soft="#e8f7ee",
    warning="#b45309", warning_soft="#fff5e0", danger="#dc2626", danger_soft="#fdecec",
    preview_bg="#14161b", chunk_done="#22c55e", chunk_partial="#a7e3b8", chunk_none="#e5e7eb",
)
DARK = Tokens(
    dark=True, bg="#15171c", surface="#1e2127", surface2="#262a31", hover="#2e333b", border="#323741",
    text="#e8eaef", muted="#9aa1ad", accent="#4f8cff", accent_hover="#6a9dff", accent_pressed="#3b78ea",
    accent_soft="#1f2b45", on_accent="#ffffff", success="#4ade80", success_soft="#16291e",
    warning="#fbbf24", warning_soft="#2e2614", danger="#f87171", danger_soft="#321a1c",
    preview_bg="#0d0f12", chunk_done="#22c55e", chunk_partial="#2f7a48", chunk_none="#343943",
)

_current: Tokens = LIGHT


def tokens() -> Tokens:
    """現在のテーマの色（独自に描画する部品が使う）。"""
    return _current


def system_is_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt 6.5 未満
        return app.palette().window().color().lightness() < 128


# --------------------------------------------------------------------------- アイコン画像
def _icon_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "QRTransfer-theme")
    os.makedirs(d, exist_ok=True)
    return d


def _draw_png(path: str, size: int, color: str, kind: str) -> None:
    """チェックマーク・矢印を PNG で描く（exe には SVG の読み込み部品を入れていないため）。"""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), size * 0.13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    s = size
    path_ = QPainterPath()
    if kind == "check":
        path_.moveTo(s * 0.24, s * 0.52)
        path_.lineTo(s * 0.43, s * 0.70)
        path_.lineTo(s * 0.77, s * 0.32)
    elif kind == "down":
        path_.moveTo(s * 0.28, s * 0.40)
        path_.lineTo(s * 0.50, s * 0.62)
        path_.lineTo(s * 0.72, s * 0.40)
    elif kind == "up":
        path_.moveTo(s * 0.28, s * 0.60)
        path_.lineTo(s * 0.50, s * 0.38)
        path_.lineTo(s * 0.72, s * 0.60)
    p.drawPath(path_)
    p.end()
    img.save(path, "PNG")


def _icons(t: Tokens) -> dict[str, str]:
    d = _icon_dir()
    out = {}
    for name, color, kind in (("check", t.on_accent, "check"), ("down", t.muted, "down"), ("up", t.muted, "up"),
                              ("down_disabled", t.border, "down")):
        path = os.path.join(d, f"{name}_{'dark' if t.dark else 'light'}.png")
        if not os.path.exists(path):
            try:
                _draw_png(path, 32, color, kind)
            except Exception:  # noqa: BLE001 - 画像が作れなくても見た目が少し変わるだけ
                pass
        out[name] = path.replace("\\", "/")
    return out


def make_icon(kind: str, color: str, size: int = 64) -> QImage:
    """起動画面の大ボタン用のアイコン（送信 = 上向き、受信 = 下向きの矢印とトレイ）。"""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), size * 0.075, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    s = size
    tray = QPainterPath()
    tray.moveTo(s * 0.18, s * 0.60)
    tray.lineTo(s * 0.18, s * 0.80)
    tray.lineTo(s * 0.82, s * 0.80)
    tray.lineTo(s * 0.82, s * 0.60)
    p.drawPath(tray)
    arrow = QPainterPath()
    if kind == "send":
        arrow.moveTo(s * 0.5, s * 0.64)
        arrow.lineTo(s * 0.5, s * 0.16)
        arrow.moveTo(s * 0.32, s * 0.33)
        arrow.lineTo(s * 0.5, s * 0.16)
        arrow.lineTo(s * 0.68, s * 0.33)
    else:
        arrow.moveTo(s * 0.5, s * 0.16)
        arrow.lineTo(s * 0.5, s * 0.64)
        arrow.moveTo(s * 0.32, s * 0.47)
        arrow.lineTo(s * 0.5, s * 0.64)
        arrow.lineTo(s * 0.68, s * 0.47)
    p.drawPath(arrow)
    p.end()
    return img


# --------------------------------------------------------------------------- スタイルシート
def stylesheet(t: Tokens) -> str:
    i = _icons(t)
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; }}
QMainWindow, QDialog, QStackedWidget, QStackedWidget > QWidget {{ background: {t.bg}; }}
QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; border-radius: 6px; padding: 6px 8px; }}

/* 面（カード） */
QGroupBox, QFrame[role="card"] {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px;
}}
QGroupBox {{ margin-top: 0px; padding: 30px 6px 6px 6px; font-weight: 600; }}
QGroupBox::title {{
    subcontrol-origin: padding; subcontrol-position: top left; left: 14px; top: 10px;
    color: {t.muted}; background: transparent; padding: 0px;
}}
QGroupBox QLabel, QFrame[role="card"] QLabel {{ font-weight: normal; }}

/* 文字 */
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 15pt; font-weight: 700; }}
QLabel[role="hero"] {{ font-size: 28pt; font-weight: 800; }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="section"] {{ color: {t.muted}; font-weight: 700; padding-top: 8px; }}

/* お知らせの帯 */
QLabel[tone], QFrame[tone] {{ border-radius: 10px; padding: 8px 12px; }}
QLabel[tone="warning"], QFrame[tone="warning"] {{ background: {t.warning_soft}; border: 1px solid {t.warning}; }}
QLabel[tone="success"], QFrame[tone="success"] {{ background: {t.success_soft}; border: 1px solid {t.success}; }}
QLabel[tone="danger"], QFrame[tone="danger"] {{ background: {t.danger_soft}; border: 1px solid {t.danger}; }}
QFrame[tone] QLabel {{ padding: 0px; border: none; background: transparent; }}

/* ボタン */
QPushButton {{
    background: {t.surface2}; border: 1px solid {t.border}; border-radius: 8px;
    padding: 6px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.surface2}; border-color: {t.border}; }}
QPushButton[variant="primary"] {{
    background: {t.accent}; color: {t.on_accent}; border: 1px solid {t.accent}; font-weight: 600; padding: 7px 20px;
}}
QPushButton[variant="primary"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {t.accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {t.accent_soft}; border-color: {t.accent_soft}; color: {t.muted}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t.muted}; }}
QPushButton[variant="ghost"]:hover {{ background: {t.hover}; color: {t.text}; }}
QToolButton[variant="tile"] {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 18px;
    font-size: 16pt; font-weight: 700; padding: 18px 12px 16px 12px;
}}
QToolButton[variant="tile"]:hover {{ border: 1px solid {t.accent}; background: {t.accent_soft}; }}
QToolButton[variant="tile"]:pressed {{ background: {t.hover}; }}

/* 入力欄 */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
    selection-background-color: {t.accent}; selection-color: {t.on_accent};
}}
QLineEdit, QSpinBox, QComboBox {{ padding: 5px 10px; min-height: 20px; }}
QPlainTextEdit, QListWidget {{ padding: 4px; background: {t.surface2}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {t.accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t.muted}; background: {t.surface2}; }}
QListWidget::item {{ padding: 5px 6px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {t.hover}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({i["down"]}); width: 14px; height: 14px; }}
QComboBox::down-arrow:disabled {{ image: url({i["down_disabled"]}); }}
QComboBox QAbstractItemView {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px; padding: 4px;
    selection-background-color: {t.accent_soft}; selection-color: {t.text};
}}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 22px; background: transparent; }}
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; }}
QSpinBox::up-arrow {{ image: url({i["up"]}); width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: url({i["down"]}); width: 12px; height: 12px; }}

/* チェックボックス */
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px; border: 1px solid {t.muted}; background: {t.surface};
}}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url({i["check"]}); }}
QCheckBox::indicator:disabled {{ border-color: {t.border}; background: {t.surface2}; }}
QCheckBox::indicator:checked:disabled {{ background: {t.accent_soft}; border-color: {t.accent_soft}; }}
QCheckBox:disabled {{ color: {t.muted}; }}

/* スライダー・進捗 */
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {t.border}; }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: {t.accent}; }}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    background: {t.surface}; border: 2px solid {t.accent};
}}
QSlider::sub-page:horizontal:disabled {{ background: {t.muted}; }}
QSlider::handle:horizontal:disabled {{ border-color: {t.muted}; }}
QProgressBar {{
    background: {t.surface2}; border: 1px solid {t.border}; border-radius: 9px;
    min-height: 16px; max-height: 18px; text-align: center; font-weight: 600;
}}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 8px; }}

/* スクロールバー・区切り */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {t.border}; border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {t.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QScrollArea#rightScroll, QWidget#rightPanel {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 12px; }}
QProgressDialog, QMessageBox {{ background: {t.surface}; }}
"""


def palette(t: Tokens) -> QPalette:
    pal = QPalette()
    c = QColor
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        muted = group == QPalette.ColorGroup.Disabled
        pal.setColor(group, QPalette.ColorRole.Window, c(t.bg))
        pal.setColor(group, QPalette.ColorRole.WindowText, c(t.muted if muted else t.text))
        pal.setColor(group, QPalette.ColorRole.Base, c(t.surface))
        pal.setColor(group, QPalette.ColorRole.AlternateBase, c(t.surface2))
        pal.setColor(group, QPalette.ColorRole.Text, c(t.muted if muted else t.text))
        pal.setColor(group, QPalette.ColorRole.Button, c(t.surface2))
        pal.setColor(group, QPalette.ColorRole.ButtonText, c(t.muted if muted else t.text))
        pal.setColor(group, QPalette.ColorRole.Highlight, c(t.accent))
        pal.setColor(group, QPalette.ColorRole.HighlightedText, c(t.on_accent))
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, c(t.surface))
        pal.setColor(group, QPalette.ColorRole.ToolTipText, c(t.text))
        pal.setColor(group, QPalette.ColorRole.PlaceholderText, c(t.muted))
        pal.setColor(group, QPalette.ColorRole.Link, c(t.accent))
        pal.setColor(group, QPalette.ColorRole.Mid, c(t.border))
    return pal


def apply(app: QApplication, dark: bool | None = None) -> Tokens:
    """テーマを当てる。dark を省略すると Windows の設定に合わせる。"""
    global _current
    _current = (DARK if system_is_dark() else LIGHT) if dark is None else (DARK if dark else LIGHT)
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSize(FONT_POINT_SIZE)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    app.setPalette(palette(_current))
    app.setStyleSheet(stylesheet(_current))
    for w in app.allWidgets():
        w.update()  # 独自に描画する部品（プレビュー・チャンクマップ）も塗り直す
    return _current


def follow_system(app: QApplication) -> None:
    """Windows のライト／ダークが切り替わったら追従する。"""
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _s: apply(app))
    except AttributeError:
        pass


def set_prop(widget: QWidget, name: str, value) -> None:
    """役割（variant / role / tone）を付け直して、見た目を更新する。"""
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def rounded_rect_path(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path

