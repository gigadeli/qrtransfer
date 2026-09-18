"""tar / zip の安全な展開、ファイル名の無害化、別名保存。"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
             "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "CONIN$", "CONOUT$"}
_DRIVE = re.compile(r"^[A-Za-z]:")

MAX_COMPONENT_LEN = 200


class UnsafePathError(ValueError):
    pass


@dataclass
class ExtractReport:
    root: Path
    files: int = 0
    dirs: int = 0
    total_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (アーカイブ内の名前, 理由)


def sanitize_component(name: str) -> str:
    """Windows で使えないファイル名の文字・予約名を置換する（パス区切りは含まない前提）。"""
    name = _INVALID_CHARS.sub("_", name).replace("/", "_").replace("\\", "_")
    name = name.rstrip(" .")
    if not name:
        name = "_"
    stem = name.split(".", 1)[0].rstrip(" ")
    if stem.upper() in _RESERVED:
        name = "_" + name
    if len(name) > MAX_COMPONENT_LEN:
        base, ext = os.path.splitext(name)
        ext = ext[:20]
        name = base[:MAX_COMPONENT_LEN - len(ext)] + ext
    return name


def sanitize_filename(name: str) -> str:
    """単一のファイル名として安全な名前にする（区切り文字も置換する）。"""
    return sanitize_component(name.replace("/", "_").replace("\\", "_"))


def split_member_path(name: str) -> list[str]:
    """アーカイブ内のパスを検証し、無害化した構成要素のリストを返す。危険なら UnsafePathError。"""
    if not name:
        raise UnsafePathError("empty name")
    if "\x00" in name:
        raise UnsafePathError("NUL in name")
    norm = name.replace("\\", "/")
    if norm.startswith("/") or _DRIVE.match(norm):
        raise UnsafePathError("absolute path")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafePathError("parent directory reference")
    # "..." や末尾ドット/空白で ".." と同一視される名前（Windows）も拒否
    if any(p.rstrip(" .") == "" for p in parts):
        raise UnsafePathError("dot-only component")
    if not parts:
        raise UnsafePathError("empty path")
    return [sanitize_component(p) for p in parts]


def safe_join(root: Path, parts: list[str]) -> Path:
    root_resolved = root.resolve()
    target = root_resolved.joinpath(*parts).resolve()
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise UnsafePathError("escapes extraction root")
    return target


def unique_path(path: Path) -> Path:
    """同名があれば `name (1).ext` のような別名を返す（フォルダにも使う）。"""
    if not path.exists():
        return path
    stem, suffix = (path.name, "") if path.is_dir() else (path.stem, path.suffix)
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def _write_stream(src: BinaryIO, dest: Path, size_limit: int | None = None) -> int:
    tmp = dest.with_name(dest.name + ".part")
    written = 0
    try:
        with open(tmp, "wb") as f:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                written += len(buf)
                if size_limit is not None and written > size_limit:
                    raise UnsafePathError("entry larger than declared size")
                f.write(buf)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return written


def extract_tar(source: bytes | str | os.PathLike | BinaryIO, dest: str | os.PathLike) -> ExtractReport:
    """非圧縮 tar を dest に安全に展開する。通常ファイルとフォルダのみ作成し、危険な項目はスキップする。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    report = ExtractReport(root=dest)
    if isinstance(source, (bytes, bytearray)):
        tar = tarfile.open(fileobj=io.BytesIO(source), mode="r:")
    elif hasattr(source, "read"):
        tar = tarfile.open(fileobj=source, mode="r:")  # type: ignore[arg-type]
    else:
        tar = tarfile.open(source, mode="r:")
    dir_times: list[tuple[Path, float]] = []
    with tar:
        for member in tar:
            name = member.name
            if not (member.isreg() or member.isdir()):
                report.skipped.append((name, "リンク/特殊ファイルは作成しません"))
                continue
            try:
                # Python 3.12 の data フィルタで検査したうえで、自前の検査も行う
                split_member_path(name)  # data フィルタは先頭の "/" を除去して通すため、元の名前で先に検査する
                member = tarfile.data_filter(member, str(dest))
                parts = split_member_path(member.name)
                target = safe_join(dest, parts)
            except (tarfile.FilterError, UnsafePathError) as e:
                report.skipped.append((name, f"危険なパス: {e}"))
                continue
            try:
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    report.dirs += 1
                    dir_times.append((target, member.mtime))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.is_dir():
                        report.skipped.append((name, "同名のフォルダがあります"))
                        continue
                    target = unique_path(target)
                src = tar.extractfile(member)
                if src is None:
                    report.skipped.append((name, "読み取れません"))
                    continue
                with src:
                    report.total_bytes += _write_stream(src, target, member.size)
                try:
                    os.utime(target, (member.mtime, member.mtime))
                except (OSError, OverflowError, ValueError):
                    pass
                report.files += 1
            except OSError as e:
                report.skipped.append((name, f"書き込みエラー: {e}"))
    for d, mtime in reversed(dir_times):
        try:
            os.utime(d, (mtime, mtime))
        except (OSError, OverflowError, ValueError):
            pass
    return report


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return info.create_system == 3 and stat.S_ISLNK(mode)


def extract_zip(source: str | os.PathLike | BinaryIO, dest: str | os.PathLike,
                max_total_bytes: int = 64 * 1024 ** 3) -> ExtractReport:
    """zip を dest に安全に展開する。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    report = ExtractReport(root=dest)
    with zipfile.ZipFile(source) as zf:
        for info in zf.infolist():
            name = info.filename
            if _zip_is_symlink(info):
                report.skipped.append((name, "リンクは作成しません"))
                continue
            mode = info.external_attr >> 16
            if info.create_system == 3 and mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                report.skipped.append((name, "特殊ファイルは作成しません"))
                continue
            try:
                parts = split_member_path(name)
                target = safe_join(dest, parts)
            except UnsafePathError as e:
                report.skipped.append((name, f"危険なパス: {e}"))
                continue
            try:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    report.dirs += 1
                    continue
                if report.total_bytes + info.file_size > max_total_bytes:
                    report.skipped.append((name, "展開後のサイズが大きすぎます"))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.is_dir():
                        report.skipped.append((name, "同名のフォルダがあります"))
                        continue
                    target = unique_path(target)
                with zf.open(info) as src:
                    report.total_bytes += _write_stream(src, target, info.file_size)
                try:
                    ts = time.mktime(info.date_time + (0, 0, -1))
                    os.utime(target, (ts, ts))
                except (OSError, OverflowError, ValueError):
                    pass
                report.files += 1
            except (OSError, zipfile.BadZipFile, UnsafePathError) as e:
                report.skipped.append((name, f"書き込みエラー: {e}"))
    return report


def remove_tree(path: str | os.PathLike) -> None:
    shutil.rmtree(path, ignore_errors=True)
