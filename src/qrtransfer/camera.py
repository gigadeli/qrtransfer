"""CameraThread: OpenCV で連続キャプチャし、最新フレームだけを保持する。

カメラの fps を確保するための接続手順（open_best）:
  OpenCV の DirectShow バックエンドは、解像度を設定するたびに撮影形式を選び直し、直前に指定した MJPG を
  忘れてしまう（非圧縮 YUY2 になり、USB の帯域制限で 1080p なら 5fps 前後に落ちる）。正しい設定順序は
  OpenCV のバージョンやカメラのドライバで異なるため、いくつかの接続方式を実際に試して fps を実測し、
  最も速いものを使う。選ばれた方式は設定に保存し、次回はそれを最初に試す。
"""

from __future__ import annotations

import os

# MSMF（Media Foundation）でカメラを開くのに数十秒かかる既知の問題を避ける（cv2 の読み込み前に設定する）
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import threading  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Callable  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QThread, Signal  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

PREVIEW_INTERVAL_SEC = 1 / 15
PREVIEW_MAX_WIDTH = 960  # プレビュー用に縮小してから UI に送る（UI スレッドの描画負荷を下げる）
TARGET_FPS = 30
MEASURE_FRAMES = 10
MEASURE_TIMEOUT_SEC = 1.5
GOOD_ENOUGH_RATIO = 0.85  # 目標 fps のこの割合以上が出たら、残りの方式は試さない

# 接続方式（キー, 表示名）。"auto" は下記を順に試して最速のものを選ぶ
STRATEGIES: tuple[tuple[str, str], ...] = (
    ("dshow_mjpg_last", "DirectShow（解像度 → MJPG の順に指定）"),
    ("msmf", "Media Foundation"),
    ("dshow_mjpg_first", "DirectShow（MJPG → 解像度の順に指定）"),
    ("dshow_default", "DirectShow（形式を指定しない）"),
)
STRATEGY_KEYS = tuple(k for k, _ in STRATEGIES)
STRATEGY_AUTO = "auto"


def camera_log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "QRTransfer", "camera.log")


def append_camera_log(line: str) -> None:
    """カメラの接続方式ごとの実測結果を %LOCALAPPDATA%\\QRTransfer\\camera.log に追記する（不具合調査用）。"""
    try:
        path = camera_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def fourcc_to_str(value: float) -> str:
    v = int(value) if value and value > 0 else 0
    s = "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))
    return s if v and s.isprintable() and s.strip() else "?"


def _backend_of(strategy: str) -> tuple[int, str]:
    return (cv2.CAP_MSMF, "MSMF") if strategy == "msmf" else (cv2.CAP_DSHOW, "DSHOW")


def apply_strategy(cap, strategy: str, width: int, height: int, fps: int = TARGET_FPS) -> None:
    """接続方式に応じた順序で、解像度・形式・fps を設定する。"""
    mjpg = cv2.VideoWriter_fourcc(*"MJPG")
    if strategy == "dshow_mjpg_last":
        # fps → 解像度 → 形式。解像度の設定で形式が選び直された後に MJPG を指定し直す
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FOURCC, mjpg)
    elif strategy == "dshow_mjpg_first":
        cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
    else:  # msmf / dshow_default: 形式はバックエンドに任せる（MSMF は自前で最適な形式を選ぶ）
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)


def measure_fps(cap, frames: int = MEASURE_FRAMES, timeout: float = MEASURE_TIMEOUT_SEC,
                stop: threading.Event | None = None) -> float:
    """実際に読み込める fps を測る（最初の 2 枚は立ち上がりとして除く）。"""
    for _ in range(2):
        ok, _f = cap.read()
        if not ok:
            return 0.0
    t0 = time.monotonic()
    count = 0
    while count < frames and time.monotonic() - t0 < timeout:
        if stop is not None and stop.is_set():
            break
        ok, _f = cap.read()
        if ok:
            count += 1
    elapsed = time.monotonic() - t0
    return count / elapsed if elapsed > 0 else 0.0


@dataclass
class CameraMode:
    strategy: str
    backend: str
    width: int
    height: int
    fourcc: str
    fps: float  # 実測

    def describe(self) -> str:
        return f"{self.backend}, {self.width}×{self.height}, {self.fourcc}, 実測 {self.fps:.0f} fps"


def _open_with(factory, index: int, strategy: str, width: int, height: int):
    backend, _ = _backend_of(strategy)
    try:
        cap = factory(index, backend)
    except cv2.error:
        return None
    if cap is None or not cap.isOpened():
        if cap is not None:
            cap.release()
        return None
    apply_strategy(cap, strategy, width, height)
    return cap


