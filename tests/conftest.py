import os
import random
from pathlib import Path

import pytest

from qrtransfer import assembler, packer, protocol


@pytest.fixture
def rng():
    return random.Random(12345)


def plan_frames(plan: packer.TransferPlan) -> tuple[bytes, list[bytes]]:
    """(META フレーム, DATA フレームのリスト)"""
    return plan.meta_frame(), [plan.data_frame(i) for i in range(plan.total)]


def make_assembler(tmp_path: Path, **kw) -> tuple[assembler.Assembler, list]:
    results: list = []
    cb = assembler.AssemblerCallbacks(on_complete=results.append)
    asm = assembler.Assembler(tmp_path / "out", tmp_path / "sessions", callbacks=cb, **kw)
    return asm, results


def feed_chaotic(asm: assembler.Assembler, meta: bytes, data: list[bytes], rng: random.Random,
                 noise: list[bytes] = (), drop_rate: float = 0.3, meta_last: bool = False,
                 max_rounds: int = 200) -> None:
    """欠落・順序入れ替え・重複・別セッション混入を与えながら、完了するまでフレームを流す。"""
    for _ in range(max_rounds):
        stream = list(data) * 2 + list(noise)
        if not meta_last:
            stream += [meta] * 3
        rng.shuffle(stream)
        for fr in stream:
            if rng.random() < drop_rate:
                continue
            if rng.random() < 0.05:
                # ビット化けしたフレーム（CRC で破棄されるはず）
                b = bytearray(fr)
                b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
                fr = bytes(b)
            asm.feed_bytes(fr)
        if meta_last:
            snap = asm.snapshot()
            if snap.total is not None and snap.received == snap.total:
                asm.feed_bytes(meta)
        if asm.last_result is not None:
            return
    raise AssertionError("transfer did not complete")


def write_tree(root: Path, files: dict[str, bytes], empty_dirs: list[str] = ()) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    for d in empty_dirs:
        (root / d).mkdir(parents=True, exist_ok=True)


def read_tree(root: Path) -> tuple[dict[str, bytes], set[str]]:
    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            dirs.add((Path(dirpath) / d).relative_to(root).as_posix())
        for f in filenames:
            p = Path(dirpath) / f
            files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files, dirs
