"""カメラの自動調整（ピント・露出）。受信を始める前に 1 回だけ行い、結果を固定する。

読み取り中にオートフォーカスや自動露出が動くと、ピントが迷ったり明るさが揺れたりしてチャンクの欠落が増える。
そこで、送信側の待機画面（最初の QR を静止表示）をカメラに映した状態で次の手順を行い、良い値に固定する。

  1. 今の状態（オートフォーカス・自動露出）での読み取り成功率とくっきり度を測る（比較の基準）
  2. ピント: オートフォーカスを切り、フォーカス値を粗く → 細かく振って、最もくっきり読める値を探す
  3. 露出: 自動露出を切り、露光時間を振って、白飛びせずコントラストが高く fps も落ちない値を探す
  4. 固定した状態で、画像補正（シャープ化など）のどれが効くかを調べる（読み取り処理の試行順に使う）

固定した結果が今の状態より明らかに悪い場合や、カメラが設定に対応していない（値を変えても画像が変わらない）
場合は、その項目は自動のままに戻す。OpenCV 以外（Qt）に依存しないので、擬似カメラでテストできる。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from .decoder import VARIANTS, decode_image, sharpness_score

FOCUS_COARSE = tuple(range(0, 241, 20)) + (255,)
FOCUS_FINE_SPAN, FOCUS_FINE_STEP = 15, 5
FOCUS_LIMITS = (0, 255)
# 露光時間（2 の累乗秒の指数。-5 = 1/32 秒）。DirectShow / Media Foundation 共通の単位
EXPOSURE_CANDIDATES = (-3, -4, -5, -6, -7, -8, -9, -10)
SETTLE_SEC = 0.35          # 設定を変えてからピント・明るさが落ち着くまで待つ時間
BASELINE_SETTLE_SEC = 1.0  # 自動（オートフォーカス・自動露出）に戻したときは長めに待つ
SAMPLES = 3                # 1 つの設定値あたりに測る画像の枚数
FINAL_SAMPLES = 8
WAIT_QR_SEC = 60.0
BLIND_AFTER_SEC = 3.0      # QR がこれだけ見つからなければ、フォーカスを振りながら探す
ROI_MARGIN = 0.3
KEEP_MARGIN = 5.0          # 固定した値のスコアがこれ以上悪くなければ、自動より固定を選ぶ（揺れを防ぐため）
SLOW_FPS_RATIO = 0.8       # 基準の fps のこの割合を下回る露出は減点する
HIGHLIGHT = 250            # 白飛びとみなす明るさ

AUTO_ON, AUTO_OFF = 1, 0   # CAP_PROP_AUTOFOCUS / CAP_PROP_AUTO_EXPOSURE の値（DirectShow / MSMF とも 0 以外 = 自動）


class Cancelled(Exception):
    pass


@dataclass
class Sample:
    """1 つの設定値で測った結果。"""
    label: str
    frames: int = 0
    decoded: int = 0
    sharpness: float = 0.0
    brightness: float = 0.0
    contrast: float = 0.0   # 0〜1（明るい側と暗い側の差）
    clipped: float = 0.0    # 白飛びした画素の割合
    fps: float = 0.0

    @property
    def rate(self) -> float:
        return self.decoded / self.frames if self.frames else 0.0

    def focus_score(self) -> float:
        return self.rate * 100 + self.sharpness

    def exposure_score(self, base_fps: float) -> float:
        s = self.rate * 100 + self.sharpness + 30 * self.contrast - 100 * self.clipped
        if base_fps > 0 and 0 < self.fps < base_fps * SLOW_FPS_RATIO:
            s -= 40
        return s

    def describe(self) -> str:
        return (f"{self.label}: 読取 {self.decoded}/{self.frames}, くっきり度 {self.sharpness:.1f}, "
                f"明るさ {self.brightness:.0f}, コントラスト {self.contrast:.2f}, 白飛び {self.clipped:.1%}, "
                f"{self.fps:.0f} fps")


@dataclass
class CameraState:
    autofocus: bool = True
    focus: int = -1               # -1 = 指定しない
    exposure: float | None = None  # None = 自動露出


@dataclass
class CalibrationResult:
    ok: bool
    message: str
    state: CameraState
    focus_supported: bool = False
    exposure_supported: bool = False
    before: Sample | None = None
    after: Sample | None = None
    variant_rates: dict[str, float] = field(default_factory=dict)
    best_variant: str | None = None
    log: list[str] = field(default_factory=list)


def roi_from_points(pts_list, w: int, h: int, margin: float = ROI_MARGIN) -> tuple[int, int, int, int]:
    xs = [x for pts in pts_list for x, _ in pts]
    ys = [y for pts in pts_list for _, y in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    mx, my = (x1 - x0) * margin + 8, (y1 - y0) * margin + 8
    return max(0, int(x0 - mx)), max(0, int(y0 - my)), min(w, int(x1 + mx)), min(h, int(y1 + my))


def _gray(frame: np.ndarray) -> np.ndarray:
    return frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _decoded(results) -> bool:
    return any(d is not None for _, d in results)


class Calibrator:
    """開いているカメラ（cv2.VideoCapture 互換）の設定を振って、最も読み取りやすい値に固定する。

    progress(text, 0〜1) で進み具合を、sink(frame) で読んだ画像を呼び出し元に渡す（プレビュー表示用）。
    """

    def __init__(self, cap, initial: CameraState | None = None, stop: threading.Event | None = None,
                 progress: Callable[[str, float], None] | None = None,
                 sink: Callable[[np.ndarray], None] | None = None, log: Callable[[str], None] | None = None,
                 settle_sec: float = SETTLE_SEC, baseline_settle_sec: float = BASELINE_SETTLE_SEC,
                 samples: int = SAMPLES, wait_qr_sec: float = WAIT_QR_SEC, clock: Callable[[], float] = time.monotonic):
        self.cap = cap
        self.initial = initial or CameraState()
        self.stop = stop
        self._progress = progress
        self.sink = sink
        self._log_fn = log
        self.settle_sec = settle_sec
        self.baseline_settle_sec = baseline_settle_sec
        self.samples = samples
        self.wait_qr_sec = wait_qr_sec
        self.clock = clock
        self.roi: tuple[int, int, int, int] | None = None
        self.log: list[str] = []
        self._step = 0
        self._steps = 1
        self._last_rois: list[np.ndarray] = []

    # --- 共通 ---------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.log.append(text)
        if self._log_fn:
            self._log_fn("calibration: " + text)

    def _check(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise Cancelled()

    def _report(self, text: str) -> None:
        if self._progress:
            self._progress(text, min(1.0, self._step / max(1, self._steps)))

    def _set(self, prop: int, value: float) -> bool:
        try:
            return bool(self.cap.set(prop, value))
        except cv2.error:
            return False

    def _read(self) -> np.ndarray | None:
        self._check()
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        if self.sink:
            self.sink(frame)
        return frame

    def _settle(self, sec: float) -> None:
        """設定変更後の画像を読み捨てる（最低 1 枚。カメラ内のバッファに古い画像が残っていることがあるため）。"""
        t0 = self.clock()
        self._read()
        while self.clock() - t0 < sec:
            self._read()

    def _measure(self, label: str, n: int, settle: float) -> Sample:
        self._settle(settle)
        s = Sample(label)
        sharp, bright, contrast, clipped = [], [], [], []
        times = []
        rois = []
        failures = 0
        while s.frames < n:
            frame = self._read()
            if frame is None:
                failures += 1
                if failures > 20:
                    break
                continue
            times.append(self.clock())
            x0, y0, x1, y1 = self.roi
            roi = np.ascontiguousarray(_gray(frame)[y0:y1, x0:x1])
            s.frames += 1
            if _decoded(decode_image(roi)):
                s.decoded += 1
            lo, hi = np.percentile(roi, (5, 95))
            sharp.append(sharpness_score(roi))
            bright.append(float(roi.mean()))
            contrast.append(float(hi - lo) / 255)
            clipped.append(float(np.count_nonzero(roi >= HIGHLIGHT)) / roi.size)
            rois.append(roi)
        if s.frames:
            s.sharpness = float(np.median(sharp))
            s.brightness = float(np.mean(bright))
            s.contrast = float(np.median(contrast))
            s.clipped = float(np.median(clipped))
        if len(times) >= 2 and times[-1] > times[0]:
            s.fps = (len(times) - 1) / (times[-1] - times[0])
        self._last_rois = rois
        self._step += 1
        self._log(s.describe())
        return s

    # --- QR を探す ------------------------------------------------------------
    def _find_qr(self, frame: np.ndarray) -> bool:
        gray = _gray(frame)
        strong = VARIANTS[1].fn  # 強いシャープ化（ボケていて素の画像では位置も取れない場合）
        found = decode_image(gray) or decode_image(np.ascontiguousarray(strong(gray)))
        if not found:
            return False
        h, w = gray.shape[:2]
        self.roi = roi_from_points([p for p, _ in found], w, h)
        self._log(f"QR を検出: ROI {self.roi}")
        return True

    def wait_for_qr(self) -> bool:
        """カメラに QR が映るまで待ち、その周辺を測定範囲（ROI）にする。

        ボケがひどくて QR の位置すら分からない場合に備え、しばらく見つからなければフォーカスを振りながら探す。
        """
        t0 = self.clock()
        text = "送信側の QR（待機画面）をカメラに映してください。映ると自動で調整を始めます…"
        can_focus = None
        while self.clock() - t0 < self.wait_qr_sec:
            if self.clock() - t0 < BLIND_AFTER_SEC or can_focus is False:
                self._report(text)
                frame = self._read()
                if frame is not None and self._find_qr(frame):
                    return True
                continue
            # フォーカスを振って探す（見つかったフォーカス値のまま、次の手順に進む）
            for v in FOCUS_COARSE:
                if self.clock() - t0 >= self.wait_qr_sec:
                    break
                ok = self._set_focus(v)
                if can_focus is None:
                    can_focus = ok
                    if not ok:
                        self._log("QR が見つからず、フォーカスも変えられないため待ち続けます")
                        self._restore_focus()
                        break
                self._report(text + f"\n（ピントを変えながら QR を探しています: フォーカス {v}）")
                self._settle(self.settle_sec)
                frame = self._read()
                if frame is not None and self._find_qr(frame):
                    self._log(f"フォーカス {v} で QR を検出")
                    return True
            else:
                self._restore_focus()
        if can_focus:
            self._restore_focus()
        return False

    # --- ピント ---------------------------------------------------------------
    def _set_focus(self, value: int) -> bool:
        self._set(cv2.CAP_PROP_AUTOFOCUS, AUTO_OFF)
        return self._set(cv2.CAP_PROP_FOCUS, value)

    def _restore_focus(self) -> None:
        if self.initial.autofocus:
            self._set(cv2.CAP_PROP_AUTOFOCUS, AUTO_ON)
        elif self.initial.focus >= 0:
            self._set_focus(self.initial.focus)

    def calibrate_focus(self, baseline: Sample) -> tuple[bool, int | None]:
        """戻り値: (カメラが対応しているか, 固定したフォーカス値 / None = 元の状態のまま)"""
        if not self._set_focus(FOCUS_COARSE[0]):
            self._log("フォーカスの設定に対応していません")
            self._restore_focus()
            self._step += len(FOCUS_COARSE) + 6
            return False, None
        measured: dict[int, Sample] = {}

        def run(values) -> None:
            for v in values:
                if v in measured:
                    continue
                self._report(f"ピントを調整しています（フォーカス {v}）…")
                self._set_focus(v)
                measured[v] = self._measure(f"focus {v}", self.samples, self.settle_sec)

        run(FOCUS_COARSE)
        sharp = [s.sharpness for s in measured.values()]
        rates = {s.decoded for s in measured.values()}
        if max(sharp) < min(sharp) * 1.1 + 0.5 and len(rates) <= 1:
            self._log("フォーカス値を変えても画像が変わりません（非対応とみなします）")
            self._restore_focus()
            self._step += 6
            return False, None
        best = max(measured, key=lambda v: measured[v].focus_score())
        lo, hi = FOCUS_LIMITS
        run([v for v in range(best - FOCUS_FINE_SPAN, best + FOCUS_FINE_SPAN + 1, FOCUS_FINE_STEP) if lo <= v <= hi])
        best = max(measured, key=lambda v: measured[v].focus_score())
        chosen = measured[best]
        if chosen.focus_score() >= baseline.focus_score() - KEEP_MARGIN:
            self._set_focus(best)
            self._log(f"フォーカスを {best} に固定")
            return True, best
        self._log(f"固定すると悪くなるため元に戻します（最良 {best}: {chosen.focus_score():.1f} < "
                  f"基準 {baseline.focus_score():.1f}）")
        self._restore_focus()
        return True, None

    # --- 露出 -----------------------------------------------------------------
    def _set_exposure(self, value: float) -> bool:
        self._set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_OFF)
        return self._set(cv2.CAP_PROP_EXPOSURE, value)

    def _restore_exposure(self) -> None:
        if self.initial.exposure is None:
            self._set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_ON)
        else:
            self._set_exposure(self.initial.exposure)

    def calibrate_exposure(self) -> tuple[bool, float | None]:
        """戻り値: (カメラが対応しているか, 固定した露出 / None = 自動露出)"""
        # 比較の基準: 自動露出（ピントは固定済み）
        self._set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_ON)
        self._report("露出を調整しています（自動露出）…")
        auto = self._measure("auto exposure", self.samples, self.baseline_settle_sec)
        if not self._set_exposure(EXPOSURE_CANDIDATES[0]):
            self._log("露出の設定に対応していません")
            self._restore_exposure()
            self._step += len(EXPOSURE_CANDIDATES)
            return False, None
        measured: dict[float, Sample] = {}
        for v in EXPOSURE_CANDIDATES:
            self._report(f"露出を調整しています（露光時間 {exposure_label(v)}）…")
            self._set_exposure(v)
            measured[v] = self._measure(f"exposure {v}", self.samples, self.settle_sec)
        bright = [s.brightness for s in measured.values()]
        if max(bright) - min(bright) < 8:
            self._log("露出を変えても明るさが変わりません（非対応とみなします）")
            self._restore_exposure()
            return False, None
        # スコアが同じなら露光時間が短い方（画面の切り替わりをまたぎにくく、fps も落ちにくい）
        best = max(measured, key=lambda v: (round(measured[v].exposure_score(auto.fps), 1), -v))
        score, auto_score = measured[best].exposure_score(auto.fps), auto.exposure_score(auto.fps)
        if score >= auto_score - KEEP_MARGIN:
            self._set_exposure(best)
            self._log(f"露出を {best} に固定（{score:.1f} / 自動 {auto_score:.1f}）")
            return True, float(best)
        self._log(f"固定すると悪くなるため自動露出にします（最良 {best}: {score:.1f} < 自動 {auto_score:.1f}）")
        self._set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_ON)
        return True, None

    # --- 画像補正 --------------------------------------------------------------
    def evaluate_variants(self, rois: list[np.ndarray]) -> dict[str, float]:
        rates = {"plain": 0.0}
        if not rois:
            return rates
        rates["plain"] = sum(_decoded(decode_image(r)) for r in rois) / len(rois)
        for v in VARIANTS:
            self._check()
            rates[v.name] = sum(_decoded(decode_image(np.ascontiguousarray(v.fn(r)))) for r in rois) / len(rois)
        return rates

    # --- 全体 -----------------------------------------------------------------
    def run(self) -> CalibrationResult:
        state = CameraState(self.initial.autofocus, self.initial.focus, self.initial.exposure)
        try:
            if not self.wait_for_qr():
                return CalibrationResult(False, "QR が見つからなかったため、調整できませんでした。送信側で待機画面"
                                         "（最初の QR）を表示し、カメラに映してから「ピント・露出を自動調整」を"
                                         "押してください。", state, log=self.log)
            self._steps = 1 + len(FOCUS_COARSE) + 6 + 1 + len(EXPOSURE_CANDIDATES) + 1
            self._report("今の状態を測っています…")
            before = self._measure("before", FINAL_SAMPLES, self.baseline_settle_sec)

            focus_ok, focus = self.calibrate_focus(before)
            if focus is not None:
                state.autofocus, state.focus = False, focus
            exposure_ok, exposure = self.calibrate_exposure()
            state.exposure = exposure

            self._report("調整後の状態を確かめています…")
            after = self._measure("after", FINAL_SAMPLES, self.settle_sec)
            rates = self.evaluate_variants(self._last_rois)
            best_variant = None
            if rates.get("plain", 0) < 1.0:
                name = max((v.name for v in VARIANTS), key=lambda k: rates[k])
                if rates[name] > rates["plain"]:
                    best_variant = name
        except Cancelled:
            self._restore_focus()
            self._restore_exposure()
            return CalibrationResult(False, "調整を中止しました（元の設定に戻しました）", self.initial, log=self.log)
        result = CalibrationResult(True, "", state, focus_ok, exposure_ok, before, after, rates, best_variant,
                                   self.log)
        result.message = summarize(result)
        self._log(result.message.replace("\n", " / "))
        return result


def exposure_label(v: float) -> str:
    return f"1/{2 ** -v:.0f} 秒" if v < 0 else f"{2 ** v:.0f} 秒"


def summarize(r: CalibrationResult) -> str:
    s = r.state
    if s.autofocus:
        focus = "オートフォーカスのまま" + ("（固定しても良くならないため）" if r.focus_supported else "（カメラが手動フォーカスに非対応）")
    else:
        focus = f"{s.focus} に固定"
    if s.exposure is None:
        exposure = "自動のまま" + ("（固定しても良くならないため）" if r.exposure_supported else "（カメラが露出の設定に非対応）")
    else:
        exposure = f"{exposure_label(s.exposure)} に固定"
    lines = [f"ピント: {focus}　露出: {exposure}"]
    if r.before is not None and r.after is not None:
        lines.append(f"読み取り成功率 {r.before.rate:.0%} → {r.after.rate:.0%}　"
                     f"くっきり度 {r.before.sharpness:.0f} → {r.after.sharpness:.0f}")
    if r.best_variant:
        label = next(v.label for v in VARIANTS if v.name == r.best_variant)
        lines.append(f"画像補正「{label}」がよく効くため、読み取り時に最初に試します")
    return "\n".join(lines)
