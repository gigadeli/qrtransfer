"""QR を画像化して zxing-cpp で読む往復テスト（カメラは使わない）。"""

import io
import os

import numpy as np
import pytest
import segno
import zxingcpp
from PIL import Image, ImageFilter

from qrtransfer import packer, protocol, qrgen

from conftest import make_assembler, read_tree, write_tree


def _decode_all(gray: np.ndarray) -> list[bytes]:
    results = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode)
    return [r.bytes for r in results if r.valid]


def test_binary_bytes_survive_png_roundtrip():
    # 全バイト値・UTF-8 として不正な並び・NUL を含む
    data = bytes(range(256)) + b"\xef\xbb\xbf\xff\xfe\x00\x00\xc3\x28\x80" + os.urandom(500)
    qr = segno.make_qr(data, error="m", mode="byte", boost_error=False)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=4, border=4)
    buf.seek(0)
    gray = np.array(Image.open(buf).convert("L"))
    assert _decode_all(gray) == [data]


@pytest.mark.parametrize("ecc", ["l", "m", "q", "h"])
def test_frame_roundtrip_every_ecc(ecc):
    payload = os.urandom(600)
    frame = protocol.build_data_frame(0x12345678, 3, 10, payload)
    v = qrgen.min_version(len(frame), ecc)
    gray = qrgen.matrix_to_gray(qrgen.make_matrix(frame, v, ecc), scale=4)
    got = _decode_all(gray)
    assert len(got) == 1
    parsed = protocol.parse_frame(got[0])
    assert parsed is not None and parsed.payload == payload


def test_capacity_matches_segno():
    for ecc in qrgen.ECC_LEVELS:
        for v in (1, 9, 10, 26, 27, 40):
            cap = qrgen.capacity(v, ecc)
            segno.make_qr(bytes(cap), error=ecc, mode="byte", version=v, boost_error=False)
            with pytest.raises(segno.DataOverflowError):
                segno.make_qr(bytes(cap + 1), error=ecc, mode="byte", version=v, boost_error=False)


def test_degraded_image_still_decodes():
    frame = protocol.build_data_frame(1, 0, 1, os.urandom(800))
    v = qrgen.min_version(len(frame), "m")
    gray = qrgen.matrix_to_gray(qrgen.make_matrix(frame, v, "m"), scale=6)
    img = Image.fromarray(gray).rotate(7, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(1.2))
    img = img.resize((int(img.width * 0.8), int(img.height * 0.8)))
    arr = np.array(img)
    arr = np.clip(arr.astype(np.int16) * 0.7 + 40 + np.random.default_rng(1).normal(0, 6, arr.shape), 0, 255)
    got = _decode_all(arr.astype(np.uint8))
    assert got and protocol.parse_frame(got[0]) is not None


def test_generate_cache_serial_and_parallel():
    frames = [protocol.build_data_frame(9, i, 80, os.urandom(300)) for i in range(80)]
    v = qrgen.choose_version(max(map(len, frames)), 0, "m")
    serial = qrgen.generate_cache(frames, v, "m", workers=1)
    parallel = qrgen.generate_cache(frames, v, "m", workers=2)
    assert serial.packed == parallel.packed
    assert serial.matrix(5).shape == (serial.size, serial.size)
    got = _decode_all(serial.gray(5, scale=3))
    assert got == [frames[5]]


@pytest.mark.parametrize("kind", ["file", "bundle", "empty"])
def test_end_to_end_through_images(tmp_path, rng, kind):
    """送信側のフレーム列 → 統一バージョンの QR 画像 → zxing-cpp → Assembler → SHA-256 一致。"""
    src = tmp_path / "src"
    if kind == "file":
        path = src / "画像テスト.bin"
        write_tree(src, {path.name: os.urandom(3000) + b"z" * 2000})
        inputs = [path]
    elif kind == "bundle":
        write_tree(src / "dir", {"a.txt": "あいう".encode() * 500, "b/c.bin": os.urandom(1500)},
                   empty_dirs=["e"])
        inputs = [src / "dir"]
    else:
        write_tree(src, {"zero.dat": b""})
        inputs = [src / "zero.dat"]

    plan = packer.make_plan(inputs, chunk_size=400)
    ecc = "m"
    v = qrgen.choose_version(plan.max_data_frame_size, plan.min_meta_frame_size(), ecc)
    cap = qrgen.capacity(v, ecc)
    order = packer.carousel_order(plan.total)
    frames = [plan.meta_frame(cap) if s == packer.CAROUSEL_META else plan.data_frame(s) for s in order]
    cache = qrgen.generate_cache(frames, v, ecc, workers=1)
    assert all(len(f) <= cap for f in frames)

    asm, results = make_assembler(tmp_path)
    idx = list(range(len(frames)))
    for _ in range(5):
        rng.shuffle(idx)
        for i in idx:
            if rng.random() < 0.2:
                continue
            for b in _decode_all(cache.gray(i, scale=3)):
                assert b == frames[i]
                asm.feed_bytes(b)
        if results:
            break
    assert results and results[0].ok, results and results[0].message
    r = results[0]
    if kind == "file":
        assert r.path.read_bytes() == inputs[0].read_bytes()
    elif kind == "bundle":
        got, dirs = read_tree(r.path)
        assert got == {"a.txt": "あいう".encode() * 500, "b/c.bin": (src / "dir" / "b" / "c.bin").read_bytes()}
        assert "e" in dirs
    else:
        assert r.path.read_bytes() == b""
