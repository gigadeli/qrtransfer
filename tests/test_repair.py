import hashlib
import os
import random

import pytest

from qrtransfer import assembler, packer, protocol, repair

from conftest import make_assembler


def _plan(tmp_path, size: int, chunk_size: int = 300) -> tuple[packer.TransferPlan, bytes]:
    src = tmp_path / "src" / "data.bin"
    src.parent.mkdir(exist_ok=True)
    content = os.urandom(size)
    src.write_bytes(content)
    return packer.make_plan([src], chunk_size=chunk_size), content


def _carousel(plan: packer.TransferPlan, repairs: int) -> list[bytes]:
    frames = {packer.CAROUSEL_META: plan.meta_frame()}
    frames.update({i: plan.data_frame(i) for i in range(plan.total)})
    frames.update({packer.carousel_repair(r): f for r, f in enumerate(plan.repair_frames(repairs))})
    return [frames[s] for s in packer.carousel_order(plan.total, repairs=repairs)]


def test_indices_are_deterministic_and_distinct():
    for total in (1, 2, 3, 10, 2047, 2048, 2049, 50_000):
        d = repair.degree(total)
        for r in (0, 1, 7, 12345):
            idx = repair.indices(0x89ABCDEF, r, total)
            assert idx == repair.indices(0x89ABCDEF, r, total)
            assert len(idx) == d == len(set(idx))
            assert idx == sorted(idx) and 0 <= idx[0] and idx[-1] < total
    assert repair.degree(10) == 5 and repair.degree(1) == 1 and repair.degree(100_000) == repair.DEGREE_MAX
    # 番号が違えば別の組み合わせ
    assert repair.indices(1, 0, 1000) != repair.indices(1, 1, 1000)
    assert repair.indices(1, 0, 1000) != repair.indices(2, 0, 1000)


def test_repair_count():
    assert repair.repair_count(100, 0) == 0
    assert repair.repair_count(100, 50) == 50
    assert repair.repair_count(3, 50) == 4  # 少ないときも最低限は作る
    assert repair.repair_count(0, 50) == 0
    assert repair.repair_count(repair.TOTAL_LIMIT, 50) == 0


def test_repair_frame_roundtrip():
    f = protocol.build_repair_frame(7, 3, 10, b"x" * 300)
    fr = protocol.parse_frame(f)
    assert fr.is_repair and not fr.is_data and fr.seq == 3 and fr.total == 10
    # 修復用フレームの番号は総チャンク数を超えてもよい
    assert protocol.parse_frame(protocol.build_repair_frame(7, 50, 10, b"x")).seq == 50


def test_carousel_order_with_repairs():
    order = packer.carousel_order(30, repairs=12)
    assert order[0] == packer.CAROUSEL_META
    body = [s for s in order if s != packer.CAROUSEL_META]
    assert body == list(range(30)) + [packer.carousel_repair(r) for r in range(12)]
    assert [packer.repair_index(s) for s in body[30:]] == list(range(12))
    assert packer.repair_index(5) is None and packer.repair_index(packer.CAROUSEL_META) is None
    # 再送（番号指定）では修復用フレームを付けない
    assert packer.carousel_order(30, {1, 2}, repairs=12) == [packer.CAROUSEL_META, 1, 2]
    assert packer.estimate_frames_per_cycle(30, 12) == len(order)


@pytest.mark.parametrize("loss", [0.05, 0.3, 0.5])
def test_decoder_recovers_from_any_losses(loss):
    rng = random.Random(int(loss * 100))
    cs = 200
    payload = os.urandom(cs * 300 + 77)  # 最終チャンクは短い
    chunks = packer.split_chunks(payload, cs)
    total = len(chunks)
    reps = repair.encode(chunks, cs, 0x1234, total * 2)
    stream = [("d", i, c) for i, c in enumerate(chunks)] + [("r", r, p) for r, p in enumerate(reps)]
    got: dict[int, bytes] = {}
    dec = repair.Decoder(total, cs)
    used = 0
    for kind, i, data in stream:
        if rng.random() < loss:
            continue
        used += 1
        if kind == "d":
            got[i] = data
            solved = dec.add_known(i, data)
        else:
            solved = dec.add_repair(repair.indices(0x1234, i, total), data, got.get)
        for q, d in solved:
            assert q not in got
            got[q] = d
        if len(got) == total:
            break
    assert len(got) == total
    assert b"".join(got[i] for i in range(total))[:len(payload)] == payload
    assert used <= total + 10  # 全チャンク数をわずかに超える枚数で解ける


def test_assembler_completes_without_resend(tmp_path):
    """DATA の 3 割を取りこぼしても、同じ周の修復用フレームだけで完了する（再送の指示が要らない）。"""
    plan, content = _plan(tmp_path, 40_000 + 123)
    repairs = repair.repair_count(plan.total, 60)
    asm, results = make_assembler(tmp_path)
    rng = random.Random(3)
    for f in _carousel(plan, repairs):
        fr = protocol.parse_frame(f)
        if fr.is_data and rng.random() < 0.3:
            continue
        asm.feed(fr)
        if results:
            break
    assert results and results[0].ok, results and results[0].message
    assert results[0].path.read_bytes() == content


def test_assembler_repairs_before_meta_are_ignored(tmp_path):
    plan, content = _plan(tmp_path, 3000)
    asm, results = make_assembler(tmp_path)
    reps = [protocol.parse_frame(f) for f in plan.repair_frames(8)]
    assert asm.feed(reps[0]) == assembler.ST_DUP  # META の前は使わない
    asm.feed(protocol.parse_frame(plan.meta_frame()))
    # DATA を 1 枚も受け取らなくても、修復用フレームだけで解ける（小さな転送）
    for fr in reps + [protocol.parse_frame(f) for f in plan.repair_frames(40)[8:]]:
        asm.feed(fr)
        if results:
            break
    assert results and results[0].ok
    assert results[0].path.read_bytes() == content


def test_assembler_rejects_wrong_repair_length(tmp_path):
    plan, _ = _plan(tmp_path, 3000)
    asm, _ = make_assembler(tmp_path)
    asm.feed(protocol.parse_frame(plan.meta_frame()))
    bad = protocol.build_repair_frame(plan.session_id, 0, plan.total, b"x" * (plan.chunk_size - 1))
    assert asm.feed_bytes(bad) == assembler.ST_INVALID


def test_decoder_memory_limit():
    dec = repair.Decoder(1000, 200, memory_limit=10 * (1000 // 8 + 200 + 64))
    assert dec.max_rows == 10
    for r in range(30):
        dec.add_repair(repair.indices(9, r, 1000), os.urandom(200), lambda i: None)
    assert len(dec.rows) == 10 and dec.dropped == 20


def test_old_receiver_ignores_repair_frames(tmp_path):
    """REPAIR を知らない受信側（parse_frame が None を返す実装）でも、DATA だけで従来どおり受信できることの確認用。"""
    plan, content = _plan(tmp_path, 5000)
    frames = _carousel(plan, 20)
    kinds = [protocol.parse_frame(f).type for f in frames]
    assert kinds.count(protocol.TYPE_REPAIR) == 20
    data_only = [f for f, k in zip(frames, kinds) if k != protocol.TYPE_REPAIR]
    asm, results = make_assembler(tmp_path)
    for f in data_only:
        asm.feed_bytes(f)
    assert results[0].ok and hashlib.sha256(results[0].path.read_bytes()).hexdigest() == plan.meta["sha256_raw"]
