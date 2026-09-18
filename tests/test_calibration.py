"""カメラの自動調整（Calibrator）のテスト。フォーカス値でボケ具合、露出で明るさが変わる擬似カメラを使う。"""

import os
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

cv2 = pytest.importorskip("cv2")
pytest.importorskip("PySide6.QtCore")

from qrtransfer import calibration, protocol, qrgen  # noqa: E402
from qrtransfer.calibration import CameraState, Calibrator  # noqa: E402

W, H = 640, 480
MODULE_PX = 3


@pytest.fixture(scope="module")
def screen():
    """カメラに映る画面（白地に QR 1 つ）。"""
    frame = protocol.build_data_frame(1, 0, 1, os.urandom(300))
    v = qrgen.choose_version(len(frame), 0, "m")
    gray = qrgen.generate_cache([frame], v, "m", workers=1).gray(0)
    n = gray.shape[0] * MODULE_PX
    g = cv2.resize(gray, (n, n), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((H, W), 255, np.uint8)
    y, x = (H - n) // 2, (W - n) // 2
    canvas[y:y + n, x:x + n] = g
    return canvas


class FocusCam:
    """ピントは best_focus で最もくっきりし、離れるほどボケる。オートフォーカスは中途半端（ややボケ）。
    露出は -6 で適正、自動露出は明るすぎて白飛びする。read() 1 回で時計が 1/30 秒進む。"""

    def __init__(self, screen, best_focus=133, focus=True, exposure=True, focus_effect=True, qr=True, af_sigma=1.6):
        self.screen = screen if qr else np.full_like(screen, 200)
        self.best_focus = best_focus
        self.can_focus, self.can_exposure, self.focus_effect = focus, exposure, focus_effect
        self.af_sigma = af_sigma
        self.af, self.focus, self.ae, self.exposure = 1, 0, 1, -5.0
        self.t = 0.0
        self.reads = 0
        self._cache = {}

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_AUTOFOCUS:
            self.af = int(value)
            return self.can_focus
        if prop == cv2.CAP_PROP_FOCUS:
            if not self.can_focus:
                return False
            self.focus = int(value)
            return True
        if prop == cv2.CAP_PROP_AUTO_EXPOSURE:
            self.ae = int(value)
            return self.can_exposure
        if prop == cv2.CAP_PROP_EXPOSURE:
            if not self.can_exposure:
                return False
            self.exposure = float(value)
            return True
        return False

    def get(self, prop):
        return {cv2.CAP_PROP_FOCUS: self.focus, cv2.CAP_PROP_EXPOSURE: self.exposure}.get(prop, -1)

    def sigma(self):
        if self.af or not self.can_focus or not self.focus_effect:
            return self.af_sigma
        return 0.3 + abs(self.focus - self.best_focus) / 12

    def gain(self):
        if self.ae or not self.can_exposure:
            return 1.6
        return 2 ** (self.exposure + 6)

    def read(self):
        self.t += 1 / 30
        self.reads += 1
        key = (round(self.sigma(), 2), round(self.gain(), 3))
        if key not in self._cache:
            img = cv2.GaussianBlur(self.screen, (0, 0), key[0]).astype(np.float32)
            self._cache[key] = np.clip(img * 0.8 * key[1] + 30, 0, 255).astype(np.uint8)
        return True, self._cache[key]


def calibrate(cam, **kw):
    kw.setdefault("initial", CameraState())
    return Calibrator(cam, settle_sec=0, baseline_settle_sec=0, clock=lambda: cam.t, **kw).run()


def test_locks_best_focus_and_exposure(screen):
    cam = FocusCam(screen)
    r = calibrate(cam)
    assert r.ok and r.focus_supported and r.exposure_supported
    assert not r.state.autofocus and abs(r.state.focus - 133) <= 5, r.log  # 粗い探索(20 刻み)の後に細かく詰める
    assert r.state.exposure == -6, r.log
    assert (cam.af, cam.focus, cam.ae, cam.exposure) == (0, r.state.focus, 0, -6)  # カメラにも固定されている
    assert r.before.rate < 0.5 and r.after.rate == 1.0
    assert r.after.sharpness > r.before.sharpness
    assert "固定" in r.message


def test_unsupported_camera_keeps_auto(screen):
    cam = FocusCam(screen, focus=False, exposure=False)
    r = calibrate(cam)
    assert r.ok and not r.focus_supported and not r.exposure_supported
    assert r.state.autofocus and r.state.exposure is None
    assert cam.af == 1 and cam.ae == 1
    assert "非対応" in r.message


def test_focus_without_effect_is_treated_as_unsupported(screen):
    cam = FocusCam(screen, focus_effect=False)
    r = calibrate(cam)
    assert not r.focus_supported and r.state.autofocus and cam.af == 1
    assert r.state.exposure == -6  # 露出は調整できる


def test_no_qr(screen):
    cam = FocusCam(screen, qr=False)
    r = calibrate(cam, wait_qr_sec=1.0)
    assert not r.ok and "QR が見つからなかった" in r.message
    assert (cam.af, cam.ae) == (1, 1)  # 何も変えていない


def test_cancel_restores_initial_state(screen):
    cam = FocusCam(screen)
    stop = threading.Event()
    seen = []

    def sink(frame):
        seen.append(1)
        if len(seen) == 30:  # ピントを探している途中で中止
            stop.set()

    r = calibrate(cam, stop=stop, sink=sink, initial=CameraState(True, -1, None))
    assert not r.ok and "中止" in r.message
    assert cam.af == 1 and cam.ae == 1


def test_keeps_previous_manual_state_on_cancel(screen):
    cam = FocusCam(screen)
    stop = threading.Event()
    stop.set()
    r = calibrate(cam, stop=stop, initial=CameraState(False, 90, -7.0))
    assert not r.ok and (cam.af, cam.focus, cam.ae, cam.exposure) == (0, 90, 0, -7.0)


def test_progress_reaches_end(screen):
    cam = FocusCam(screen)
    fracs = []
    calibrate(cam, progress=lambda text, f: fracs.append(f))
    assert fracs and max(fracs) <= 1.0 and max(fracs) > 0.8
    assert fracs == sorted(fracs)


def test_exposure_label():
    assert calibration.exposure_label(-6) == "1/64 秒"
    assert calibration.exposure_label(-3) == "1/8 秒"


def test_settings_roundtrip(tmp_path):
    from qrtransfer.settings import Settings
    s = Settings(tmp_path / "s.ini")
    assert s.exposure is None and s.calibrate_on_start
    s.exposure = -7.0
    s.calibrate_on_start = False
    s.sync()
    s2 = Settings(tmp_path / "s.ini")
    assert s2.exposure == -7.0 and not s2.calibrate_on_start
    s2.exposure = None
    assert s2.exposure is None


def test_receiver_applies_result(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    from qrtransfer.settings import Settings
    from qrtransfer.ui.receiver_view import ReceiverView
    s = Settings(tmp_path / "s.ini")
    s.output_dir = str(tmp_path / "out")
    view = ReceiverView(s)
    try:
        view._on_calibration_progress("ピントを調整しています", 0.3)
        assert view.is_calibrating() and view.btn_calib.text() == "調整を中止"
        assert not view.chk_af.isEnabled() and "ピント" in view.preview.overlay
        state = CameraState(False, 120, -6.0)
        r = calibration.CalibrationResult(True, "", state, True, True)
        r.message = calibration.summarize(r)
        view._on_calibration_done(r)
        assert not view.is_calibrating() and view.preview.overlay == ""
        assert not view.chk_af.isChecked() and view.slider_focus.value() == 120
        assert s.manual_focus == 120 and not s.autofocus and s.exposure == -6.0
        assert "1/64" in view.lbl_exposure.text() and "調整完了" in view.lbl_calib.text()
        view.reset_calibration()
        assert view.chk_af.isChecked() and s.autofocus and s.exposure is None
        assert view.lbl_exposure.text() == "露出: 自動"
    finally:
        view.shutdown()


def test_finds_qr_by_sweeping_focus_when_too_blurry(screen):
    """オートフォーカスではボケすぎて QR の位置すら分からないカメラでも、フォーカスを振って見つけ、調整できる。"""
    cam = FocusCam(screen, af_sigma=4.0)
    r = calibrate(cam)
    assert r.ok and abs(r.state.focus - 133) <= 5, r.log
    assert any("で QR を検出" in line for line in r.log)
    assert r.after.rate == 1.0


def test_camera_thread_calibrates_on_start_and_restores_auto_on_close(screen, monkeypatch, tmp_path):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QEventLoop, QTimer
    from qrtransfer import camera
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cam = FocusCam(screen)
    cam.release = lambda: setattr(cam, "released", True)
    mode = camera.CameraMode("dshow_mjpg_last", "DSHOW", W, H, "MJPG", 30.0)
    monkeypatch.setattr(camera, "open_best", lambda *a, **k: (cam, mode, [mode]))
    th = camera.CameraThread(0, W, H, autofocus=True, calibrate_on_start=True)
    th.calibrator_options = {"settle_sec": 0, "baseline_settle_sec": 0, "clock": lambda: cam.t}
    results, progress = [], []
    loop = QEventLoop()
    th.calibration_progress.connect(lambda t, f: progress.append(f))
    th.calibration_done.connect(lambda r: (results.append(r), loop.quit()))
    QTimer.singleShot(20000, loop.quit)
    th.start()
    loop.exec()
    try:
        assert results and results[0].ok, results
        assert progress
        assert not th.autofocus and abs(th.manual_focus - 133) <= 5 and th.exposure == -6
        _, frame = th.source.get_latest(0)
        assert frame is not None  # 調整中の画像も読み取りに回している
    finally:
        th.stop()
        th.wait(5000)
    assert cam.released and cam.af == 1 and cam.ae == 1  # 閉じる前に自動へ戻している
