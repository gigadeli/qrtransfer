"""転送プロトコル v1: フレームの生成と解析、範囲文字列のパースと生成。

フレーム形式（ビッグエンディアン）:
    magic(2) "QZ" | version(1) | type(1) | session_id(4) | seq(4) | total(4) | payload(N) | crc32(4)
CRC32 はオフセット 0 から payload 末尾までを対象とする。

type: 0=META、1=DATA（seq 番目のチャンク）、2=REPAIR（修復用。seq は修復用フレームの番号で、
payload は複数のチャンクの XOR。どのチャンクを重ねたかは repair.py の決まりで session_id と seq から求まる）。
REPAIR を知らない古い受信側は、種類が不明なフレームとして読み飛ばす（DATA だけで従来どおり受信できる）。
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass
from typing import Iterable

MAGIC = b"QZ"
VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})

TYPE_META = 0
TYPE_DATA = 1
TYPE_REPAIR = 2
FRAME_TYPES = (TYPE_META, TYPE_DATA, TYPE_REPAIR)

HEADER = struct.Struct(">2sBBIII")
HEADER_SIZE = HEADER.size  # 16
CRC = struct.Struct(">I")
CRC_SIZE = CRC.size  # 4
OVERHEAD = HEADER_SIZE + CRC_SIZE  # 20

META_INTERVAL = 20  # DATA 20 枚ごとに META を 1 枚挟む

_U32_MAX = 0xFFFFFFFF


@dataclass(frozen=True)
class Frame:
    type: int
    session_id: int
    seq: int
    total: int
    payload: bytes

    @property
    def is_meta(self) -> bool:
        return self.type == TYPE_META

    @property
    def is_data(self) -> bool:
        return self.type == TYPE_DATA

    @property
    def is_repair(self) -> bool:
        return self.type == TYPE_REPAIR


def build_frame(ftype: int, session_id: int, seq: int, total: int, payload: bytes) -> bytes:
    if ftype not in FRAME_TYPES:
        raise ValueError(f"unknown frame type: {ftype}")
    for name, value in (("session_id", session_id), ("seq", seq), ("total", total)):
        if not 0 <= value <= _U32_MAX:
            raise ValueError(f"{name} out of range: {value}")
    body = HEADER.pack(MAGIC, VERSION, ftype, session_id, seq, total) + bytes(payload)
    return body + CRC.pack(zlib.crc32(body) & _U32_MAX)


def build_meta_frame(session_id: int, total: int, payload: bytes) -> bytes:
    return build_frame(TYPE_META, session_id, 0, total, payload)


def build_data_frame(session_id: int, seq: int, total: int, payload: bytes) -> bytes:
    return build_frame(TYPE_DATA, session_id, seq, total, payload)


def build_repair_frame(session_id: int, index: int, total: int, payload: bytes) -> bytes:
    return build_frame(TYPE_REPAIR, session_id, index, total, payload)


def parse_frame(data: bytes | bytearray | memoryview | None) -> Frame | None:
    """フレームを解析する。不正なフレームは例外を出さずに None を返す（黙って破棄）。"""
    if data is None:
        return None
    data = bytes(data)
    if len(data) < OVERHEAD:
        return None
    magic, version, ftype, session_id, seq, total = HEADER.unpack_from(data, 0)
    if magic != MAGIC or version not in SUPPORTED_VERSIONS:
        return None
    (crc,) = CRC.unpack_from(data, len(data) - CRC_SIZE)
    if zlib.crc32(data[:-CRC_SIZE]) & _U32_MAX != crc:
        return None
    if ftype not in FRAME_TYPES:
        return None
    if ftype == TYPE_DATA and seq >= total:
        return None
    return Frame(ftype, session_id, seq, total, data[HEADER_SIZE:-CRC_SIZE])


# ---------------------------------------------------------------------------
# 範囲文字列 "12,57-60,99"
# ---------------------------------------------------------------------------

_RANGE_ITEM = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")
# 全角の数字・記号も受け付ける（手入力のため）
_NORMALIZE = str.maketrans("０１２３４５６７８９，、－ー―‐〜~　", "0123456789,,------ ")


def parse_ranges(text: str, limit: int | None = None) -> set[int]:
    """`"1,3-5,7"` → `{1,3,4,5,7}`。

    limit を指定すると、limit 以上の値を含む入力を拒否する（0 <= n < limit）。
    空文字列（空白のみ）は空集合を返す。不正な入力は ValueError。
    """
    if text is None:
        raise ValueError("input is None")
    text = text.translate(_NORMALIZE).replace("\n", ",").replace("\r", ",")
    if not text.strip():
        return set()
    result: set[int] = set()
    for raw in text.split(","):
        item = raw.strip(" \t")
        if not item:
            raise ValueError(f"empty item in {text!r}")
        m = _RANGE_ITEM.match(item)
        if not m:
            raise ValueError(f"invalid range item: {raw!r}")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if end < start:
            raise ValueError(f"reversed range: {raw!r}")
        if limit is not None and end >= limit:
            raise ValueError(f"value out of range (must be < {limit}): {raw!r}")
        if end > _U32_MAX:
            raise ValueError(f"value too large: {raw!r}")
        if end - start > 10_000_000:
            raise ValueError(f"range too large: {raw!r}")
        result.update(range(start, end + 1))
    return result


def iter_ranges(values: Iterable[int]) -> Iterable[tuple[int, int]]:
    """昇順の (start, end) 区間列を返す（end を含む）。"""
    start = prev = None
    for v in sorted(set(values)):
        if start is None:
            start = prev = v
        elif v == prev + 1:
            prev = v
        else:
            yield start, prev
            start = prev = v
    if start is not None:
        yield start, prev


def format_ranges(values: Iterable[int], max_items: int | None = None) -> str:
    """`{1,3,4,5,7}` → `"1,3-5,7"`。

    max_items を指定すると、先頭 max_items 区間だけを出力し、残りがあれば末尾に ",…" を付ける
    （表示用。",…" 付きの文字列は parse_ranges では解析できない）。
    """
    parts: list[str] = []
    for i, (s, e) in enumerate(iter_ranges(values)):
        if max_items is not None and i >= max_items:
            parts.append("…")
            break
        parts.append(str(s) if s == e else f"{s}-{e}")
    return ",".join(parts)
