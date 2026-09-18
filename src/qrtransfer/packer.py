"""送信データの作成: 梱包（file / bundle）、圧縮の自動判定、ハッシュ、チャンク分割、フレーム列の生成。"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import lzma
import math
import os
import secrets
import stat
import tarfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import __version__
from . import protocol, repair

MODE_FILE = "file"
MODE_BUNDLE = "bundle"

COMP_NONE = "none"
COMP_ZLIB = "zlib"
COMP_LZMA = "lzma"
COMPRESSIONS = (COMP_NONE, COMP_ZLIB, COMP_LZMA)

COMPRESSION_RATIO = 0.95
LZMA_MAX_RAW = 20 * 1024 * 1024

CHUNK_SIZE_DEFAULT = 800
CHUNK_SIZE_MIN = 200
CHUNK_SIZE_MAX = 2000

META_NAME_MIN_BYTES = 64  # META が収まるバージョンを決めるときに確保するファイル名の長さ

CAROUSEL_META = -1  # carousel_order の中で META を表す値
CAROUSEL_REPAIR = -2  # carousel_order の中で r 番目の修復用フレームは CAROUSEL_REPAIR - r


def carousel_repair(r: int) -> int:
    return CAROUSEL_REPAIR - r


def repair_index(entry: int) -> int | None:
    """carousel_order の値が修復用フレームなら、その番号。"""
    return CAROUSEL_REPAIR - entry if entry <= CAROUSEL_REPAIR else None

ProgressCB = Callable[[str, float], None]


class PackError(Exception):
    pass


@dataclass
class Packed:
    name: str
    mode: str
    raw: bytes
    mtime: int | None
    file_count: int = 1


# ---------------------------------------------------------------------------
# 梱包
# ---------------------------------------------------------------------------

def _is_regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _dedupe(name: str, used: set[str]) -> str:
    if name.lower() not in used:
        used.add(name.lower())
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = f"{stem} ({i}){ext}"
        if cand.lower() not in used:
            used.add(cand.lower())
            return cand
        i += 1


def _iter_tree(root: Path, arc_prefix: str) -> Iterable[tuple[Path, str, bool]]:
    """フォルダ配下を (実パス, tar 内パス, is_dir) で列挙する。シンボリックリンクは辿らない。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel = Path(dirpath).relative_to(root).as_posix()
        base = arc_prefix if rel == "." else (f"{arc_prefix}/{rel}" if arc_prefix else rel)
        # 空フォルダも含めるため、ディレクトリ自身を出力する（ルートは prefix がある場合のみ）
        if base:
            yield Path(dirpath), base, True
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if _is_regular_file(p):
                yield p, f"{base}/{fn}" if base else fn, False
        # シンボリックリンクのディレクトリは os.walk が辿らないが、dirnames に残るので除外する
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]


