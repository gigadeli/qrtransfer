"""ボケた撮影画像に対する読み取り補正（RobustDecoder）のテスト。カメラは使わず、撮影を模した画像で確かめる。"""

import os
import time

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from qrtransfer import packer, protocol, qrgen  # noqa: E402
from qrtransfer.decoder import RobustDecoder, decode_image, process_image, sharpness_score  # noqa: E402

from conftest import make_assembler  # noqa: E402

W, H = 1280, 720
QR_PX = 560  # バージョン 23（117 モジュール）で 1 セル約 4.8px


def camera_shot(gray_qr: np.ndarray, sigma: float, seed: int = 0, qr_px: int = QR_PX) -> np.ndarray:
    """画面の QR をカメラで撮ったような画像: 射影歪み・ボケ・コントラスト低下・明るさムラ・ノイズ・JPEG。"""
    rng = np.random.default_rng(seed)
    g = cv2.resize(gray_qr, (qr_px, qr_px), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((H, W), 255, np.uint8)
    y, x = (H - qr_px) // 2, (W - qr_px) // 2
    canvas[y:y + qr_px, x:x + qr_px] = g
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    d = 25
    dst = np.float32([[d, d * 0.6], [W - d * 0.4, 0], [W - d, H - d * 0.3], [0, H]])
    img = cv2.warpPerspective(canvas, cv2.getPerspectiveTransform(src, dst), (W, H), borderValue=200)
    img = cv2.GaussianBlur(img, (0, 0), sigma)
    yy, xx = np.mgrid[0:H, 0:W]
    shade = 1.0 - 0.25 * ((xx - W * 0.7) ** 2 + (yy - H * 0.3) ** 2) / (W ** 2)
    img = img.astype(np.float32) * 0.7 * shade + 40 + rng.normal(0, 5, (H, W))
    img = np.clip(img, 0, 255).astype(np.uint8)
    _, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)


@pytest.fixture(scope="module")
def qr_set():
    n = 24
    frames = [protocol.build_data_frame(7, i, n, os.urandom(800)) for i in range(n)]
    v = qrgen.choose_version(max(map(len, frames)), 0, "m")
    cache = qrgen.generate_cache(frames, v, "m", workers=1)
    return frames, cache


def _rate(frames, shots, decode) -> float:
    ok = [frames[i] in [d for _, d in decode(s) if d is not None] for i, s in enumerate(shots)]
    return sum(ok) / len(ok)


def test_sharpening_rescues_moderate_blur(qr_set):
    frames, cache = qr_set
    shots = [camera_shot(cache.gray(i), 2.2, seed=i) for i in range(len(frames))]
    plain = _rate(frames, shots, decode_image)
    dec = RobustDecoder()
    robust = _rate(frames, shots, dec.decode)
    assert plain < 0.6, plain  # 補正なしでは半分以上落ちる条件であること
    assert robust >= 0.95, robust
    assert dec.stats.rescued >= len(frames) * 0.4


def test_strong_blur_is_improved(qr_set):
    frames, cache = qr_set
    shots = [camera_shot(cache.gray(i), 2.6, seed=100 + i) for i in range(len(frames))]
    assert _rate(frames, shots, decode_image) < 0.1
    assert _rate(frames, shots, RobustDecoder().decode) >= 0.8


def test_sharp_image_uses_plain_pass_only(qr_set):
    frames, cache = qr_set
    dec = RobustDecoder()
    for i in range(5):
        out = dec.decode(camera_shot(cache.gray(i), 0.8, seed=i))
        assert frames[i] in [d for _, d in out]
        assert dec.last_variant == "plain"
    assert dec.stats.rescued == 0 and dec.stats.frames_ok == 5


def test_enhance_off_matches_plain(qr_set):
    frames, cache = qr_set
    shots = [camera_shot(cache.gray(i), 2.2, seed=i) for i in range(8)]
    dec = RobustDecoder(enhance=False)
    assert _rate(frames[:8], shots, dec.decode) == _rate(frames[:8], shots, decode_image)
    assert dec.stats.rescued == 0


def test_points_are_mapped_back_to_camera_coordinates(qr_set):
    frames, cache = qr_set
    dec = RobustDecoder()
    shots = [camera_shot(cache.gray(i), 2.4, seed=i) for i in range(6)]
    got_rescued = False
    for s in shots:
        out = dec.decode(s)
        for pts, data in out:
            if data is None:
                continue
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            # QR は画面中央の約 560px 四方にある
            assert 250 < min(xs) < 520 and 760 < max(xs) < 1030, xs
            assert 20 < min(ys) < 200 and 520 < max(ys) < 700, ys
            got_rescued |= dec.last_variant != "plain"
    assert got_rescued


def test_successful_variant_is_tried_first(qr_set):
    frames, cache = qr_set
    dec = RobustDecoder()
    for i in range(6):
        dec.decode(camera_shot(cache.gray(i), 2.6, seed=200 + i))
    first = dec._ordered_variants()[0].name
    assert dec.stats.by_variant.get(first, 0) > 0


def test_time_budget_without_qr():
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 255, (1080, 1920), dtype=np.uint8)
    dec = RobustDecoder()
    t = time.perf_counter()
    for _ in range(3):
        assert all(d is None for _, d in dec.decode(noise))
    per_frame = (time.perf_counter() - t) / 3
    assert per_frame < 0.5, per_frame  # 1 回の素の読み取り＋補正の上限時間程度に収まる


def test_blurred_transfer_completes_end_to_end(tmp_path):
    f = tmp_path / "blur.bin"
    f.write_bytes(os.urandom(6000))
    plan = packer.make_plan([f], chunk_size=800)
    v = qrgen.choose_version(plan.max_data_frame_size, plan.min_meta_frame_size(), "m")
    frames = [plan.meta_frame(qrgen.capacity(v, "m"))] + [plan.data_frame(i) for i in range(plan.total)]
    cache = qrgen.generate_cache(frames, v, "m", workers=1)
    asm, results = make_assembler(tmp_path)
    dec = RobustDecoder()
    for rnd in range(3):
        for i in range(len(frames)):
            process_image(camera_shot(cache.gray(i), 2.3, seed=rnd * 100 + i), asm, dec)
        if results:
            break
    assert results and results[0].ok
    assert results[0].path.read_bytes() == f.read_bytes()


def test_sharpness_score_orders_blur(qr_set):
    _, cache = qr_set
    scores = [sharpness_score(camera_shot(cache.gray(0), s)[100:620, 360:920]) for s in (0.8, 1.8, 2.8)]
    assert scores[0] > scores[1] > scores[2]
    assert sharpness_score(np.full((50, 50), 128, np.uint8)) == 0.0


def test_reading_hint():
    pytest.importorskip("PySide6.QtWidgets")
    from qrtransfer.decoder import DecodeStats
    from qrtransfer.ui.receiver_view import ReceiverView
    assert ReceiverView.reading_hint(DecodeStats(frames=40, frames_ok=30, frames_detected_bad=5)) == ""
    assert "フォーカス" in ReceiverView.reading_hint(DecodeStats(frames=40, frames_ok=5, frames_detected_bad=20))
    assert ReceiverView.reading_hint(DecodeStats(frames=3, frames_ok=0, frames_detected_bad=3)) == ""
