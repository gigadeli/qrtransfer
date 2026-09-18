import datetime as dt
import io
import json
import os
import tarfile

import pytest

from qrtransfer import packer, protocol, qrgen

from conftest import write_tree


def test_single_file_mode(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF" + os.urandom(100))
    os.utime(f, (1726531200, 1726531200))
    packed = packer.pack_paths([f])
    assert packed.mode == "file" and packed.name == "report.pdf"
    assert packed.raw == f.read_bytes() and packed.mtime == 1726531200


def test_zip_is_sent_as_file(tmp_path):
    f = tmp_path / "a.zip"
    f.write_bytes(b"PK\x03\x04junk")
    assert packer.pack_paths([f]).mode == "file"


def _tar_names(raw: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
        return {m.name: m for m in t.getmembers()}


def test_folder_bundle(tmp_path):
    root = tmp_path / "資料フォルダ"
    write_tree(root, {"a.txt": b"A", "サブ/深い/階層/b.bin": b"\x00\x01", "日本語.txt": "こんにちは".encode()},
               empty_dirs=["空のフォルダ"])
    packed = packer.pack_paths([root])
    assert packed.mode == "bundle" and packed.name == "資料フォルダ" and packed.mtime is None
    names = _tar_names(packed.raw)
    assert "a.txt" in names and names["a.txt"].isreg()
    assert "サブ/深い/階層/b.bin" in names
    assert "日本語.txt" in names
    assert "空のフォルダ" in names and names["空のフォルダ"].isdir()
    assert all("\\" not in n and not n.startswith("/") for n in names)
    assert packed.file_count == 3


def test_multiple_bundle(tmp_path):
    write_tree(tmp_path / "x", {"a.txt": b"1"})
    write_tree(tmp_path / "y", {"a.txt": b"2"})
    write_tree(tmp_path / "d", {"inner/c.txt": b"3"})
    packed = packer.pack_paths([tmp_path / "x" / "a.txt", tmp_path / "y" / "a.txt", tmp_path / "d"],
                               now=dt.datetime(2026, 9, 18, 12, 34, 56))
    assert packed.mode == "bundle" and packed.name == "bundle_20260918_123456"
    names = _tar_names(packed.raw)
    assert {"a.txt", "a (1).txt", "d", "d/inner", "d/inner/c.txt"} <= set(names)


def test_empty_file(tmp_path):
    f = tmp_path / "empty.dat"
    f.write_bytes(b"")
    plan = packer.make_plan([f])
    assert plan.total == 0 and plan.payload == b"" and plan.meta["compression"] == "none"
    assert packer.carousel_order(plan.total) == [packer.CAROUSEL_META]


def test_missing_input(tmp_path):
    with pytest.raises(packer.PackError):
        packer.pack_paths([tmp_path / "nope"])
    with pytest.raises(packer.PackError):
        packer.pack_paths([])


def test_compression_text_vs_random():
    text = ("QRTransfer のテストです。" * 2000).encode()
    method, payload = packer.choose_compression(text)
    assert method in ("zlib", "lzma") and len(payload) < len(text) * 0.95
    assert packer.decompress(method, payload, len(text)) == text
    rnd = os.urandom(50_000)
    method, payload = packer.choose_compression(rnd)
    assert method == "none" and payload == rnd


def test_lzma_skipped_for_large(monkeypatch):
    monkeypatch.setattr(packer, "LZMA_MAX_RAW", 10)
    method, _ = packer.choose_compression(b"a" * 1000)
    assert method == "zlib"


def test_decompress_rejects_bomb_and_truncated():
    data = b"\x00" * 100_000
    z = zlib_c = packer.compress("zlib", data)
    with pytest.raises(ValueError):
        packer.decompress("zlib", z, 1000)
    with pytest.raises(ValueError):
        packer.decompress("zlib", zlib_c[: len(zlib_c) // 2], len(data))
    x = packer.compress("lzma", data)
    with pytest.raises(ValueError):
        packer.decompress("lzma", x, 1000)


def test_chunks_and_meta(tmp_path):
    f = tmp_path / "r.bin"
    f.write_bytes(os.urandom(2000))
    plan = packer.make_plan([f], chunk_size=800, session_id=42)
    assert plan.total == 3 and [len(c) for c in plan.chunks] == [800, 800, 400]
    meta = packer.parse_meta(protocol.parse_frame(plan.meta_frame()).payload)
    assert meta is not None
    for key in ("name", "mode", "compression", "raw_size", "payload_size", "chunk_size", "total",
                "sha256_raw", "sha256_payload", "mtime", "app_version"):
        assert key in meta
    assert b"".join(protocol.parse_frame(plan.data_frame(i)).payload for i in range(3)) == plan.payload


def test_chunk_size_bounds():
    with pytest.raises(ValueError):
        packer.split_chunks(b"x", 199)
    with pytest.raises(ValueError):
        packer.split_chunks(b"x", 2001)


def test_meta_name_truncation():
    meta = {"name": "長いファイル名" * 200 + ".docx", "mode": "file", "compression": "none", "raw_size": 1,
            "payload_size": 1, "chunk_size": 200, "total": 1, "sha256_raw": "0" * 64,
            "sha256_payload": "0" * 64, "mtime": 1, "app_version": "1.0.0"}
    data = packer.meta_payload(meta, 400)
    assert len(data) <= 400
    parsed = json.loads(data)
    assert parsed["name"].endswith(".docx") and len(parsed["name"]) < len(meta["name"])
    assert packer.parse_meta(data) is not None


def test_version_uniform_and_meta_fits(tmp_path):
    packed = packer.Packed(name="とても長い名前" * 40 + ".bin", mode="file", raw=os.urandom(5000), mtime=0)
    for chunk_size in (200, 800, 2000):
        for ecc in ("l", "m", "q", "h"):
            plan = packer.make_plan_from_packed(packed, chunk_size=chunk_size)
            try:
                v = qrgen.choose_version(plan.max_data_frame_size, plan.min_meta_frame_size(), ecc)
            except qrgen.QRGenError:
                assert plan.max_data_frame_size > qrgen.capacity(40, ecc)
                continue
            cap = qrgen.capacity(v, ecc)
            meta_frame = plan.meta_frame(cap)
            assert len(meta_frame) <= cap
            assert plan.max_data_frame_size <= cap


def test_carousel_order():
    order = packer.carousel_order(45)
    M = packer.CAROUSEL_META
    assert order[0] == M
    assert order[1:21] == list(range(20)) and order[21] == M
    assert order[22:42] == list(range(20, 40)) and order[42] == M
    assert order[43:] == list(range(40, 45))
    assert sorted(x for x in order if x != M) == list(range(45))
    resend = packer.carousel_order(100, {12, 57, 58, 99, 150})
    assert resend == [M, 12, 57, 58, 99]
