"""送信側の QR 表示ウィンドウ（全画面／ウィンドウモードを切り替えられる）。"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QPainter
from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from .. import packer, protocol, qrgen
from ..settings import CODES_MAX, FPS_MAX, FPS_MIN

TEXT_AREA = 36  # 下部のステータス表示の高さ（px）
WINDOW_MARGIN = 8  # ウィンドウモードでの QR の余白（px）
FULLSCREEN_RATIO = 0.9  # 全画面での QR の大きさ（画面の短辺に対する比率）。複数並べるときは横幅もこの比率まで使う
MIN_WINDOW_SIZE = (360, 420)
DEFAULT_WINDOW_SIZE = (820, 880)


def grid_layout(count: int, area_w: int, area_h: int, modules: int) -> tuple[int, int, int]:
    """count 個の正方形の QR を area_w × area_h に並べるときの (列数, 行数, 1 個の一辺)。一辺が最も大きくなる並べ方を選ぶ。

    QR には周囲の余白（クワイエットゾーン）が含まれているので、隣り合わせに並べても読み取れる。
    """
    best = (1, count, 0)
    for cols in range(1, count + 1):
        rows = -(-count // cols)
        avail = max(1, min(area_w // cols, area_h // rows))
        if avail > best[2]:
            best = (cols, rows, avail)
    cols, rows, avail = best
    # 整数倍で十分近ければ整数倍にしてセル幅を揃える（最近傍補間なのでどちらでもぼやけない）
    scale = avail // modules
    side = modules * scale if scale >= 1 and modules * scale >= avail * 0.93 else avail
    return cols, rows, side


class FullscreenQR(QWidget):
    """QR を 1 枚ずつ（または codes 枚ずつ並べて）表示するウィンドウ。全画面（既定）とウィンドウモードの両方に対応する。"""

    closed = Signal(object)  # 終了時の状態 dict（fps / codes / fullscreen / always_on_top / geometry）

    def __init__(self, plan: packer.TransferPlan, cache: qrgen.QRCache, frame_seqs: list[int], fps: int,
                 fullscreen: bool = True, always_on_top: bool = False,
                 geometry: QByteArray | bytes | None = None, wait_for_start: bool = False, codes: int = 1,
                 parent=None):
        """frame_seqs[i] は cache の i 番目のフレームの seq（META は CAROUSEL_META）。

        wait_for_start=True のときは、最初の QR（META）を静止表示した「待機中」の状態で開き、
        Space / Enter キー（またはクリック）で送信を始める。受信側はその間にカメラの位置やピントを合わせられる。

        codes: 同時に並べて表示する QR の数。カルーセルの順に codes 枚ずつ表示し、1 回の切り替えで codes 枚進む
        （各 QR は独立したフレームなので、受信側は読めたものから使う）。
        """
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
        self.codes = max(1, min(CODES_MAX, codes))
        self.paused = False
        self.waiting = wait_for_start  # 送信開始前の待機中（META を静止表示する）
        self.resend: set[int] | None = None
        self.repairs = sum(1 for s in frame_seqs if packer.repair_index(s) is not None)
        self.order = packer.carousel_order(plan.total, repairs=self.repairs)
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
        if not self.waiting:
            self.timer.start()

    def begin_sending(self) -> None:
        """待機を終えて送信（QR の切り替え）を始める。"""
        if not self.waiting:
            return
        self.waiting = False
        self.paused = False
        self.pos = 0
        self.cycle = 1
        self.timer.start()
        self.update()

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
        return {"fps": self.fps, "codes": self.codes, "fullscreen": self.want_fullscreen, "always_on_top": self.always_on_top,
                "geometry": bytes(geometry.data()) if geometry is not None else b""}

    def _apply_fps(self) -> None:
        self.timer.setInterval(max(1, round(1000 / self.fps)))

    def current_seq(self) -> int:
        return self.order[self.pos]

    def shown(self) -> int:
        """1 回に表示する QR の数（1 周の枚数より多くは並べない。同じ QR が 2 つ並ばないように）。"""
        return max(1, min(self.codes, len(self.order)))

    def current_seqs(self) -> list[int]:
        n = len(self.order)
        return [self.order[(self.pos + i) % n] for i in range(self.shown())]

    def set_codes(self, codes: int) -> None:
        self.codes = max(1, min(CODES_MAX, codes))
        self.update()

    def advance(self, step: int = 1) -> None:
        """表示を step 回分進める（1 回分は、並べて表示する QR の数だけ進む）。"""
        n = len(self.order)
        new = self.pos + step * self.shown()
        if new >= n:
            self.cycle += new // n
        elif new < 0:
            self.cycle = max(1, self.cycle - 1)
        self.pos = new % n
        self.update()

    def set_resend(self, seqs: set[int] | None) -> None:
        self.resend = seqs if seqs else None
        self.order = packer.carousel_order(self.plan.total, self.resend, repairs=self.repairs)
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
        """1 つ目の QR の位置。"""
        return self.qr_rects()[0]

    def qr_rects(self) -> list[QRect]:
        """表示する QR の位置（current_seqs() の順）。"""
        w, h = self.width(), self.height()
        if self.want_fullscreen:
            # 画面の端まで使わず、約 90% にする
            area_w = int(w * FULLSCREEN_RATIO)
            area_h = max(1, min(int(h * FULLSCREEN_RATIO), h - 2 * TEXT_AREA))
        else:
            # ウィンドウモードでは、余白を取りつつ可能な限り大きく表示する
            area_w = w - 2 * WINDOW_MARGIN
            area_h = h - TEXT_AREA - 2 * WINDOW_MARGIN
        count = self.shown()
        cols, rows, side = grid_layout(count, max(1, area_w), max(1, area_h), self.cache.size)
        # 並べた全体を中央に置く（最後の行が埋まらないときは、その行も中央に寄せる）
        top = max(0, (h - TEXT_AREA - rows * side) // 2)
        rects = []
        for i in range(count):
            r, c = divmod(i, cols)
            in_row = min(cols, count - r * cols)
            left = (w - in_row * side) // 2
            rects.append(QRect(left + c * side, top + r * side, side, side))
        return rects

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        seqs = self.current_seqs()
        for seq, rect in zip(seqs, self.qr_rects()):
            scaled = self._image_for(seq).scaled(rect.width(), rect.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                                 Qt.TransformationMode.FastTransformation)
            p.drawImage(rect.topLeft(), scaled)

        font = QFont(self.font())
        text_rect = QRect(0, self.height() - TEXT_AREA, self.width(), TEXT_AREA)
        if self.waiting:
            # 待機中は目立つ表示にする（QR には重ならない下部の帯に描く）
            p.fillRect(text_rect, QColor(255, 243, 205))
            font.setPointSize(12)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor(150, 90, 0))
            mode = "ウィンドウ" if self.want_fullscreen else "全画面"
            candidates = (
                f"待機中（最初の QR を表示中）― 受信側のカメラ調整が終わったら Space / Enter で送信開始"
                f"　　F:{mode}  T:常に手前  Esc:中止",
                "待機中 ― 受信側の準備ができたら Space / Enter で送信開始",
                "待機中 ― Space / Enter で開始",
            )
            fm = p.fontMetrics()
            text = next((t for t in candidates if fm.horizontalAdvance(t) <= self.width() - 16), candidates[-1])
            p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
            return

        if len(seqs) == 1:
            label = self._label(seqs[0])
        else:
            label = " / ".join(self._label(s, short=True) for s in seqs) + f"（{len(seqs)} 個同時）"
        parts = [label, f"周回 {self.cycle}", f"session {self.plan.session_id:08x}", f"{self.fps} fps"]
        if self.resend:
            parts.append(f"再送モード: {len(self.resend)} チャンク")
        if self.always_on_top:
            parts.append("常に手前")
        if self.paused:
            parts.append("一時停止中（←/→ でコマ送り）")
        parts.append("Space:停止  +/-:fps  C:並べる数  R:再送  F:" + ("ウィンドウ" if self.want_fullscreen else "全画面")
                     + "  T:常に手前  Esc:終了")
        font.setPointSize(10)
        p.setFont(font)
        p.setPen(QColor(90, 90, 90))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "    ".join(parts))

    def _label(self, seq: int, short: bool = False) -> str:
        r = packer.repair_index(seq)
        if seq == packer.CAROUSEL_META:
            return "META"
        if r is not None:
            return f"修復 {r}" if short else f"修復 {r}（0〜{self.repairs - 1}）"
        return f"DATA {seq}" if short else f"DATA {seq}（0〜{self.plan.total - 1}）"

    # ------------------------------------------------------------------ キー操作
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif self.waiting and key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.begin_sending()
        elif self.waiting and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            pass  # 待機中は最初の QR のまま
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
        elif key == Qt.Key.Key_C:
            self.set_codes(self.codes % CODES_MAX + 1)  # 1 → 2 → … → CODES_MAX → 1
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.set_fullscreen(not self.want_fullscreen)
        elif key == Qt.Key.Key_T:
            self.set_always_on_top(not self.always_on_top)
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # 待機中はダブルクリックでも開始できる（シングルクリックはウィンドウを選ぶ操作と紛らわしいので使わない）
        if self.waiting and event.button() == Qt.MouseButton.LeftButton:
            self.begin_sending()
        else:
            super().mouseDoubleClickEvent(event)

    def ask_resend(self) -> None:
        was_running = not self.paused and not self.waiting
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