def _make_tar(entries: Sequence[tuple[Path, str, bool]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, arcname, is_dir in entries:
            st = os.stat(path)
            info = tarfile.TarInfo(arcname)
            info.mtime = int(st.st_mtime)
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.mode = 0o644
                info.size = st.st_size
                with open(path, "rb") as f:
                    tar.addfile(info, f)
    return buf.getvalue()


def pack_paths(paths: Sequence[str | os.PathLike], now: _dt.datetime | None = None) -> Packed:
    """入力パスを梱包する。

    - 単一ファイル → mode=file（バイト列そのまま）
    - 単一フォルダ → mode=bundle（name=フォルダ名、tar 内はフォルダの中身からの相対パス）
    - 複数 → mode=bundle（name=bundle_YYYYMMDD_HHMMSS、tar 内はそれぞれのベース名から）
    """
    items = [Path(p) for p in paths]
    if not items:
        raise PackError("送信するファイルが選択されていません")
    for p in items:
        if not p.exists():
            raise PackError(f"見つかりません: {p}")

    if len(items) == 1 and items[0].is_file():
        p = items[0]
        return Packed(name=p.name, mode=MODE_FILE, raw=p.read_bytes(), mtime=int(p.stat().st_mtime))

    entries: list[tuple[Path, str, bool]] = []
    if len(items) == 1:
        root = items[0]
        name = root.resolve().name or "bundle"
        entries.extend(_iter_tree(root, ""))
    else:
        now = now or _dt.datetime.now()
        name = now.strftime("bundle_%Y%m%d_%H%M%S")
        used: set[str] = set()
        for p in items:
            arc = _dedupe(p.resolve().name or "item", used)
            if p.is_dir():
                entries.extend(_iter_tree(p, arc))
            elif _is_regular_file(p):
                entries.append((p, arc, False))
    file_count = sum(1 for _, _, d in entries if not d)
    return Packed(name=name, mode=MODE_BUNDLE, raw=_make_tar(entries), mtime=None, file_count=file_count)


def total_input_size(paths: Iterable[str | os.PathLike]) -> tuple[int, int]:
    """(合計バイト数, ファイル数) を返す（見積もり表示用）。"""
    size = count = 0
    for p in map(Path, paths):
        try:
            if p.is_file():
                size += p.stat().st_size
                count += 1
            elif p.is_dir():
                for fp, _, is_dir in _iter_tree(p, "x"):
                    if not is_dir:
                        size += fp.stat().st_size
                        count += 1
        except OSError:
            continue
    return size, count


# ---------------------------------------------------------------------------
# 圧縮・ハッシュ・分割
# ---------------------------------------------------------------------------

def choose_compression(raw: bytes) -> tuple[str, bytes]:
    """zlib(level 9) と lzma(preset 6) を試し、95% を下回る最小のものを採用する。"""
    candidates: list[tuple[str, bytes]] = [(COMP_ZLIB, zlib.compress(raw, 9))]
    if len(raw) <= LZMA_MAX_RAW:
        candidates.append((COMP_LZMA, lzma.compress(raw, format=lzma.FORMAT_XZ, preset=6)))
    method, data = min(candidates, key=lambda c: len(c[1]))
    if len(data) < len(raw) * COMPRESSION_RATIO:
        return method, data
    return COMP_NONE, raw


def compress(method: str, raw: bytes) -> bytes:
    if method == COMP_NONE:
        return raw
    if method == COMP_ZLIB:
        return zlib.compress(raw, 9)
    if method == COMP_LZMA:
        return lzma.compress(raw, format=lzma.FORMAT_XZ, preset=6)
    raise ValueError(f"unknown compression: {method}")


def decompress(method: str, payload: bytes, max_size: int) -> bytes:
    """伸長する。max_size を超える出力（壊れた/悪意あるデータ）は ValueError。"""
    if method == COMP_NONE:
        out = payload
    elif method == COMP_ZLIB:
        d = zlib.decompressobj()
        out = d.decompress(payload, max_size + 1)
        if len(out) <= max_size and not d.eof:
            raise ValueError("zlib stream is truncated")
    elif method == COMP_LZMA:
        d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        out = d.decompress(payload, max_length=max_size + 1)
        if len(out) <= max_size and not d.eof:
            raise ValueError("lzma stream is truncated")
    else:
        raise ValueError(f"unknown compression: {method}")
    if len(out) > max_size:
        raise ValueError("decompressed data exceeds expected size")
    return out


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def total_chunks(payload_size: int, chunk_size: int) -> int:
    return math.ceil(payload_size / chunk_size) if payload_size > 0 else 0


def split_chunks(payload: bytes, chunk_size: int) -> list[bytes]:
    if not CHUNK_SIZE_MIN <= chunk_size <= CHUNK_SIZE_MAX:
        raise ValueError(f"chunk_size must be {CHUNK_SIZE_MIN}..{CHUNK_SIZE_MAX}")
    return [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]


# ---------------------------------------------------------------------------
# META
# ---------------------------------------------------------------------------

def _json_bytes(meta: dict) -> bytes:
    return json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def truncate_name(name: str, max_bytes: int) -> str:
    """UTF-8 で max_bytes 以下になるように、拡張子を残して切り詰める。"""
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > max_bytes // 2:
        ext = ""
    budget = max_bytes - len(ext.encode("utf-8")) - len("~".encode("utf-8"))
    out = ""
    for ch in stem:
        if len((out + ch).encode("utf-8")) > budget:
            break
        out += ch
    return (out + "~" + ext) if budget > 0 else name.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def meta_payload(meta: dict, max_bytes: int | None = None) -> bytes:
    """META の JSON を作る。max_bytes に収まらない場合は name を切り詰める。"""
    data = _json_bytes(meta)
    if max_bytes is None or len(data) <= max_bytes:
        return data
    m = dict(meta)
    name = str(m.get("name", ""))
    base = len(_json_bytes({**m, "name": ""}))
    lo_budget = max_bytes - base
    if lo_budget < 1:
        raise ValueError("META does not fit even with an empty name")
    # JSON エスケープで伸びる可能性があるので、収まるまで予算を減らす
    budget = lo_budget
    while budget >= 1:
        m["name"] = truncate_name(name, budget)
        data = _json_bytes(m)
        if len(data) <= max_bytes:
            return data
        budget -= max(1, len(data) - max_bytes)
    raise ValueError("META does not fit")


def parse_meta(payload: bytes) -> dict | None:
    """META の JSON を解析・検証する。不正なら None。"""
    try:
        meta = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    try:
        ok = (
            isinstance(meta["name"], str)
            and meta["mode"] in (MODE_FILE, MODE_BUNDLE)
            and meta["compression"] in COMPRESSIONS
            and isinstance(meta["raw_size"], int) and meta["raw_size"] >= 0
            and isinstance(meta["payload_size"], int) and meta["payload_size"] >= 0
            and isinstance(meta["chunk_size"], int) and meta["chunk_size"] > 0
            and isinstance(meta["total"], int)
            and meta["total"] == total_chunks(meta["payload_size"], meta["chunk_size"])
            and isinstance(meta["sha256_raw"], str) and len(meta["sha256_raw"]) == 64
            and isinstance(meta["sha256_payload"], str) and len(meta["sha256_payload"]) == 64
            and (meta.get("mtime") is None or isinstance(meta.get("mtime"), (int, float)))
        )
    except (KeyError, TypeError):
        return None
    return meta if ok else None


# ---------------------------------------------------------------------------
# 転送計画
# ---------------------------------------------------------------------------

@dataclass
class TransferPlan:
    session_id: int
    meta: dict
    payload: bytes
    chunk_size: int
    chunks: list[bytes] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.chunks)

    @property
    def max_data_frame_size(self) -> int:
        return protocol.OVERHEAD + (max(len(c) for c in self.chunks) if self.chunks else 0)

    def min_meta_frame_size(self) -> int:
        m = dict(self.meta)
        m["name"] = truncate_name(m["name"], META_NAME_MIN_BYTES)
        return protocol.OVERHEAD + len(meta_payload(m))

    def data_frame(self, seq: int) -> bytes:
        return protocol.build_data_frame(self.session_id, seq, self.total, self.chunks[seq])

    def repair_frames(self, count: int, progress: Callable[[int, int], None] | None = None,
                      cancel=None) -> list[bytes] | None:
        """修復用フレームを count 枚作る。cancel（threading.Event）がセットされたら None。"""
        payloads = repair.encode(self.chunks, self.chunk_size, self.session_id, count, progress, cancel)
        if payloads is None:
            return None
        return [protocol.build_repair_frame(self.session_id, r, self.total, p) for r, p in enumerate(payloads)]

    def meta_frame(self, max_frame_size: int | None = None) -> bytes:
        max_payload = None if max_frame_size is None else max_frame_size - protocol.OVERHEAD
        return protocol.build_meta_frame(self.session_id, self.total, meta_payload(self.meta, max_payload))


def make_plan_from_packed(packed: Packed, chunk_size: int = CHUNK_SIZE_DEFAULT,
                          session_id: int | None = None,
                          progress: ProgressCB | None = None) -> TransferPlan:
    if progress:
        progress("圧縮", 0.0)
    compression, payload = choose_compression(packed.raw)
    chunks = split_chunks(payload, chunk_size)
    meta = {
        "name": packed.name,
        "mode": packed.mode,
        "compression": compression,
        "raw_size": len(packed.raw),
        "payload_size": len(payload),
        "chunk_size": chunk_size,
        "total": len(chunks),
        "sha256_raw": sha256_hex(packed.raw),
        "sha256_payload": sha256_hex(payload),
        "mtime": packed.mtime if packed.mode == MODE_FILE else None,
        "app_version": __version__,
    }
    if session_id is None:
        session_id = secrets.randbits(32)
    if progress:
        progress("圧縮", 1.0)
    return TransferPlan(session_id=session_id, meta=meta, payload=payload, chunk_size=chunk_size, chunks=chunks)


def make_plan(paths: Sequence[str | os.PathLike], chunk_size: int = CHUNK_SIZE_DEFAULT,
              session_id: int | None = None, progress: ProgressCB | None = None) -> TransferPlan:
    if progress:
        progress("梱包", 0.0)
    packed = pack_paths(paths)
    return make_plan_from_packed(packed, chunk_size, session_id, progress)


def carousel_order(total: int, seqs: Iterable[int] | None = None,
                   meta_interval: int = protocol.META_INTERVAL, repairs: int = 0) -> list[int]:
    """1 周分の表示順。META は CAROUSEL_META(-1)。先頭は必ず META、DATA meta_interval 枚ごとに META。

    repairs > 0 のときは、DATA のあとに修復用フレーム（carousel_repair(r)）を repairs 枚続ける（seqs 指定の再送では付けない）。
    """
    data = list(range(total)) if seqs is None else sorted({s for s in seqs if 0 <= s < total})
    if seqs is None:
        data += [carousel_repair(r) for r in range(repairs)]
    order: list[int] = []
    for i, s in enumerate(data):
        if i % meta_interval == 0:
            order.append(CAROUSEL_META)
        order.append(s)
    if not order:
        order.append(CAROUSEL_META)
    return order


def estimate_frames_per_cycle(total: int, repairs: int = 0) -> int:
    return total + repairs + max(1, math.ceil((total + repairs) / protocol.META_INTERVAL))
