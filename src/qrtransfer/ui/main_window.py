"""メインウィンドウ: 起動画面（送信 / 受信 / 設定）と各モードの切り替え。"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPushButton, QSpinBox, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__, packer, qrgen
from ..assembler import Assembler, IncompleteSession
from ..settings import FPS_MAX, FPS_MIN, RESOLUTIONS, Settings
from .fullscreen_qr import FullscreenQR
from .receiver_view import ReceiverView
from . import theme
from .sender_view import SenderView, human_size


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    return os.path.join(base, rel)


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "section")
    return label


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("設定")
        form = QFormLayout(self)
        form.setContentsMargins(22, 18, 22, 18)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self.spin_cam = QSpinBox()
        self.spin_cam.setRange(0, 5)
        self.spin_cam.setValue(min(5, settings.camera_index))
        self.combo_res = QComboBox()
        for w, h in RESOLUTIONS:
            self.combo_res.addItem(f"{w}×{h}", (w, h))
        self.combo_res.setCurrentIndex(RESOLUTIONS.index(settings.resolution))
        self.chk_af = QCheckBox("オートフォーカスを ON にする")
        self.chk_af.setChecked(settings.autofocus)

        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(FPS_MIN, FPS_MAX)
        self.spin_fps.setValue(settings.fps)
        self.spin_chunk = QSpinBox()
        self.spin_chunk.setRange(packer.CHUNK_SIZE_MIN, packer.CHUNK_SIZE_MAX)
        self.spin_chunk.setSingleStep(50)
        self.spin_chunk.setValue(settings.chunk_size)
        self.combo_ecc = QComboBox()
        for e in qrgen.ECC_LEVELS:
            self.combo_ecc.addItem(e.upper(), e)
        self.combo_ecc.setCurrentIndex(qrgen.ECC_LEVELS.index(settings.ecc))
        self.combo_display = QComboBox()
        self.combo_display.addItem("全画面", True)
        self.combo_display.addItem("ウィンドウ", False)
        self.combo_display.setCurrentIndex(0 if settings.qr_fullscreen else 1)
        self.chk_on_top = QCheckBox("常に手前に表示する（ウィンドウモード向け）")
        self.chk_on_top.setChecked(settings.qr_always_on_top)
        self.chk_wait = QCheckBox("待機状態で開き、Space / Enter で送信を開始する")
        self.chk_wait.setChecked(settings.qr_wait_for_start)

        out_row = QHBoxLayout()
        self.edit_out = QLineEdit(settings.output_dir)
        btn = QPushButton("参照…")
        btn.clicked.connect(self._browse)
        out_row.addWidget(self.edit_out, 1)
        out_row.addWidget(btn)
        self.chk_zip = QCheckBox("受信した .zip を自動で展開する")
        self.chk_zip.setChecked(settings.auto_extract_zip)

        form.addRow(_section("受信"))
        form.addRow("カメラのインデックス", self.spin_cam)
        form.addRow("解像度", self.combo_res)
        form.addRow("", self.chk_af)
        form.addRow("保存先", out_row)
        form.addRow("", self.chk_zip)
        form.addRow(_section("送信"))
        form.addRow("表示速度 (fps)", self.spin_fps)
        form.addRow("チャンクサイズ (バイト)", self.spin_chunk)
        form.addRow("誤り訂正レベル (ECC)", self.combo_ecc)
        form.addRow("QR の表示方法", self.combo_display)
        form.addRow("", self.chk_on_top)
        form.addRow("", self.chk_wait)
        path_label = QLabel(f"設定ファイル: {settings._q.fileName()}")
        path_label.setProperty("role", "muted")
        path_label.setWordWrap(True)
        form.addRow(path_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("variant", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "保存先フォルダ", self.edit_out.text())
        if d:
            self.edit_out.setText(os.path.normpath(d))

    def accept(self) -> None:
        s = self.settings
        s.camera_index = self.spin_cam.value()
        s.resolution = self.combo_res.currentData()
        s.autofocus = self.chk_af.isChecked()
        s.fps = self.spin_fps.value()
        s.chunk_size = self.spin_chunk.value()
        s.ecc = self.combo_ecc.currentData()
        s.qr_fullscreen = bool(self.combo_display.currentData())
        s.qr_always_on_top = self.chk_on_top.isChecked()
        s.qr_wait_for_start = self.chk_wait.isChecked()
        if self.edit_out.text().strip():
            s.output_dir = self.edit_out.text().strip()
        s.auto_extract_zip = self.chk_zip.isChecked()
        s.sync()
        super().accept()


class ResumeDialog(QDialog):
    """未完了セッションの再開確認。"""

    def __init__(self, sessions: list[IncompleteSession], parent=None):
        super().__init__(parent)
        self.setWindowTitle("未完了の受信があります")
        self.sessions = sessions
        self.choice: IncompleteSession | None = None
        self.discard_all = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(12)
        lay.addWidget(QLabel("前回、途中で終了した受信があります。再開しますか？\n"
                             "（再開すると受信モードに移ります。送信側で同じ転送を表示してください）"))
        self.list = QListWidget()
        for s in sessions:
            name = s.meta["name"] if s.meta else "（メタ情報未取得）"
            size = f"、{human_size(s.meta['raw_size'])}" if s.meta else ""
            pct = s.received / s.total * 100 if s.total else 0
            item = QListWidgetItem(f"{name}{size}　{s.received}/{s.total} チャンク（{pct:.0f}%）　session {s.session_id:08x}")
            item.setData(Qt.ItemDataRole.UserRole, s.session_id)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        b_resume = QPushButton("選んだ受信を再開")
        b_discard = QPushButton("すべて破棄")
        b_later = QPushButton("後で")
        b_resume.clicked.connect(self._resume)
        b_discard.clicked.connect(self._discard)
        b_later.clicked.connect(self.reject)
        b_resume.setProperty("variant", "primary")
        b_later.setProperty("variant", "ghost")
        row.addWidget(b_resume)
        row.addWidget(b_discard)
        row.addStretch(1)
        row.addWidget(b_later)
        lay.addLayout(row)
        self.resize(560, 260)

    def _resume(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.choice = self.sessions[row]
            self.accept()

    def _discard(self) -> None:
        if QMessageBox.question(self, "破棄", "未完了の受信データをすべて削除しますか？") == QMessageBox.StandardButton.Yes:
            self.discard_all = True
            self.accept()


class StartPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.addStretch(1)
        title = QLabel(APP_NAME)
        title.setProperty("role", "hero")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        lay.addSpacing(6)
        sub = QLabel("QR コードの表示とカメラの読み取りで、ファイルを一方向に転送します")
        sub.setProperty("role", "muted")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)
        lay.addSpacing(36)
        row = QHBoxLayout()
        row.setSpacing(20)
        row.addStretch(1)
        self.btn_send = self._tile("送信", "send")
        self.btn_recv = self._tile("受信", "receive")
        row.addWidget(self.btn_send)
        row.addWidget(self.btn_recv)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(18)
        srow = QHBoxLayout()
        srow.addStretch(1)
        self.btn_settings = QPushButton("設定…")
        self.btn_settings.setProperty("variant", "ghost")
        srow.addWidget(self.btn_settings)
        srow.addStretch(1)
        lay.addLayout(srow)
        lay.addStretch(2)
        ver = QLabel(f"v{__version__}")
        ver.setProperty("role", "muted")
        ver.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(ver)

    @staticmethod
    def _tile(text: str, kind: str) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setProperty("variant", "tile")
        b.setProperty("icon_kind", kind)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setIconSize(QSize(52, 52))
        b.setFixedSize(220, 160)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        StartPage._update_tile_icon(b)
        return b

    @staticmethod
    def _update_tile_icon(b: QToolButton) -> None:
        img = theme.make_icon(b.property("icon_kind"), theme.tokens().accent, 128)
        b.setIcon(QIcon(QPixmap.fromImage(img)))

    def changeEvent(self, event) -> None:
        # テーマ（ライト／ダーク）が切り替わったら、アイコンの色も合わせる
        if event.type() in (event.Type.PaletteChange, event.Type.StyleChange):
            for b in (self.btn_send, self.btn_recv):
                self._update_tile_icon(b)
        super().changeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        icon = resource_path(os.path.join("assets", "app.ico"))
        if os.path.exists(icon):
            self.setWindowIcon(QIcon(icon))
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.start_page = StartPage()
        self.sender: SenderView | None = None
        self.receiver: ReceiverView | None = None
        self.fullscreen: FullscreenQR | None = None
        self.stack.addWidget(self.start_page)
        self.start_page.btn_send.clicked.connect(self.show_sender)
        self.start_page.btn_recv.clicked.connect(self.show_receiver)
        self.start_page.btn_settings.clicked.connect(self.open_settings)
        self.resize(1200, 780)

    # ------------------------------------------------------------------ 画面遷移
    def show_start(self) -> None:
        if self.receiver is not None:
            self.receiver.stop_camera()
        self.stack.setCurrentWidget(self.start_page)

    def show_sender(self) -> None:
        if self.sender is None:
            self.sender = SenderView(self.settings)
            self.sender.back_requested.connect(self.show_start)
            self.sender.start_fullscreen.connect(self.start_fullscreen)
            self.stack.addWidget(self.sender)
        self.stack.setCurrentWidget(self.sender)

    def show_receiver(self) -> ReceiverView:
        if self.receiver is None:
            self.receiver = ReceiverView(self.settings)
            self.receiver.back_requested.connect(self.show_start)
            self.stack.addWidget(self.receiver)
        self.stack.setCurrentWidget(self.receiver)
        return self.receiver

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # 作成済みの画面は、次に開いたときに設定を反映するため作り直す
            for view in (self.sender, self.receiver):
                if view is not None:
                    view.shutdown()
                    self.stack.removeWidget(view)
                    view.deleteLater()
            self.sender = self.receiver = None

    def start_fullscreen(self, plan, cache, frame_seqs, fps: int) -> None:
        if self.fullscreen is not None:
            self.fullscreen.close()
        use_fullscreen = self.settings.qr_fullscreen
        fs = FullscreenQR(plan, cache, frame_seqs, fps, fullscreen=use_fullscreen,
                          always_on_top=self.settings.qr_always_on_top,
                          geometry=self.settings.qr_window_geometry or None,
                          wait_for_start=self.settings.qr_wait_for_start)
        if self.windowIcon() is not None:
            fs.setWindowIcon(self.windowIcon())
        fs.closed.connect(self._fullscreen_closed)
        # メインウィンドウがあるモニタに表示する（別モニタに出したい場合はウィンドウを移動してから開始）
        screen = self.screen()
        if screen is not None and use_fullscreen:
            fs.setGeometry(screen.geometry())
        self.fullscreen = fs
        fs.start()

    def _fullscreen_closed(self, state: dict) -> None:
        self.fullscreen = None
        fps = int(state.get("fps", self.settings.fps))
        if self.sender is not None:
            self.sender.spin_fps.setValue(fps)
            self.sender.combo_display.setCurrentIndex(0 if state.get("fullscreen", True) else 1)
            self.sender.chk_on_top.setChecked(bool(state.get("always_on_top", False)))
        self.settings.fps = fps
        self.settings.qr_fullscreen = bool(state.get("fullscreen", True))
        self.settings.qr_always_on_top = bool(state.get("always_on_top", False))
        if state.get("geometry"):
            self.settings.qr_window_geometry = state["geometry"]
        self.settings.sync()
        self.activateWindow()

    # ------------------------------------------------------------------ 再開
    def check_incomplete_sessions(self) -> None:
        sessions = Assembler.list_incomplete()
        if not sessions:
            return
        dlg = ResumeDialog(sessions, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.discard_all:
            for s in sessions:
                Assembler.delete_session_dir(s.path)
            return
        if dlg.choice is not None:
            view = self.show_receiver()
            if not view.resume_session(dlg.choice.session_id):
                QMessageBox.warning(self, "再開", "受信データを読み込めませんでした。")

    def closeEvent(self, event) -> None:
        if self.fullscreen is not None:
            self.fullscreen.close()
        for view in (self.sender, self.receiver):
            if view is not None:
                view.shutdown()
        super().closeEvent(event)


def run(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    theme.apply(app)
    theme.follow_system(app)
    win = MainWindow()
    win.show()
    from PySide6.QtCore import QTimer
    QTimer.singleShot(0, win.check_incomplete_sessions)
    return app.exec()
