"""受信側の組立部: 受信状態、ディスクへの逐次保存、再開、完了処理。

Qt に依存しない。通知は AssemblerCallbacks のコールバックで行う（呼び出しは feed を呼んだスレッド上）。
feed / snapshot / switch などの公開メソッドは内部の Lock で保護されている。
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import extractor, packer, protocol

FLUSH_INTERVAL_SEC = 1.0
FLUSH_EVERY_CHUNKS = 50
RATE_WINDOW_SEC = 5.0

# feed の戻り値
ST_NEW = "new"            # 新しいチャンクを受信した
ST_META = "meta"          # META を初めて受信した
ST_DUP = "dup"            # 受信済み（無視）
ST_CONFLICT = "conflict"  # 別のセッションのフレーム（採用しない）
ST_FINISHED = "finished"  # 完了/失敗済みセッションのフレーム（無視）
ST_INVALID = "invalid"    # 内容に矛盾があるフレーム（無視）
ST_COMPLETE = "complete"  # このフレームで完了した
ST_FAILED = "failed"      # このフレームで完了処理を行ったが失敗した


def default_sessions_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "QRTransfer" / "sessions"


def session_dirname(session_id: int) -> str:
    return f"{session_id:08x}"


@dataclass
class CompletionResult:
    ok: bool
    session_id: int
    message: str
    name: str = ""
    path: Path | None = None
    raw_size: int = 0
    payload_size: int = 0
    elapsed_sec: float = 0.0
    sha256_ok: bool = False
    extract_report: extractor.ExtractReport | None = None
    zip_report: extractor.ExtractReport | None = None


@dataclass
class Snapshot:
    session_id: int | None
    meta: dict | None
    total: int | None
    received: int
    bitmap: bytes  # 1 チャンク = 1 ビット（MSB から）
    rate_chunks: float
    rate_bytes: float
    eta_sec: float | None
    elapsed_sec: float
    finished: bool
    conflict_session: int | None


@dataclass
class AssemblerCallbacks:
    on_meta: Callable[[dict], None] | None = None
    on_conflict: Callable[[int], None] | None = None
    on_complete: Callable[[CompletionResult], None] | None = None


@dataclass
class IncompleteSession:
    session_id: int
    path: Path
    total: int
    received: int
    meta: dict | None
    updated: float


class Bitmap:
    """ビット単位の受信状況。"""

    def __init__(self, total: int, data: bytes | None = None):
        self.total = total
        n = (total + 7) // 8
        self.data = bytearray(data[:n]) if data is not None else bytearray(n)
        if len(self.data) < n:
            self.data.extend(bytes(n - len(self.data)))
        # total 以降のビットはクリアしておく
        if total % 8 and n:
            self.data[-1] &= (0xFF00 >> (total % 8)) & 0xFF
        self.count = int(np.unpackbits(np.frombuffer(bytes(self.data), dtype=np.uint8)).sum()) if n else 0

    def get(self, i: int) -> bool:
        return bool(self.data[i >> 3] & (0x80 >> (i & 7)))

    def set(self, i: int) -> bool:
        """ビットを立てる。新たに立てた場合 True。"""
        mask = 0x80 >> (i & 7)
        if self.data[i >> 3] & mask:
            return False
        self.data[i >> 3] |= mask
        self.count += 1
        return True

    def clear(self, i: int) -> None:
        mask = 0x80 >> (i & 7)
        if self.data[i >> 3] & mask:
            self.data[i >> 3] &= ~mask & 0xFF
            self.count -= 1

    @property
    def complete(self) -> bool:
        return self.count >= self.total

    def missing(self) -> list[int]:
        if self.total == 0:
            return []
        bits = np.unpackbits(np.frombuffer(bytes(self.data), dtype=np.uint8), count=self.total)
        return np.flatnonzero(bits == 0).tolist()


class _Session:
    def __init__(self, root: Path, session_id: int, total: int):
        self.session_id = session_id
        self.total = total
        self.dir = root / session_dirname(session_id)
        self.meta: dict | None = None
        self.chunk_size: int | None = None
        self.bitmap = Bitmap(total)
        self.pending: dict[int, bytes] = {}  # chunk_size 判明前に受け取ったチャンク
        self.chunk_file = None
        self.started = time.monotonic()
        self.last_flush = time.monotonic()
        self.unflushed = 0
        self.times: collections.deque[float] = collections.deque()
        self.finished = False
        self.last_len: int | None = None  # 最終チャンクの長さ（META 前でも検証に使う）

    # --- 永続化 ---------------------------------------------------------------
    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def write_state(self) -> None:
        self.ensure_dir()
        state = {"session_id": self.session_id, "total": self.total, "chunk_size": self.chunk_size,
                 "updated": time.time()}
        _atomic_write(self.dir / "state.json", json.dumps(state).encode())

    def write_meta(self) -> None:
        self.ensure_dir()
        _atomic_write(self.dir / "meta.json", json.dumps(self.meta, ensure_ascii=False).encode("utf-8"))

    def open_chunks(self) -> None:
        if self.chunk_file is None:
            self.ensure_dir()
            p = self.dir / "chunks.bin"
            self.chunk_file = open(p, "r+b" if p.exists() else "w+b")

    def write_chunk(self, seq: int, data: bytes) -> None:
        assert self.chunk_size is not None
        self.open_chunks()
        self.chunk_file.seek(seq * self.chunk_size)
        self.chunk_file.write(data)

    def flush(self) -> None:
        if self.chunk_file is not None:
            self.chunk_file.flush()
            try:
                os.fsync(self.chunk_file.fileno())
            except OSError:
                pass
        if self.chunk_size is not None or self.total == 0:
            disk = Bitmap(self.total, bytes(self.bitmap.data))
            for seq in self.pending:
                disk.clear(seq)
            self.ensure_dir()
            _atomic_write(self.dir / "bitmap.bin", bytes(disk.data))
            self.write_state()
        self.last_flush = time.monotonic()
        self.unflushed = 0

    def close(self) -> None:
        if self.chunk_file is not None:
            try:
                self.chunk_file.close()
            finally:
                self.chunk_file = None

    def delete(self) -> None:
        self.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Assembler:
    def __init__(self, output_dir: str | os.PathLike, sessions_dir: str | os.PathLike | None = None,
                 auto_extract_zip: bool = False, callbacks: AssemblerCallbacks | None = None):
        self.output_dir = Path(output_dir)
        self.sessions_dir = Path(sessions_dir) if sessions_dir is not None else default_sessions_dir()
        self.auto_extract_zip = auto_extract_zip
        self.callbacks = callbacks or AssemblerCallbacks()
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._finished_ids: set[int] = set()
        self._conflict_id: int | None = None
        self._conflict_total = 0
        self._notified_conflicts: set[int] = set()
        self.last_result: CompletionResult | None = None

    # ------------------------------------------------------------------ 公開 API
    @property
    def session_id(self) -> int | None:
        with self._lock:
            return self._session.session_id if self._session else None

    def feed_bytes(self, data: bytes) -> str | None:
        frame = protocol.parse_frame(data)
        return None if frame is None else self.feed(frame)

    def feed(self, frame: protocol.Frame) -> str:
        pending_callbacks: list[Callable[[], None]] = []
        with self._lock:
            status = self._feed_locked(frame, pending_callbacks)
        for cb in pending_callbacks:
            cb()
        return status

    def switch_to_conflict(self) -> bool:
        """「切り替え」: 現在のセッションを閉じ（ディスクには残すので後で再開できる）、検出した別の転送に移る。"""
        with self._lock:
            if self._conflict_id is None:
                return False
            sid, total = self._conflict_id, self._conflict_total
            if self._session is not None:
                if not self._session.finished:
                    self._session.flush()
                self._session.close()
            self._session = None
            self._conflict_id = None
            self._notified_conflicts.clear()
            self._finished_ids.discard(sid)
            if not self._try_load(sid) or self._session.total != total:
                self._session = _Session(self.sessions_dir, sid, total)
            return True

    def reset(self, forget_finished: bool = False) -> None:
        """「次の受信」: 現在のセッションを閉じる。"""
        with self._lock:
            if self._session is not None:
                if not self._session.finished:
                    self._session.flush()
                self._session.close()
            self._session = None
            self._conflict_id = None
            self._notified_conflicts.clear()
            if forget_finished:
                self._finished_ids.clear()

    def discard_current(self) -> None:
        with self._lock:
            if self._session is not None:
                self._finished_ids.add(self._session.session_id)
                self._session.delete()
            self._session = None
            self._conflict_id = None

    def flush(self) -> None:
        with self._lock:
            if self._session is not None and not self._session.finished:
                self._session.flush()

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                if not self._session.finished:
                    self._session.flush()
                self._session.close()
            self._session = None

    def tick(self) -> None:
        """定期呼び出し用: 一定時間ごとの flush。"""
        with self._lock:
            s = self._session
            if s is not None and not s.finished and s.unflushed and \
                    time.monotonic() - s.last_flush >= FLUSH_INTERVAL_SEC:
                s.flush()

    def snapshot(self) -> Snapshot:
        with self._lock:
            s = self._session
            if s is None:
                return Snapshot(None, None, None, 0, b"", 0.0, 0.0, None, 0.0, False, self._conflict_id)
            now = time.monotonic()
            while s.times and now - s.times[0] > RATE_WINDOW_SEC:
                s.times.popleft()
            window = min(RATE_WINDOW_SEC, max(now - s.started, 1e-6))
            rate = len(s.times) / window if s.times else 0.0
            csize = s.chunk_size or (s.meta or {}).get("chunk_size") or 0
            remaining = s.total - s.bitmap.count
            eta = (remaining / rate) if rate > 0 else None
            return Snapshot(s.session_id, dict(s.meta) if s.meta else None, s.total, s.bitmap.count,
                            bytes(s.bitmap.data), rate, rate * csize, eta, now - s.started, s.finished,
                            self._conflict_id)

    def missing(self) -> list[int]:
        with self._lock:
            return self._session.bitmap.missing() if self._session else []

    # ---------------------------------------------------------------- 再開
    @staticmethod
    def list_incomplete(sessions_dir: str | os.PathLike | None = None) -> list[IncompleteSession]:
        root = Path(sessions_dir) if sessions_dir is not None else default_sessions_dir()
        out: list[IncompleteSession] = []
        if not root.is_dir():
            return out
        for d in root.iterdir():
            try:
                state = json.loads((d / "state.json").read_text())
                sid = int(state["session_id"])
                total = int(state["total"])
                meta = None
                if (d / "meta.json").exists():
                    meta = packer.parse_meta(
                        json.dumps(json.loads((d / "meta.json").read_text(encoding="utf-8"))).encode())
                bm = Bitmap(total, (d / "bitmap.bin").read_bytes() if (d / "bitmap.bin").exists() else None)
                out.append(IncompleteSession(sid, d, total, bm.count, meta, float(state.get("updated", 0))))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        out.sort(key=lambda x: x.updated, reverse=True)
        return out

    @staticmethod
    def delete_session_dir(path: str | os.PathLike) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def resume(self, session_id: int) -> bool:
        """ディスク上の未完了セッションを現在のセッションとして読み込む。完了していればすぐ完了処理を行う。"""
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            if self._session is not None:
                self._session.flush()
                self._session.close()
                self._session = None
            ok = self._try_load(session_id)
            if ok:
                self._maybe_complete(callbacks)
        for cb in callbacks:
            cb()
        return ok

    def _try_load(self, session_id: int) -> bool:
        d = self.sessions_dir / session_dirname(session_id)
        try:
            state = json.loads((d / "state.json").read_text())
            total = int(state["total"])
            s = _Session(self.sessions_dir, session_id, total)
            chunk_size = state.get("chunk_size")
            s.chunk_size = int(chunk_size) if chunk_size else None
            if (d / "meta.json").exists():
                raw = (d / "meta.json").read_text(encoding="utf-8")
                meta = packer.parse_meta(json.dumps(json.loads(raw), ensure_ascii=False).encode("utf-8"))
                if meta is not None and meta["total"] == total:
                    s.meta = meta
                    s.chunk_size = meta["chunk_size"]
            if (d / "bitmap.bin").exists() and s.chunk_size is not None:
                s.bitmap = Bitmap(total, (d / "bitmap.bin").read_bytes())
            chunks = d / "chunks.bin"
            if s.bitmap.count and not chunks.exists():
                s.bitmap = Bitmap(total)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self._session = s
        return True

    # ---------------------------------------------------------------- 内部処理
    def _feed_locked(self, frame: protocol.Frame, callbacks: list[Callable[[], None]]) -> str:
        sid = frame.session_id
        if sid in self._finished_ids:
            return ST_FINISHED
        s = self._session
        if s is None:
            if frame.is_data and frame.seq >= frame.total:
                return ST_INVALID
            if not self._try_load(sid) or self._session.total != frame.total:
                self._session = _Session(self.sessions_dir, sid, frame.total)
            s = self._session
            self._conflict_id = None
            self._notified_conflicts.clear()
        elif sid != s.session_id:
            if s.finished:
                # 完了済みの表示中に新しい転送が来たら、そのまま採用する
                self.reset()
                return self._feed_locked(frame, callbacks)
            self._conflict_id = sid
            self._conflict_total = frame.total
            if sid not in self._notified_conflicts:
                self._notified_conflicts.add(sid)
                if self.callbacks.on_conflict:
                    callbacks.append(lambda: self.callbacks.on_conflict(sid))
            return ST_CONFLICT

        if s.finished:
            return ST_FINISHED
        if frame.total != s.total:
            return ST_INVALID

        if frame.is_meta:
            status = self._handle_meta(s, frame, callbacks)
        else:
            status = self._handle_data(s, frame)
        if status in (ST_NEW, ST_META):
            done = self._maybe_complete(callbacks)
            if done is not None:
                return ST_COMPLETE if done.ok else ST_FAILED
            if s.unflushed >= FLUSH_EVERY_CHUNKS or time.monotonic() - s.last_flush >= FLUSH_INTERVAL_SEC:
                s.flush()
        return status

    def _handle_meta(self, s: _Session, frame: protocol.Frame, callbacks: list[Callable[[], None]]) -> str:
        if s.meta is not None:
            return ST_DUP
        meta = packer.parse_meta(frame.payload)
        if meta is None or meta["total"] != s.total:
            return ST_INVALID
        if s.chunk_size is not None and s.chunk_size != meta["chunk_size"]:
            return ST_INVALID
        s.meta = meta
        s.chunk_size = meta["chunk_size"]
        s.write_meta()
        # 既に受け取っていたチャンクの長さを検証し、保留分を書き出す
        for seq, data in list(s.pending.items()):
            if not self._length_ok(s, seq, len(data)):
                s.bitmap.clear(seq)
            else:
                s.write_chunk(seq, data)
        s.pending.clear()
        s.unflushed += 1
        s.flush()
        if self.callbacks.on_meta:
            m = dict(meta)
            callbacks.append(lambda: self.callbacks.on_meta(m))
        return ST_META

    @staticmethod
    def _length_ok(s: _Session, seq: int, n: int) -> bool:
        if s.meta is not None:
            cs = s.meta["chunk_size"]
            if seq < s.total - 1:
                return n == cs
            return n == s.meta["payload_size"] - (s.total - 1) * cs
        if s.chunk_size is not None:
            return n == s.chunk_size if seq < s.total - 1 else 0 < n <= s.chunk_size
        return n > 0

    def _handle_data(self, s: _Session, frame: protocol.Frame) -> str:
        seq, data = frame.seq, frame.payload
        if seq >= s.total:
            return ST_INVALID
        if s.bitmap.get(seq):
            return ST_DUP
        if not data:
            return ST_INVALID
        if s.chunk_size is None and seq < s.total - 1:
            # 最終チャンク以外の長さ = chunk_size
            if not packer.CHUNK_SIZE_MIN <= len(data) <= packer.CHUNK_SIZE_MAX * 4:
                return ST_INVALID
            if any((sq < s.total - 1 and len(d) != len(data)) or len(d) > len(data)
                   for sq, d in s.pending.items()):
                return ST_INVALID
            s.chunk_size = len(data)
            s.write_state()
            for sq, d in list(s.pending.items()):
                s.write_chunk(sq, d)
            s.pending.clear()
        if not self._length_ok(s, seq, len(data)):
            return ST_INVALID
        if s.chunk_size is None:
            if any(len(d) != len(data) and sq < s.total - 1 for sq, d in s.pending.items()):
                return ST_INVALID
            s.pending[seq] = data
        else:
            s.write_chunk(seq, data)
        s.bitmap.set(seq)
        s.unflushed += 1
        now = time.monotonic()
        s.times.append(now)
        while now - s.times[0] > RATE_WINDOW_SEC:
            s.times.popleft()
        return ST_NEW

    def _maybe_complete(self, callbacks: list[Callable[[], None]]) -> CompletionResult | None:
        s = self._session
        if s is None or s.finished or s.meta is None or not s.bitmap.complete:
            return None
        s.flush()
        result = self._finalize(s)
        s.finished = True
        s.close()
        self._finished_ids.add(s.session_id)
        s.delete()  # 完了（成功/失敗とも）したセッションは削除する
        self.last_result = result
        if self.callbacks.on_complete:
            callbacks.append(lambda: self.callbacks.on_complete(result))
        return result

    def _finalize(self, s: _Session) -> CompletionResult:
        meta = s.meta
        assert meta is not None
        elapsed = time.monotonic() - s.started
        base = dict(session_id=s.session_id, name=meta["name"], raw_size=meta["raw_size"],
                    payload_size=meta["payload_size"], elapsed_sec=elapsed)
        try:
            s.close()
            chunks = s.dir / "chunks.bin"
            payload = b""
            if meta["payload_size"] > 0:
                with open(chunks, "rb") as f:
                    payload = f.read(meta["payload_size"])
            if len(payload) != meta["payload_size"] or packer.sha256_hex(payload) != meta["sha256_payload"]:
                return CompletionResult(False, message="NG: 結合後のデータの SHA-256 が一致しません", **base)
            try:
                raw = packer.decompress(meta["compression"], payload, meta["raw_size"])
            except (ValueError, OSError, EOFError) as e:
                return CompletionResult(False, message=f"NG: 伸長に失敗しました ({e})", **base)
            if len(raw) != meta["raw_size"] or packer.sha256_hex(raw) != meta["sha256_raw"]:
                return CompletionResult(False, message="NG: 元データの SHA-256 が一致しません", **base)
            return self._save(meta, raw, base)
        except OSError as e:
            return CompletionResult(False, message=f"NG: 保存に失敗しました ({e})", **base)

    def _save(self, meta: dict, raw: bytes, base: dict) -> CompletionResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        name = extractor.sanitize_filename(meta["name"]) or "received"
        if meta["mode"] == packer.MODE_FILE:
            target = extractor.unique_path(self.output_dir / name)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(raw)
            os.replace(tmp, target)
            if meta.get("mtime") is not None:
                try:
                    os.utime(target, (meta["mtime"], meta["mtime"]))
                except (OSError, OverflowError, ValueError):
                    pass
            result = CompletionResult(True, message="OK: SHA-256 一致", path=target, sha256_ok=True, **base)
            if self.auto_extract_zip and target.suffix.lower() == ".zip":
                dest = extractor.unique_path(target.with_suffix(""))
                try:
                    result.zip_report = extractor.extract_zip(target, dest)
                    result.message += f"／ZIP を展開しました: {dest}"
                except Exception as e:  # noqa: BLE001 - zip の不正は保存済みファイルに影響させない
                    result.message += f"／ZIP の展開に失敗しました: {e}"
            return result
        # bundle
        dest = extractor.unique_path(self.output_dir / name)
        report = extractor.extract_tar(raw, dest)
        msg = "OK: SHA-256 一致"
        if report.skipped:
            msg += f"／{len(report.skipped)} 件の項目を安全のためスキップしました"
        return CompletionResult(True, message=msg, path=dest, sha256_ok=True, extract_report=report, **base)