def open_best(index: int, width: int, height: int, strategy: str = STRATEGY_AUTO,
              preferred: str | None = None, stop: threading.Event | None = None,
              factory: Callable = cv2.VideoCapture, measure: Callable = measure_fps,
              log: Callable[[str], None] | None = None):
    """接続方式を試して fps を実測し、最も速い方式で開いたカメラを返す。

    strategy が "auto" 以外なら、その方式だけを使う。preferred（前回選ばれた方式）があれば最初に試す。
    戻り値: (cap, CameraMode, 試した方式の一覧) / 開けなければ (None, None, 一覧)
    """
    if strategy != STRATEGY_AUTO:
        order = [strategy]
    else:
        order = ([preferred] if preferred in STRATEGY_KEYS else []) + \
                [k for k in STRATEGY_KEYS if k != preferred]
    tried: list[CameraMode] = []
    best: CameraMode | None = None
    best_score = -1.0
    cap = None
    for key in order:
        if stop is not None and stop.is_set():
            break
        cap = _open_with(factory, index, key, width, height)
        if cap is None:
            continue
        fps = measure(cap, stop=stop)
        _, backend = _backend_of(key)
        mode = CameraMode(key, backend, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                          int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), fourcc_to_str(cap.get(cv2.CAP_PROP_FOURCC)), fps)
        tried.append(mode)
        if log:
            log(f"camera {index} {key}: {mode.describe()}")
        resolution_ok = (mode.width, mode.height) == (width, height)
        score = fps * (1.0 if resolution_ok else 0.5)
        if score > best_score:
            best, best_score = mode, score
        if fps > 0 and resolution_ok and fps >= TARGET_FPS * GOOD_ENOUGH_RATIO:
            return cap, mode, tried  # 十分速いので、これを使う
        if key == order[-1] and best is mode:
            return cap, mode, tried  # 最後に試したものが最良なら開き直さない
        cap.release()  # 同じカメラは同時に 1 つのバックエンドでしか開けないため、閉じてから次を試す
        cap = None
    if best is None or best.fps <= 0:
        return None, None, tried
    cap = _open_with(factory, index, best.strategy, width, height)
    if cap is None:
        return None, None, tried
    ok, _f = cap.read()
    if not ok:
        cap.release()
        return None, None, tried
    return cap, best, tried


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


def bgr_to_qimage(frame: np.ndarray, max_width: int | None = None) -> QImage:
    if max_width is not None and frame.shape[1] > max_width:
        h = round(frame.shape[0] * max_width / frame.shape[1])
        frame = cv2.resize(frame, (max_width, h), interpolation=cv2.INTER_AREA)  # GIL を解放して縮小
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
    mode_selected = Signal(object)  # CameraMode（選ばれた接続方式と実測 fps）

    def __init__(self, index: int, width: int, height: int, autofocus: bool, manual_focus: int = -1,
                 strategy: str = STRATEGY_AUTO, preferred: str | None = None, parent=None):
        super().__init__(parent)
        self.index = index
        self.width = width
        self.height = height
        self.autofocus = autofocus
        self.manual_focus = manual_focus  # -1 = 指定しない（カメラの現在値のまま）
        self.strategy = strategy
        self.preferred = preferred
        self.mode: CameraMode | None = None
        self.tried: list[CameraMode] = []
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

    def _configure_focus(self, cap: cv2.VideoCapture) -> None:
        # フォーカスの設定は撮影形式を変えないので、接続方式が決まった後に行う
        if self.autofocus:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        else:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            if self.manual_focus >= 0:
                cap.set(cv2.CAP_PROP_FOCUS, self.manual_focus)

    def run(self) -> None:
        cap, mode, self.tried = open_best(self.index, self.width, self.height, self.strategy, self.preferred,
                                          stop=self._stop, log=append_camera_log)
        if cap is None:
            if not self._stop.is_set():
                self.opened.emit(False, f"カメラ {self.index} を開けませんでした")
            return
        append_camera_log(f"camera {self.index} selected: {mode.strategy} ({mode.describe()})")
        try:
            self.mode = mode
            self._configure_focus(cap)
            self.mode_selected.emit(mode)
            self.opened.emit(True, f"カメラ {self.index}（{mode.describe()}）")
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
                    self.preview.emit(bgr_to_qimage(frame, PREVIEW_MAX_WIDTH))
                if now - t0 >= 2.0:
                    self.fps_measured.emit(count / (now - t0))
                    count, t0 = 0, now
        finally:
            if cap is not None:
                cap.release()
