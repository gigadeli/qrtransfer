import struct
import zlib

import pytest

from qrtransfer import protocol as p


def test_header_format():
    assert p.HEADER.format == ">2sBBIII"
    assert p.HEADER_SIZE == 16
    assert p.OVERHEAD == 20


def test_build_and_parse_data():
    fr = p.build_data_frame(0xDEADBEEF, 5, 10, b"hello\x00\xff")
    assert fr[:2] == b"QZ" and fr[2] == 1 and fr[3] == p.TYPE_DATA
    assert len(fr) == 20 + 7
    assert struct.unpack(">I", fr[-4:])[0] == zlib.crc32(fr[:-4])
    parsed = p.parse_frame(fr)
    assert parsed == p.Frame(p.TYPE_DATA, 0xDEADBEEF, 5, 10, b"hello\x00\xff")


def test_empty_payload_meta():
    fr = p.build_meta_frame(1, 0, b"")
    assert len(fr) == 20
    parsed = p.parse_frame(fr)
    assert parsed is not None and parsed.is_meta and parsed.payload == b"" and parsed.total == 0


def test_max_payload():
    payload = bytes(range(256)) * 12
    fr = p.build_data_frame(0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFF, payload)
    parsed = p.parse_frame(fr)
    assert parsed.payload == payload and parsed.session_id == 0xFFFFFFFF


def test_all_bit_flips_rejected():
    fr = p.build_data_frame(7, 1, 3, b"abcdefgh")
    for i in range(len(fr)):
        for bit in range(8):
            b = bytearray(fr)
            b[i] ^= 1 << bit
            assert p.parse_frame(bytes(b)) is None, (i, bit)


def test_bad_magic_version_short():
    fr = bytearray(p.build_data_frame(7, 1, 3, b"abc"))
    bad = bytearray(fr)
    bad[0:2] = b"QX"
    bad[-4:] = struct.pack(">I", zlib.crc32(bytes(bad[:-4])))
    assert p.parse_frame(bytes(bad)) is None
    badv = bytearray(fr)
    badv[2] = 2
    badv[-4:] = struct.pack(">I", zlib.crc32(bytes(badv[:-4])))
    assert p.parse_frame(bytes(badv)) is None
    for n in range(20):
        assert p.parse_frame(bytes(fr[:n])) is None
    assert p.parse_frame(bytes(fr[:-1])) is None
    assert p.parse_frame(None) is None


def test_seq_out_of_range_rejected():
    body = p.HEADER.pack(b"QZ", 1, p.TYPE_DATA, 1, 5, 5) + b"x"
    fr = body + struct.pack(">I", zlib.crc32(body))
    assert p.parse_frame(fr) is None


def test_build_rejects_invalid():
    with pytest.raises(ValueError):
        p.build_frame(3, 0, 0, 0, b"")
    with pytest.raises(ValueError):
        p.build_frame(1, -1, 0, 0, b"")


@pytest.mark.parametrize("text,expected", [
    ("1,3-5,7", {1, 3, 4, 5, 7}),
    ("0", {0}),
    (" 12 , 57-60 ,99 ", {12, 57, 58, 59, 60, 99}),
    ("3 - 4", {3, 4}),
    ("5-5", {5}),
    ("１２，５７－５８", {12, 57, 58}),
    ("", set()),
    ("   ", set()),
])
def test_parse_ranges(text, expected):
    assert p.parse_ranges(text) == expected


@pytest.mark.parametrize("text", ["a", "1,", ",1", "1,,2", "5-3", "-1", "1-", "1-2-3", "1.5", "0x10", "1 2"])
def test_parse_ranges_invalid(text):
    with pytest.raises(ValueError):
        p.parse_ranges(text)


def test_parse_ranges_limit():
    assert p.parse_ranges("0-9", limit=10) == set(range(10))
    with pytest.raises(ValueError):
        p.parse_ranges("0-10", limit=10)


def test_format_ranges():
    assert p.format_ranges({1, 3, 4, 5, 7}) == "1,3-5,7"
    assert p.format_ranges([]) == ""
    assert p.format_ranges([9, 8, 8, 7]) == "7-9"
    assert p.format_ranges([1, 3, 5, 7], max_items=2) == "1,3,…"


def test_ranges_roundtrip(rng):
    for _ in range(200):
        s = {rng.randrange(500) for _ in range(rng.randrange(60))}
        assert p.parse_ranges(p.format_ranges(s)) == s
