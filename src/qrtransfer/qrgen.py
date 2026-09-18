"""segno による QR 生成、バージョン決定、画像化、キャッシュ。

このモジュールは Qt に依存しない（to_qimage のみ、呼ばれたときに PySide6 を import する）。
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Sequence

import numpy as np
import segno
from segno import consts as _consts

ECC_LEVELS = ("l", "m", "q", "h")
ECC_DEFAULT = "m"
QUIET_ZONE = 4
MAX_VERSION = 40

ProgressCB = Callable[[int, int], None]


class QRGenError(Exception):
    pass


def _norm_ecc(ecc: str) -> str:
    e = ecc.lower()
    if e not in ECC_LEVELS:
        raise ValueError(f"invalid ECC level: {ecc}")
    return e


@lru_cache(maxsize=None)
def capacity(version: int, ecc: str) -> int:
    """バイトモードで格納できる最大バイト数。"""
    if not 1 <= version <= MAX_VERSION:
        raise ValueError(f"invalid version: {version}")
    level = _consts.ERROR_MAPPING[_norm_ecc(ecc).upper()]
    bits = _consts.SYMBOL_CAPACITY[version][level]
    cci = 8 if version < 10 else 16
    return (bits - 4 - cci) // 8


def min_version(nbytes: int, ecc: str) -> int | None:
    for v in range(1, MAX_VERSION + 1):
        if capacity(v, ecc) >= nbytes:
            return v
    return None


def choose_version(max_data_frame: int, min_meta_frame: int, ecc: str) -> int:
    """全フレーム共通の QR バージョンを決める。DATA の最大フレームと META（最小の名前）が収まる最小版。"""
    need = max(max_data_frame, min_meta_frame)
    v = min_version(need, ecc)
    if v is None:
        raise QRGenError(
            f"1 フレーム {need} バイトは ECC={ecc.upper()} の QR（最大 {capacity(MAX_VERSION, ecc)} バイト）に収まりません。"
            "チャンクサイズを小さくするか、ECC を下げてください。")
    return v


def modules_for_version(version: int, border: int = QUIET_ZONE) -> int:
    return 17 + 4 * version + 2 * border


def make_qr(data: bytes, version: int, ecc: str) -> segno.QRCode:
    return segno.make_qr(bytes(data), error=_norm_ecc(ecc), mode="byte", version=version, boost_error=False)


def make_matrix(data: bytes, version: int, ecc: str, border: int = QUIET_ZONE) -> np.ndarray:
    """QR のモジュール行列（True=黒）をクワイエットゾーン込みで返す。"""
    qr = make_qr(data, version, ecc)
    return np.array([list(row) for row in qr.matrix_iter(scale=1, border=border)], dtype=bool)


def matrix_to_gray(matrix: np.ndarray, scale: int = 1) -> np.ndarray:
    """モジュール行列を 8bit グレースケール画像（黒=0, 白=255）にする。"""
    img = np.where(matrix, 0, 255).astype(np.uint8)
    if scale > 1:
        img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
    return img


def to_qimage(matrix: np.ndarray):
    """モジュール 1 個 = 1 ピクセルの QImage（Grayscale8）。表示側で FastTransformation 拡大する。"""
    from PySide6.QtGui import QImage

    gray = np.ascontiguousarray(matrix_to_gray(matrix))
    h, w = gray.shape
    return QImage(gray.data, w, h, w, QImage.Format.Format_Grayscale8).copy()


# ---------------------------------------------------------------------------
# 一括生成とキャッシュ
# ---------------------------------------------------------------------------

def _encode_packed(args: tuple[bytes, int, str]) -> bytes:
    data, version, ecc = args
    return np.packbits(make_matrix(data, version, ecc)).tobytes()


@dataclass
class QRCache:
    """全フレームの QR 行列を圧縮して保持する。index はフレームリストの添字。"""
    version: int
    ecc: str
    size: int  # クワイエットゾーン込みの 1 辺モジュール数
    packed: list[bytes]
    parallel: bool = False  # プロセスプールで生成できたか（診断用）

    def __len__(self) -> int:
        return len(self.packed)

    def matrix(self, index: int) -> np.ndarray:
        bits = np.unpackbits(np.frombuffer(self.packed[index], dtype=np.uint8), count=self.size * self.size)
        return bits.reshape(self.size, self.size).astype(bool)

    def gray(self, index: int, scale: int = 1) -> np.ndarray:
        return matrix_to_gray(self.matrix(index), scale)


def generate_cache(frames: Sequence[bytes], version: int, ecc: str,
                   progress: ProgressCB | None = None,
                   cancel: threading.Event | None = None,
                   workers: int | None = None) -> QRCache | None:
    """全フレームの QR を生成する。cancel がセットされたら None を返す。

    フレーム数が多い場合はプロセスプールで並列に生成する（失敗したら逐次生成にフォールバック）。
    """
    ecc = _norm_ecc(ecc)
    n = len(frames)
    size = modules_for_version(version)
    out: list[bytes] = []
    if progress:
        progress(0, n)
    if workers is None:
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))

    def serial(start: int) -> bool:
        for i in range(start, n):
            if cancel is not None and cancel.is_set():
                return False
            out.append(_encode_packed((frames[i], version, ecc)))
            if progress:
                progress(i + 1, n)
        return True

    parallel = False
    if workers > 1 and n >= 64:
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                args = ((f, version, ecc) for f in frames)
                for packed in ex.map(_encode_packed, args, chunksize=16):
                    if cancel is not None and cancel.is_set():
                        ex.shutdown(wait=False, cancel_futures=True)
                        return None
                    out.append(packed)
                    if progress:
                        progress(len(out), n)
            parallel = len(out) == n
        except Exception:
            # プロセスプールが使えない環境では、残りを逐次生成する
            pass
    if not serial(len(out)):
        return None
    return QRCache(version=version, ecc=ecc, size=size, packed=out, parallel=parallel)
