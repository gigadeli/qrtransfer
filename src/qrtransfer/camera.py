"""CameraThread: OpenCV で連続キャプチャし、最新フレームだけを保持する。"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

PREVIEW_INTERVAL_SEC = 1 / 15


def open_camera(index: int) -> tuple[cv2.VideoCapture | None, str]:
    """DSHOW で開き、だめなら MSMF にフォールバックする。"""
    for backend, name in ((cv2.CAP_DSHOW, "DSHOW"), (cv2.CAP_MSMF, "MSMF")):
        try:
            cap = cv2.VideoCapture(index, backend)
        except cv2.error:
            continue
        if cap is not None and cap.isOpened():
            ok, _ = cap.read()
            if ok:
                return cap, name
        if cap is not None:
            cap.release()
    return None, ""


def probe_cameras(max_index: int = 5) -> list[tuple[int, str]]:
    """インデックス 0〜max_index を試し、開けたカメラの (index, backend) を返す。"""
    found = []
    for i in range(max_index + 1):
        cap, backend = open_camera(i)
        if cap is not None:
            found.append((i, backend))
            cap.release()
    return found


def bgr_to_qimage(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class FrameSource:
    """最新フレームだけを保持するバッファ（キュー長 1）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._id = 0

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._id += 1

    def get_latest(self, last_id: int) -> tuple[int, np.ndarray | None]:
        with self._lock:
            if self._id == last_id:
                return last_id, None
            return self._id, self._frame


FOCUS_MIN, FOCUS_MAX = 0, 255  # CAP_PROP_FOCUS の範囲はカメラによって異なる（多くの UVC カメラは 0〜255）


class CameraThread(QThread):
    preview = Signal(QImage)
    opened = Signal(bool, str)  # (成功, 説明)
    fps_measured = Signal(float)
    focus_value = Signal(int)  # 開いたときのフォーカス値（取得できない場合は -1）

    def __init__(self, index: int, width: int, height: int, autofocus: bool, manual_focus: int = -1,
                 parent=None):
        super().__init__(parent)
        self.index = index
        self.width = width
        self.height = height
        self.autofocus = autofocus
        self.manual_focus = manual_focus  # -1 = 指定しない（カメラの現在値のまま）
        self.source = FrameSource()
        self._stop = threading.Event()
        self._props_lock = threading.Lock()
        self._pending_props: dict[int, float] = {}

    def stop(self) -> None:
        self._stop.set()

    def set_autofocus(self, on: bool) -> None:
        """撮影中にオートフォーカスを切り替える（カメラが対応している場合）。"""
        self.autofocus = on
        props = {cv2.CAP_PROP_AUTOFOCUS: 1 if on else 0}
        if not on and self.manual_focus >= 0:
            props[cv2.CAP_PROP_FOCUS] = self.manual_focus
        self._request(props)

    def set_focus(self, value: int) -> None:
        """撮影中に手動フォーカス値を変える（オートフォーカス OFF のときに有効）。"""
        self.manual_focus = int(value)
        if not self.autofocus:
            self._request({cv2.CAP_PROP_AUTOFOCUS: 0, cv2.CAP_PROP_FOCUS: self.manual_focus})

    def _request(self, props: dict[int, float]) -> None:
        with self._props_lock:
            self._pending_props.update(props)

    def _apply_pending(self, cap: cv2.VideoCapture) -> None:
        with self._props_lock:
            props, self._pending_props = self._pending_props, {}
        for prop, value in props.items():  # AUTOFOCUS → FOCUS の順（dict は挿入順）
            try:
                cap.set(prop, value)
            except cv2.error:
                pass

    def _configure(self, cap: cv2.VideoCapture, mjpg: bool) -> bool:
        if mjpg:
            # USB カメラの多くは非圧縮 (YUY2) だと 1080p で 5fps 程度しか出ないため、MJPG を要求する
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if self.autofocus:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        else:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            if self.manual_focus >= 0:
                cap.set(cv2.CAP_PROP_FOCUS, self.manual_focus)
        ok, frame = cap.read()
        return bool(ok and frame is not None)

    def run(self) -> None:
        cap, backend = open_camera(self.index)
        if cap is None:
            self.opened.emit(False, f"カメラ {self.index} を開けませんでした")
            return
        try:
            mjpg = backend == "DSHOW"
            if not self._configure(cap, mjpg):
                cap.release()
                cap, backend = open_camera(self.index)
                if cap is None or not self._configure(cap, False):
                    self.opened.emit(False, f"カメラ {self.index} の設定に失敗しました")
                    return
                mjpg = False
            aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fmt = "MJPG" if mjpg else "既定形式"
            self.opened.emit(True, f"カメラ {self.index}（{backend}, {aw}×{ah}, {fmt}）")
            try:
                focus = cap.get(cv2.CAP_PROP_FOCUS)
            except cv2.error:
                focus = -1
            self.focus_value.emit(int(focus) if focus is not None and focus >= 0 else -1)
            last_preview = 0.0
            count, t0 = 0, time.monotonic()
            failures = 0
            while not self._stop.is_set():
                self._apply_pending(cap)
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures > 50:
                        self.opened.emit(False, "カメラから画像を取得できなくなりました")
                        break
                    time.sleep(0.02)
                    continue
                failures = 0
                self.source.put(frame)
                count += 1
                now = time.monotonic()
                if now - last_preview >= PREVIEW_INTERVAL_SEC:
                    last_preview = now
                    self.preview.emit(bgr_to_qimage(frame))
                if now - t0 >= 2.0:
                    self.fps_measured.emit(count / (now - t0))
                    count, t0 = 0, now
        finally:
            if cap is not None:
                cap.release()
