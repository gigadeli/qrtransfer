"""カメラの接続方式の自動選択（open_best）のテスト。実カメラの代わりに、OpenCV DirectShow の挙動を真似た擬似カメラを使う。"""

import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("PySide6.QtCore")

from qrtransfer import camera  # noqa: E402

MJPG = cv2.VideoWriter_fourcc(*"MJPG")
YUY2 = cv2.VideoWriter_fourcc(*"YUY2")
NV12 = cv2.VideoWriter_fourcc(*"NV12")


class FakeCam:
    """OpenCV DirectShow の既知の挙動を再現する擬似カメラ。

    - 解像度を設定すると撮影形式が選び直され、直前に指定した MJPG は失われて YUY2 になる
    - YUY2 は 1080p で 5fps、720p で 10fps しか出ない（USB 帯域の制限）。MJPG は 30fps
    - MSMF は自前で最適な形式（NV12 30fps）を選ぶ。msmf_fps で速度を変えられる
    """

    opened_count = 0

    def __init__(self, index, backend, msmf_fps=30.0, dshow_ok=True, msmf_ok=True, max_res=(1920, 1080),
                 mjpg_last_works=True):
        FakeCam.opened_count += 1
        self.backend = backend
        self.ok = (dshow_ok if backend == cv2.CAP_DSHOW else msmf_ok)
        self.w, self.h = 640, 480
        self.fourcc = YUY2 if backend == cv2.CAP_DSHOW else NV12
        self.msmf_fps = msmf_fps
        self.max_res = max_res
        self.mjpg_last_works = mjpg_last_works
        self.released = False

    def isOpened(self):
        return self.ok

    def set(self, prop, value):
        if prop in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                self.w = min(int(value), self.max_res[0])
            else:
                self.h = min(int(value), self.max_res[1])
            if self.backend == cv2.CAP_DSHOW:
                self.fourcc = YUY2  # 解像度の設定で形式が選び直される（MJPG が失われる）
        elif prop == cv2.CAP_PROP_FOURCC and self.backend == cv2.CAP_DSHOW:
            if int(value) == MJPG and self.mjpg_last_works:
                self.fourcc = MJPG
        return True

    def get(self, prop):
        return {cv2.CAP_PROP_FRAME_WIDTH: self.w, cv2.CAP_PROP_FRAME_HEIGHT: self.h,
                cv2.CAP_PROP_FOURCC: self.fourcc}.get(prop, -1)

    def read(self):
        return True, None

    def fps(self):
        if self.backend == cv2.CAP_MSMF:
            return self.msmf_fps
        if self.fourcc == MJPG:
            return 30.0
        return 5.0 if self.w >= 1920 else 10.0

    def release(self):
        self.released = True


def fake_measure(cap, stop=None):
    return cap.fps()


def run(factory_kw=None, **kw):
    made = []

    def factory(index, backend):
        cam = FakeCam(index, backend, **(factory_kw or {}))
        made.append(cam)
        return cam

    cap, mode, tried = camera.open_best(0, 1920, 1080, factory=factory, measure=fake_measure, **kw)
    return cap, mode, tried, made


def test_old_order_loses_mjpg():
    """修正前の順序（MJPG → 解像度）では YUY2 になり 5fps しか出ないことの再現。"""
    cap, mode, tried, _ = run(strategy="dshow_mjpg_first")
    assert mode.fourcc == "YUY2" and mode.fps == 5.0


def test_auto_picks_fast_mode_and_stops_early():
    cap, mode, tried, made = run()
    assert mode.strategy == "dshow_mjpg_last" and mode.fourcc == "MJPG" and mode.fps == 30.0
    assert len(tried) == 1 and len(made) == 1  # 十分速いので他は試さない
    assert not made[0].released and cap is made[0]


