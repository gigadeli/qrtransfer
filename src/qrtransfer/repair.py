"""修復用フレーム（REPAIR）: 複数のチャンクを XOR で重ねたもの。欠けたチャンクを受信側で計算して復元する。

送信側は DATA を 1 周流したあと、修復用フレームを流す（以後はその繰り返し）。受信側は、どのフレームを
取りこぼしても、受け取った DATA と REPAIR の枚数が全チャンク数を少し超えた時点で、残りを計算で求められる
（欠落番号を送信側に伝えて再送してもらう必要がない）。

どのチャンクを重ねるかは、session_id と修復用フレームの番号から決まる（Web 版 web/src/lib/repair.ts と同じ手順。
浮動小数点を使わない整数演算だけで決めるので、Python と JavaScript で必ず一致する）。
"""

from __future__ import annotations

import math
from typing import Callable, Iterable

import numpy as np

_M = 0xFFFFFFFF

DEGREE_MAX = 1024  # 1 枚の修復用フレームに重ねるチャンク数の上限
TOTAL_LIMIT = 1 << 21  # これより多いチャンク数の転送では修復用フレームを使わない（番号の計算を整数の範囲に収める）
RATIO_DEFAULT = 50  # 修復用フレームの枚数（全チャンク数に対する %）
RATIO_MAX = 200
MEMORY_LIMIT = 128 * 1024 * 1024  # 受信側で、まだ解けていない式を保持する上限（バイト）


def degree(total: int) -> int:
    """重ねるチャンク数。少ないときは半分（どの欠落にも効く）、多いときは DEGREE_MAX。"""
    return max(1, min(total, DEGREE_MAX, (total + 1) // 2))


def repair_count(total: int, ratio: int) -> int:
    """送信側で作る修復用フレームの枚数。ratio=0（従来方式）や大きすぎる転送では 0。"""
    if ratio <= 0 or total <= 0 or total >= TOTAL_LIMIT:
        return 0
    return max(4, math.ceil(total * ratio / 100))


class _Mulberry32:
    def __init__(self, seed: int):
        self.a = seed & _M

    def next(self) -> int:
        self.a = (self.a + 0x6D2B79F5) & _M
        t = self.a
        t = ((t ^ (t >> 15)) * (t | 1)) & _M
        t ^= (t + ((t ^ (t >> 7)) * (t | 61))) & _M
        return (t ^ (t >> 14)) & _M

    def below(self, n: int) -> int:
        return (self.next() * n) >> 32


def indices(session_id: int, index: int, total: int) -> list[int]:
    """index 番目の修復用フレームが重ねるチャンクの番号（昇順）。"""
    if not 0 < total < TOTAL_LIMIT:
        raise ValueError(f"total out of range: {total}")
    rng = _Mulberry32(session_id ^ (((index + 1) * 0x9E3779B1) & _M))
    d = degree(total)
    chosen: set[int] = set()
    # Floyd の方法: total 個から d 個を重複なく選ぶ
    for j in range(total - d, total):
        t = rng.below(j + 1)
        chosen.add(j if t in chosen else t)
    return sorted(chosen)


def encode(chunks: list[bytes], chunk_size: int, session_id: int, count: int) -> list[bytes]:
    """修復用フレームの payload（長さ chunk_size。短い最終チャンクは 0 で埋めて重ねる）を count 枚作る。"""
    total = len(chunks)
    if count <= 0 or total == 0:
        return []
    arr = np.zeros((total, chunk_size), dtype=np.uint8)
    for i, c in enumerate(chunks):
        arr[i, :len(c)] = np.frombuffer(c, dtype=np.uint8)
    return [np.bitwise_xor.reduce(arr[indices(session_id, r, total)], axis=0).tobytes() for r in range(count)]


class Decoder:
    """修復用フレームの式（どのチャンクの XOR か）を集め、解けたチャンクを返す。

    式は「まだ受け取っていないチャンク」だけを未知数として、既約行階段形（各行の先頭の未知数は他の行に現れない）で持つ。
    未知数が 1 つだけになった行は、そのチャンクが解けたことを表す。
    ビット列と payload は Python の int で持つ（XOR が速い）。
    """

    def __init__(self, total: int, length: int, memory_limit: int = MEMORY_LIMIT):
        self.total = total
        self.length = length
        self.rows: dict[int, tuple[int, int]] = {}  # 先頭の未知数 → (未知数のビット列, payload)
        self.max_rows = max(1, memory_limit // (total // 8 + length + 64))
        self.dropped = 0  # 保持の上限で捨てた式の数（診断用）

    def _pad(self, data: bytes) -> int:
        return int.from_bytes(bytes(data).ljust(self.length, b"\0"), "big")

    def add_repair(self, idx: Iterable[int], payload: bytes, known: Callable[[int], bytes | None]) -> list[tuple[int, bytes]]:
        """修復用フレーム 1 枚分の式を加える。known(i) は受信済みのチャンク（なければ None）。解けたチャンクを返す。"""
        bits = 0
        data = self._pad(payload)
        for i in idx:
            c = known(i)
            if c is None:
                bits |= 1 << i
            else:
                data ^= self._pad(c)
        return self._insert(bits, data, None)

    def add_known(self, seq: int, chunk: bytes) -> list[tuple[int, bytes]]:
        """DATA で受け取ったチャンクを式に反映する。これによって解けた（ほかの）チャンクを返す。"""
        mask = 1 << seq
        if not any(b & mask for b, _ in self.rows.values()):
            return []
        return self._insert(mask, self._pad(chunk), seq)

    def _insert(self, bits: int, data: int, skip: int | None) -> list[tuple[int, bytes]]:
        x = bits
        while x:
            low = x & -x
            x ^= low
            row = self.rows.get(low.bit_length() - 1)
            if row is not None:
                bits ^= row[0]
                data ^= row[1]
        if not bits:
            return []  # ほかの式から導ける（新しい情報がない）
        if len(self.rows) >= self.max_rows and bits & (bits - 1):
            self.dropped += 1
            return []
        low = bits & -bits
        pivot = low.bit_length() - 1
        touched = [pivot]
        for q, (b, d) in self.rows.items():
            if b & low:
                self.rows[q] = (b ^ bits, d ^ data)
                touched.append(q)
        self.rows[pivot] = (bits, data)
        solved: list[tuple[int, bytes]] = []
        for q in touched:
            b, d = self.rows[q]
            if not b & (b - 1):
                del self.rows[q]
                if q != skip:
                    solved.append((q, d.to_bytes(self.length, "big")))
        return solved
