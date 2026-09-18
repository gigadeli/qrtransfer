"""送信側の QR 表示ウィンドウ（全画面／ウィンドウモードを切り替えられる）。"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QPainter
from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from .. import packer, protocol, qrgen
from ..settings import FPS_MAX, FPS_MIN

TEXT_AREA = 36  # 下部のステータス表示の高さ（px）
WINDOW_MARGIN = 8  # ウィンドウモードでの QR の余白（px）
FULLSCREEN_RATIO = 0.9  # 全画面での QR の大きさ（画面の短辺に対する比率）
MIN_WINDOW_SIZE = (360, 420)
DEFAULT_WINDOW_SIZE = (820, 880)


class FullscreenQR(QWidget):
    """QR を 1 枚ずつ表示するウィンドウ。全画面（既定）とウィンドウモードの両方に対応する。"""

    closed = Signal(object)  # 終了時の状態 dict（fps / fullscreen / always_on_top / geometry）

    def __init__(self, plan: packer.TransferPlan, cache: qrgen.QRCache, frame_seqs: list[int], fps: int,
                 fullscreen: bool = True, always_on_top: bool = False,
                 geometry: QByteArray | bytes | None = None, parent=None):
        """frame_seqs[i] は cache の i 番目のフレームの seq（META は CAROUSEL_META）。"""
        super().__init__(parent)
        self.setWindowTitle(f"QRTransfer - 送信中: {plan.meta.get('name', '')}")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self.want_fullscreen = fullscreen
        self.always_on_top = always_on_top
        self._saved_geometry = QByteArray(geometry) if geometry else QByteArray()
        self.plan = plan
        self.cache = cache
        # seq → cache の添字（META は 1 つだけ使う）
        self.meta_index = frame_seqs.index(packer.CAROUSEL_META)
        self.data_index = {s: i for i, s in enumerate(frame_seqs) if s != packer.CAROUSEL_META}
        self.fps = max(FPS_MIN, min(FPS_MAX, fps))
        self.paused = False
        self.resend: set[int] | None = None
        self.order = packer.carousel_order(plan.total)
        self.pos = 0
        self.cycle = 1
        self._image_cache: dict[int, QImage] = {}

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.advance)
        self._apply_fps()

    # ------------------------------------------------------------------ 制御
    def start(self) -> None:
        self._sync_flags_and_show()
        self.timer.start()

    def _sync_flags_and_show(self) -> None:
        """「常に手前」フラグを整えてから、現在のモードで表示する。

        全画面のときは WindowStaysOnTopHint を付けない（全画面自体が最前面であり、
        表示中にフラグを変えると Qt が全画面状態を解除してしまうため）。
        """
        want_flag = self.always_on_top and not self.want_fullscreen
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) != want_flag:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, want_flag)
        self._show_current_mode()
        self.activateWindow()
        self.setFocus()

    def _show_current_mode(self) -> None:
        if self.want_fullscreen:
            self.setCursor(Qt.CursorShape.BlankCursor)
            # ウィンドウフラグを変えた直後は showFullScreen() が効かないことがあるため、状態を明示する
            self.setWindowState(self.windowState() | Qt.WindowState.WindowFullScreen)
            self.show()
        else:
            self.unsetCursor()
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)
            self.showNormal()
            if not self._saved_geometry.isEmpty():
                self.restoreGeometry(self._saved_geometry)
            else:
                self.resize(*DEFAULT_WINDOW_SIZE)
        self.update()

    def set_fullscreen(self, on: bool) -> None:
        """全画面／ウィンドウモードを切り替える。ウィンドウモードの位置と大きさは保持する。"""
        if on == self.want_fullscreen and self.isVisible():
            return
        if on and not self.want_fullscreen and self.isVisible():
            self._saved_geometry = self.saveGeometry()
        self.want_fullscreen = on
        if self.isVisible():
            self._sync_flags_and_show()

    def set_always_on_top(self, on: bool) -> None:
        self.always_on_top = on
        if not self.isVisible():
            return
        if self.want_fullscreen:
            self.update()  # 全画面中は設定を覚えるだけ（ウィンドウに戻したときに反映する）
            return
        self._saved_geometry = self.saveGeometry()
        # Windows ではウィンドウフラグの変更で非表示になるため、表示し直す
        self._sync_flags_and_show()

    def state(self) -> dict:
        geometry = self.saveGeometry() if (self.isVisible() and not self.want_fullscreen) else self._saved_geometry
        return {"fps": self.fps, "fullscreen": self.want_fullscreen, "always_on_top": self.always_on_top,
                "geometry": bytes(geometry.data()) if geometry is not None else b""}

    def _apply_fps(self) -> None:
        self.timer.setInterval(max(1, round(1000 / self.fps)))

    def current_seq(self) -> int:
        return self.order[self.pos]

    def advance(self, step: int = 1) -> None:
        n = len(self.order)
        new = self.pos + step
        if new >= n:
            self.cycle += new // n
        elif new < 0:
            self.cycle = max(1, self.cycle - 1)
        self.pos = new % n
        self.update()

    def set_resend(self, seqs: set[int] | None) -> None:
        self.resend = seqs if seqs else None
        self.order = packer.carousel_order(self.plan.total, self.resend)
        self.pos = 0
        self.cycle = 1
        self.update()

    def _image_for(self, seq: int) -> QImage:
        idx = self.meta_index if seq == packer.CAROUSEL_META else self.data_index[seq]
        img = self._image_cache.get(idx)
        if img is None:
            img = qrgen.to_qimage(self.cache.matrix(idx))
            if len(self._image_cache) > 256:
                self._image_cache.clear()
            self._image_cache[idx] = img
        return img

    # ------------------------------------------------------------------ 描画
    def qr_rect(self) -> QRect:
        w, h = self.width(), self.height()
        if self.want_fullscreen:
            # 画面の端まで使わず、短辺の約 90% にする
            avail = max(1, min(int(min(w, h) * FULLSCREEN_RATIO), h - 2 * TEXT_AREA))
        else:
            # ウィンドウモードでは、余白を取りつつ可能な限り大きく表示する
            avail = max(1, min(w - 2 * WINDOW_MARGIN, h - TEXT_AREA - 2 * WINDOW_MARGIN))
        modules = self.cache.size
        # 整数倍で短辺の 90% に十分近ければ整数倍にしてセル幅を揃える（最近傍補間なのでどちらでもぼやけない）
        scale = avail // modules
        side = modules * scale if scale >= 1 and modules * scale >= avail * 0.93 else avail
        x = (w - side) // 2
        y = max(0, (h - TEXT_AREA - side) // 2)
        return QRect(x, y, side, side)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        seq = self.current_seq()
        img = self._image_for(seq)
        rect = self.qr_rect()
        scaled = img.scaled(rect.width(), rect.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)
        p.drawImage(rect.topLeft(), scaled)

        total = self.plan.total
        label = "META" if seq == packer.CAROUSEL_META else f"DATA {seq}（0〜{total - 1}）"
        parts = [label, f"周回 {self.cycle}", f"session {self.plan.session_id:08x}", f"{self.fps} fps"]
        if self.resend:
            parts.append(f"再送モード: {len(self.resend)} チャンク")
        if self.always_on_top:
            parts.append("常に手前")
        if self.paused:
            parts.append("一時停止中（←/→ でコマ送り）")
        parts.append("Space:停止  +/-:fps  R:再送  F:" + ("ウィンドウ" if self.want_fullscreen else "全画面")
                     + "  T:常に手前  Esc:終了")
        font = QFont(self.font())
        font.setPointSize(10)
        p.setFont(font)
        p.setPen(QColor(90, 90, 90))
        text_rect = QRect(0, self.height() - TEXT_AREA, self.width(), TEXT_AREA)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "    ".join(parts))

    # ------------------------------------------------------------------ キー操作
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_Space:
            self.paused = not self.paused
            if self.paused:
                self.timer.stop()
            else:
                self.timer.start()
            self.update()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Left) and self.paused:
            self.advance(1 if key == Qt.Key.Key_Right else -1)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Semicolon):
            self.fps = min(FPS_MAX, self.fps + 1)
            self._apply_fps()
            self.update()
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.fps = max(FPS_MIN, self.fps - 1)
            self._apply_fps()
            self.update()
        elif key == Qt.Key.Key_R:
            self.ask_resend()
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.set_fullscreen(not self.want_fullscreen)
        elif key == Qt.Key.Key_T:
            self.set_always_on_top(not self.always_on_top)
        else:
            super().keyPressEvent(event)

    def ask_resend(self) -> None:
        was_running = not self.paused
        self.timer.stop()
        self.unsetCursor()
        try:
            current = protocol.format_ranges(self.resend) if self.resend else ""
            while True:
                text, ok = QInputDialog.getText(
                    self, "再送モード",
                    f"受信側に表示された欠落番号を入力してください（例: 12,57-60,99）\n"
                    f"有効な範囲: 0〜{max(0, self.plan.total - 1)}。空にして OK で解除（全チャンク表示に戻る）。",
                    text=current)
                if not ok:
                    return
                try:
                    seqs = protocol.parse_ranges(text, limit=self.plan.total)
                except ValueError as e:
                    QMessageBox.warning(self, "入力エラー", f"番号の書式が正しくありません。\n{e}")
                    current = text
                    continue
                self.set_resend(seqs)
                return
        finally:
            if self.want_fullscreen:
                self.setCursor(Qt.CursorShape.BlankCursor)
            self.activateWindow()
            self.setFocus()
            if was_running:
                self.timer.start()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.closed.emit(self.state())
        super().closeEvent(event)
