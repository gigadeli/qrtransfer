"""DecodeThread: 最新フレームを zxing-cpp で読み取り、正常なフレームを Assembler に渡す。

ボケた・コントラストの低いカメラ画像に対応するため、RobustDecoder は次の順に読み取りを試みる。
  1. 素の画像（画面全体）… 通常はこれで読める。QR の位置（ROI）もここで捕捉する
  2. 補正した画像（前回 QR が見つかった位置の周辺だけを処理する）
     - シャープ化（弱／強）… ボケ対策。実験では中程度のボケで読み取り率 0〜35% → 100%
     - 2 倍拡大＋適応的二値化 … 強いボケ・明るさムラ対策
     - CLAHE（局所コントラスト強調）＋シャープ化 … 画面の反射・白飛び対策
  補正は「最近うまくいったもの」から順に試し、1 フレームあたりの処理時間に上限を設ける。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np
import zxingcpp
from PySide6.QtCore import QThread, Signal

from . import assembler as asm_mod
from . import protocol

Points = list[tuple[int, int]]

ENHANCE_BUDGET_SEC = 0.06   # 補正による再試行に使う 1 フレームあたりの時間の上限
ROI_MARGIN = 0.2            # ROI は QR の外接矩形の 20% 外側まで
ROI_FORGET_FRAMES = 10      # 読み取れない状態がこれだけ続いたら ROI を忘れる
SCORE_DECAY = 0.9           # 補正の成功率（指数移動平均）の減衰


@dataclass
class Detection:
    points: Points  # 4 点（カメラ画像の座標）
    ok: bool


def _points(r) -> Points:
    pos = r.position
    return [(pos.top_left.x, pos.top_left.y), (pos.top_right.x, pos.top_right.y),
            (pos.bottom_right.x, pos.bottom_right.y), (pos.bottom_left.x, pos.bottom_left.y)]


def _read(img: np.ndarray, binarizer=None) -> list:
    kw = {} if binarizer is None else {"binarizer": binarizer}
    return zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode, return_errors=True, **kw)


def decode_image(gray: np.ndarray) -> list[tuple[Points, bytes | None]]:
    """グレースケール画像から QR を読み取り、(4 点, バイト列 or None) のリストを返す（補正なし）。"""
    return [(_points(r), bytes(r.bytes) if r.valid else None) for r in _read(gray)]  # text は使わない


# ---------------------------------------------------------------------------
# 画像補正
# ---------------------------------------------------------------------------

def unsharp(img: np.ndarray, sigma: float, amount: float) -> np.ndarray:
    """アンシャープマスク（ボケた輪郭を強調する）。"""
    blurred = cv2.GaussianBlur(img, (0, 0), sigma)
    return cv2.addWeighted(img, 1.0 + amount, blurred, -amount, 0)


_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


@dataclass(frozen=True)
class Variant:
    name: str
    label: str
    scale: float
    fn: Callable[[np.ndarray], np.ndarray]


VARIANTS: tuple[Variant, ...] = (
    Variant("sharpen", "シャープ化（弱）", 1.0, lambda g: unsharp(g, 2.0, 1.0)),
    Variant("sharpen_strong", "シャープ化（強）", 1.0, lambda g: unsharp(g, 3.0, 3.0)),
    Variant("upscale_adaptive", "拡大＋適応的二値化", 2.0,
            lambda g: cv2.adaptiveThreshold(cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC),
                                            255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 2)),
    Variant("clahe_sharpen", "コントラスト強調＋シャープ化", 1.0, lambda g: unsharp(_CLAHE.apply(g), 2.0, 1.5)),
)


def sharpness_score(gray: np.ndarray) -> float:
    """ピントの目安（0〜100 程度。大きいほどシャープ）。コントラストで正規化した勾配の強さ。"""
    if gray.size == 0:
        return 0.0
    lo, hi = np.percentile(gray, (5, 95))
    if hi - lo < 10:
        return 0.0
    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    return float(np.mean(np.abs(lap)) / (hi - lo) * 100)


@dataclass
class DecodeStats:
    frames: int = 0            # 処理したカメラ画像の数
    frames_ok: int = 0         # 1 枚以上正しく読めた画像の数
    frames_detected_bad: int = 0  # QR は見つかったが読めなかった画像の数
    rescued: int = 0           # 補正で初めて読めた画像の数
    by_variant: dict[str, int] = field(default_factory=dict)

    def copy(self) -> "DecodeStats":
        return DecodeStats(self.frames, self.frames_ok, self.frames_detected_bad, self.rescued,
                           dict(self.by_variant))


class RobustDecoder:
    """素の読み取り → 補正付きの再試行、の順で QR を読む。スレッド 1 本から使う前提。"""

    def __init__(self, enhance: bool = True, budget_sec: float = ENHANCE_BUDGET_SEC,
                 variants: tuple[Variant, ...] = VARIANTS):
        self.enhance = enhance
        self.budget_sec = budget_sec
        self.variants = variants
        self.scores = {v.name: 0.0 for v in variants}
        self.roi: tuple[int, int, int, int] | None = None  # x0, y0, x1, y1
        self._miss = 0
        self.stats = DecodeStats()
        self.last_variant = "plain"

    # --- ROI -----------------------------------------------------------------
    def _update_roi(self, pts_list: list[Points], w: int, h: int) -> None:
        xs = [x for pts in pts_list for x, _ in pts]
        ys = [y for pts in pts_list for _, y in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        mx, my = (x1 - x0) * ROI_MARGIN + 8, (y1 - y0) * ROI_MARGIN + 8
        self.roi = (max(0, int(x0 - mx)), max(0, int(y0 - my)), min(w, int(x1 + mx)), min(h, int(y1 + my)))
        self._miss = 0

    def prefer(self, name: str) -> None:
        """指定した補正を最初に試すようにする（カメラの自動調整で効くと分かった補正）。"""
        if name in self.scores:
            self.scores[name] = max(self.scores.values()) + 3.0

    def _ordered_variants(self) -> list[Variant]:
        # 最近成功した補正から試す（同点なら定義順）
        return sorted(self.variants, key=lambda v: -self.scores[v.name])

    # --- 読み取り -------------------------------------------------------------
    def _plan(self) -> list[tuple[Variant, bool]]:
        """補正の試行順 (補正, ROI を使うか)。ROI → 画面全体 の順で、成功率の高い補正から。"""
        ordered = self._ordered_variants()
        plan: list[tuple[Variant, bool]] = []
        if self.roi is not None:
            plan += [(v, True) for v in ordered]
        # 画面全体での再試行（ROI がずれた・未確定の場合の保険）。全体の拡大は重いので除く
        plan += [(v, False) for v in ordered if v.scale == 1]
        return plan

    def decode(self, gray: np.ndarray) -> list[tuple[Points, bytes | None]]:
        h, w = gray.shape[:2]
        t0 = time.monotonic()
        self.stats.frames += 1
        results = decode_image(gray)
        found = [p for p, _ in results]
        self.last_variant = "plain"
        if any(d is not None for _, d in results):
            self._finish(found, w, h, "plain", rescued=False)
            return results

        if self.enhance:
            for v, use_roi in self._plan():
                if time.monotonic() - t0 > self.budget_sec:
                    break
                if use_roi:
                    x0, y0, x1, y1 = self.roi
                    work, ox, oy = gray[y0:y1, x0:x1], x0, y0
                else:
                    work, ox, oy = gray, 0, 0
                if work.size == 0:
                    continue
                attempt = []
                for r in _read(np.ascontiguousarray(v.fn(work))):
                    pts = [(int(x / v.scale + ox), int(y / v.scale + oy)) for x, y in _points(r)]
                    attempt.append((pts, bytes(r.bytes) if r.valid else None))
                if any(d is not None for _, d in attempt):
                    self.scores[v.name] = self.scores[v.name] * SCORE_DECAY + 1.0
                    self.last_variant = v.name
                    self._finish([p for p, _ in attempt], w, h, v.name, rescued=True)
                    return attempt
                self.scores[v.name] *= SCORE_DECAY
                if attempt and not found:
                    found = [p for p, _ in attempt]
                    results = attempt

        # 読めなかった
        self._miss += 1
        if found:
            self.stats.frames_detected_bad += 1
            if self.roi is None:
                # 読めなかった検出は位置が不正確なことがあるので、ROI が無いときだけ使う
                self._update_roi(found, w, h)
                self._miss = 1
        if self._miss >= ROI_FORGET_FRAMES:
            self.roi = None  # QR が動いた可能性があるので、画面全体から探し直す
            self._miss = 0
        return results

    def _finish(self, found: list[Points], w: int, h: int, variant: str, rescued: bool) -> None:
        self.stats.frames_ok += 1
        self.stats.by_variant[variant] = self.stats.by_variant.get(variant, 0) + 1
        if rescued:
            self.stats.rescued += 1
        self._update_roi(found, w, h)

    def roi_sharpness(self, gray: np.ndarray) -> float | None:
        if self.roi is None:
            return None
        x0, y0, x1, y1 = self.roi
        return sharpness_score(gray[y0:y1, x0:x1])


def process_image(image: np.ndarray, assembler: asm_mod.Assembler,
                  decoder: RobustDecoder | None = None) -> tuple[list[Detection], list[str]]:
    """1 画像分の処理（テストや UI 以外からも使えるように分離）。decoder を省略すると補正なしで読む。"""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    decoded = decoder.decode(gray) if decoder is not None else decode_image(gray)
    detections: list[Detection] = []
    statuses: list[str] = []
    for pts, data in decoded:
        frame = protocol.parse_frame(data) if data is not None else None
        if frame is None:
            detections.append(Detection(pts, False))
            continue
        status = assembler.feed(frame)
        statuses.append(status)
        detections.append(Detection(pts, status != asm_mod.ST_INVALID))
    return detections, statuses


class DecodeThread(QThread):
    detections = Signal(object, int, int)  # list[Detection], 画像幅, 画像高さ
    decode_fps = Signal(float)
    stats = Signal(object)  # dict: DecodeStats（直近の区間）とピント指標
    error = Signal(str)

    def __init__(self, source, assembler: asm_mod.Assembler, enhance: bool = True, parent=None):
        super().__init__(parent)
        self.source = source
        self.assembler = assembler
        self.decoder = RobustDecoder(enhance=enhance)
        self._stop = threading.Event()

    def set_enhance(self, on: bool) -> None:
        self.decoder.enhance = on  # bool の代入はスレッド間でも安全

    def prefer_variant(self, name: str) -> None:
        self.decoder.prefer(name)  # dict の 1 要素の代入なので、読み取りスレッドと競合しても壊れない

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        last_id = 0
        count, t0 = 0, time.monotonic()
        last_error = ""
        prev = self.decoder.stats.copy()
        while not self._stop.is_set():
            last_id, frame = self.source.get_latest(last_id)
            if frame is None:
                time.sleep(0.003)
                self.assembler.tick()
                continue
            gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            try:
                dets, _ = process_image(gray, self.assembler, self.decoder)
                self.detections.emit(dets, frame.shape[1], frame.shape[0])
            except Exception as e:  # noqa: BLE001 - スレッドを止めずに UI に通知する
                msg = f"{type(e).__name__}: {e}"
                if msg != last_error:
                    last_error = msg
                    traceback.print_exc()
                    self.error.emit(msg)
            self.assembler.tick()
            count += 1
            now = time.monotonic()
            if now - t0 >= 2.0:
                self.decode_fps.emit(count / (now - t0))
                cur = self.decoder.stats.copy()
                window = DecodeStats(
                    cur.frames - prev.frames, cur.frames_ok - prev.frames_ok,
                    cur.frames_detected_bad - prev.frames_detected_bad, cur.rescued - prev.rescued,
                    {k: v - prev.by_variant.get(k, 0) for k, v in cur.by_variant.items()})
                self.stats.emit({"window": window, "total": cur,
                                 "sharpness": self.decoder.roi_sharpness(gray)})
                prev = cur
                count, t0 = 0, now
