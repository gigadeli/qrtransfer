import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from qrtransfer import extractor

from conftest import read_tree


def _add(tar: tarfile.TarFile, name: str, data: bytes = b"x", **attrs):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for k, v in attrs.items():
        setattr(info, k, v)
    tar.addfile(info, io.BytesIO(data) if info.isreg() else None)


def _evil_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        _add(t, "ok/good.txt", b"good")
        _add(t, "../evil.txt", b"evil")
        _add(t, "ok/../../evil2.txt", b"evil")
        _add(t, "C:\\x", b"drive")
        _add(t, "C:/y", b"drive")
        _add(t, "/abs", b"abs")
        _add(t, "\\abs2", b"abs")
        _add(t, "link", b"", type=tarfile.SYMTYPE, linkname="../../etc/passwd")
        _add(t, "hard", b"", type=tarfile.LNKTYPE, linkname="ok/good.txt")
        _add(t, "dev", b"", type=tarfile.CHRTYPE)
        _add(t, "fifo", b"", type=tarfile.FIFOTYPE)
        _add(t, "CON.txt", b"reserved")
        _add(t, "sub/aux", b"reserved2")
        _add(t, 'bad<>:"|?*name.txt', b"chars")
        _add(t, "trailing. ", b"trail")
        _add(t, "...", b"dots")
        _add(t, "emptydir", type=tarfile.DIRTYPE)
    return buf.getvalue()


def _assert_nothing_outside(base: Path, dest: Path):
    for p in base.rglob("*"):
        assert p == dest or dest in p.parents, p


def test_evil_tar(tmp_path):
    dest = tmp_path / "work" / "dest"
    report = extractor.extract_tar(_evil_tar(), dest)
    files, dirs = read_tree(dest)
    assert files["ok/good.txt"] == b"good"
    assert files["_CON.txt"] == b"reserved"
    assert files["sub/_aux"] == b"reserved2"
    assert files["bad_______name.txt"] == b"chars"
    assert files["trailing"] == b"trail"
    assert "emptydir" in dirs
    skipped = {n for n, _ in report.skipped}
    assert {"../evil.txt", "ok/../../evil2.txt", "C:\\x", "C:/y", "/abs", "\\abs2", "link", "hard", "dev",
            "fifo", "..."} <= skipped
    _assert_nothing_outside(tmp_path / "work", dest)
    assert not any(p.is_symlink() for p in dest.rglob("*"))
    assert "abs" not in files and "evil.txt" not in files


def test_symlink_in_dest_not_followed(tmp_path):
    # 展開先に既存のリンクがあっても、その外へ書き込まない（resolve チェック）
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, dest / "jump", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        _add(t, "jump/pwn.txt", b"pwn")
    report = extractor.extract_tar(buf.getvalue(), dest)
    assert not (outside / "pwn.txt").exists()
    assert report.skipped


def test_evil_zip(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok/good.txt", "good")
        zf.writestr("../evil.txt", "evil")
        zf.writestr("C:\\x", "drive")
        zf.writestr("/abs", "abs")
        zf.writestr("CON.txt", "reserved")
        zf.writestr("dir/", "")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "../../outside")
    dest = tmp_path / "work" / "dest"
    report = extractor.extract_zip(z, dest)
    files, dirs = read_tree(dest)
    assert files == {"ok/good.txt": b"good", "_CON.txt": b"reserved"}
    assert "dir" in dirs
    skipped = {n for n, _ in report.skipped}
    assert {"../evil.txt", "/abs", "link"} <= skipped
    assert "C:\\x" in skipped or "C:/x" in skipped  # zipfile は Windows で区切りを "/" に正規化する
    _assert_nothing_outside(tmp_path / "work", dest)


@pytest.mark.parametrize("name,expected", [
    ("normal.txt", "normal.txt"),
    ("CON", "_CON"),
    ("con.txt", "_con.txt"),
    ("COM1.tar.gz", "_COM1.tar.gz"),
    ("LPT9", "_LPT9"),
    ("CONSOLE.txt", "CONSOLE.txt"),
    ("a<b>c", "a_b_c"),
    ("x?", "x_"),
    ("end.", "end"),
    ("", "_"),
    ("日本語.txt", "日本語.txt"),
])
def test_sanitize(name, expected):
    assert extractor.sanitize_component(name) == expected


def test_unique_path(tmp_path):
    p = tmp_path / "a.txt"
    assert extractor.unique_path(p) == p
    p.write_text("1")
    assert extractor.unique_path(p).name == "a (1).txt"
    (tmp_path / "a (1).txt").write_text("2")
    assert extractor.unique_path(p).name == "a (2).txt"
    d = tmp_path / "folder.v1"
    d.mkdir()
    assert extractor.unique_path(d).name == "folder.v1 (1)"