def test_auto_falls_back_to_msmf_when_dshow_mjpg_fails():
    cap, mode, tried, made = run({"mjpg_last_works": False})
    assert mode.strategy == "msmf" and mode.fps == 30.0
    assert [m.strategy for m in tried] == ["dshow_mjpg_last", "msmf"]
    assert made[0].released and not made[1].released  # 同じカメラは閉じてから次を試す


def test_auto_reopens_best_when_all_are_slow():
    cap, mode, tried, made = run({"mjpg_last_works": False, "msmf_fps": 15.0})
    assert mode.strategy == "msmf" and mode.fps == 15.0
    assert len(tried) == len(camera.STRATEGY_KEYS)
    assert cap is made[-1] and cap.backend == cv2.CAP_MSMF  # 最良の方式で開き直している
    assert all(c.released for c in made[:-1])


def test_preferred_strategy_is_tried_first():
    cap, mode, tried, made = run(preferred="msmf")
    assert tried[0].strategy == "msmf" and mode.strategy == "msmf" and len(made) == 1


def test_fixed_strategy_only():
    cap, mode, tried, made = run(strategy="dshow_default")
    assert [m.strategy for m in tried] == ["dshow_default"] and mode.fps == 5.0


def test_resolution_mismatch_is_penalized():
    # DSHOW は 1080p に対応せず 720p になるが MJPG 30fps、MSMF は 1080p 20fps → 解像度が合う MSMF を選ぶ
    made = []

    def factory(index, backend):
        cam = FakeCam(index, backend, msmf_fps=20.0,
                      max_res=(1280, 720) if backend == cv2.CAP_DSHOW else (1920, 1080))
        made.append(cam)
        return cam

    cap, mode, tried = camera.open_best(0, 1920, 1080, factory=factory, measure=fake_measure)
    assert mode.strategy == "msmf" and (mode.width, mode.height) == (1920, 1080)


def test_no_camera():
    cap, mode, tried, made = run({"dshow_ok": False, "msmf_ok": False})
    assert cap is None and mode is None and tried == []


def test_stop_event_aborts():
    import threading
    ev = threading.Event()
    ev.set()
    cap, mode, tried, made = run(stop=ev)
    assert cap is None and made == []


def test_fourcc_to_str():
    assert camera.fourcc_to_str(MJPG) == "MJPG"
    assert camera.fourcc_to_str(0) == "?" and camera.fourcc_to_str(-1) == "?"


def test_preview_is_downscaled():
    import numpy as np
    img = camera.bgr_to_qimage(np.zeros((1080, 1920, 3), np.uint8), camera.PREVIEW_MAX_WIDTH)
    assert img.width() == camera.PREVIEW_MAX_WIDTH and img.height() == 540


def test_camera_fps_warning():
    pytest.importorskip("PySide6.QtWidgets")
    from qrtransfer.ui.receiver_view import ReceiverView
    slow = camera.CameraMode("dshow_default", "DSHOW", 1920, 1080, "YUY2", 5.0)
    fast = camera.CameraMode("dshow_mjpg_last", "DSHOW", 1920, 1080, "MJPG", 30.0)
    assert "接続方式" in ReceiverView.camera_fps_warning(slow)
    assert ReceiverView.camera_fps_warning(fast) == ""


def test_strategy_settings(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from qrtransfer.settings import Settings
    s = Settings(tmp_path / "s.ini")
    assert s.camera_strategy == "auto" and s.preferred_strategy(0, 1920, 1080) is None
    s.set_preferred_strategy(0, 1920, 1080, "msmf")
    s.camera_strategy = "dshow_mjpg_last"
    s.sync()
    s2 = Settings(tmp_path / "s.ini")
    assert s2.preferred_strategy(0, 1920, 1080) == "msmf" and s2.preferred_strategy(1, 1920, 1080) is None
    assert s2.camera_strategy == "dshow_mjpg_last"
    s2.camera_strategy = "bogus"
    assert s2.camera_strategy == "auto"
