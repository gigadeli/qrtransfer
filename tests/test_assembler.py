import hashlib
import os
import random

import pytest

from qrtransfer import assembler, packer, protocol

from conftest import feed_chaotic, make_assembler, plan_frames, read_tree, write_tree


def _noise_frames(rng, n=30):
    """別セッションのフレーム。"""
    other = packer.TransferPlan(session_id=0x0BADF00D, meta={}, payload=b"", chunk_size=200,
                                chunks=[os.urandom(200) for _ in range(5)])
    return [other.data_frame(i % 5) for i in range(n)]


def test_single_file_chaos(tmp_path, rng):
    src = tmp_path / "src" / "データ.bin"
    src.parent.mkdir()
    content = os.urandom(20_000) + b"a" * 5000
    src.write_bytes(content)
    os.utime(src, (1700000000, 1700000000))
    plan = packer.make_plan([src], chunk_size=300)
    asm, results = make_assembler(tmp_path)
    meta, data = plan_frames(plan)
    conflicts = []
    asm.callbacks.on_conflict = conflicts.append
    feed_chaotic(asm, meta, data, rng, noise=_noise_frames(rng))
    assert len(results) == 1
    r = results[0]
    assert r.ok, r.message
    assert r.path.read_bytes() == content
    assert hashlib.sha256(r.path.read_bytes()).hexdigest() == plan.meta["sha256_raw"]
    assert int(r.path.stat().st_mtime) == 1700000000
    assert r.path.name == "データ.bin"
    # 完了後のセッションディレクトリは削除される
    assert not (tmp_path / "sessions" / assembler.session_dirname(plan.session_id)).exists()
    # 完了後に同じセッションのフレームが来ても無視される
    assert asm.feed_bytes(data[0]) == assembler.ST_FINISHED


def test_bundle_chaos(tmp_path, rng):
    root = tmp_path / "src" / "project"
    files = {"readme.md": b"# hi\n" * 100, "深い/階層/の/ファイル.txt": "テキスト".encode() * 300,
             "bin/data.bin": os.urandom(3000), "empty.txt": b""}
    write_tree(root, files, empty_dirs=["空フォルダ", "深い/空"])
    plan = packer.make_plan([root], chunk_size=500)
    asm, results = make_assembler(tmp_path)
    meta, data = plan_frames(plan)
    feed_chaotic(asm, meta, data, rng, noise=_noise_frames(rng))
    r = results[0]
    assert r.ok, r.message
    got_files, got_dirs = read_tree(r.path)
    assert got_files == files
    assert {"空フォルダ", "深い/空"} <= got_dirs
    assert r.path.name == "project"


def test_multiple_files_bundle(tmp_path, rng):
    write_tree(tmp_path / "src", {"a.txt": b"aaa" * 1000, "b.txt": os.urandom(1000)})
    plan = packer.make_plan([tmp_path / "src" / "a.txt", tmp_path / "src" / "b.txt"], chunk_size=200)
    asm, results = make_assembler(tmp_path)
    feed_chaotic(asm, *plan_frames(plan), rng)
    r = results[0]
    assert r.ok
    got, _ = read_tree(r.path)
    assert got == {"a.txt": b"aaa" * 1000, "b.txt": (tmp_path / "src" / "b.txt").read_bytes()}
    assert r.path.name.startswith("bundle_")


def test_zero_byte_file(tmp_path, rng):
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    plan = packer.make_plan([f])
    asm, results = make_assembler(tmp_path)
    noise = _noise_frames(rng, 5)
    for fr in noise:
        asm.feed_bytes(fr)  # 別セッションが先に採用される
    asm.reset()
    asm.discard_current()
    status = asm.feed_bytes(plan.meta_frame())
    assert status == assembler.ST_COMPLETE
    assert results[0].ok and results[0].path.read_bytes() == b""


def test_meta_arrives_last(tmp_path, rng):
    f = tmp_path / "x.bin"
    f.write_bytes(os.urandom(4321))
    plan = packer.make_plan([f], chunk_size=250)
    asm, results = make_assembler(tmp_path)
    meta, data = plan_frames(plan)
    order = list(data)
    rng.shuffle(order)
    for fr in order:
        assert asm.feed_bytes(fr) == assembler.ST_NEW
    assert not results
    assert asm.feed_bytes(meta) == assembler.ST_COMPLETE
    assert results[0].ok and results[0].path.read_bytes() == f.read_bytes()


def test_last_chunk_first_before_meta(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(os.urandom(1000))
    plan = packer.make_plan([f], chunk_size=300)  # 300,300,300,100
    asm, results = make_assembler(tmp_path)
    meta, data = plan_frames(plan)
    for i in (3, 1, 0, 2):
        asm.feed_bytes(data[i])
    asm.feed_bytes(meta)
    assert results[0].ok


def test_duplicates_ignored(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(os.urandom(1000))
    plan = packer.make_plan([f], chunk_size=300)
    asm, _ = make_assembler(tmp_path)
    meta, data = plan_frames(plan)
    assert asm.feed_bytes(data[0]) == assembler.ST_NEW
    assert asm.feed_bytes(data[0]) == assembler.ST_DUP
    assert asm.feed_bytes(meta) == assembler.ST_META
    assert asm.feed_bytes(meta) == assembler.ST_DUP
    assert asm.snapshot().received == 1


def test_conflict_and_switch(tmp_path):
    f1 = tmp_path / "one.bin"
    f1.write_bytes(os.urandom(1000))
    f2 = tmp_path / "two.bin"
    f2.write_bytes(os.urandom(700))
    p1 = packer.make_plan([f1], chunk_size=300, session_id=1)
    p2 = packer.make_plan([f2], chunk_size=300, session_id=2)
    asm, results = make_assembler(tmp_path)
    conflicts = []
    asm.callbacks.on_conflict = conflicts.append
    asm.feed_bytes(p1.data_frame(0))
    assert asm.feed_bytes(p2.data_frame(0)) == assembler.ST_CONFLICT
    assert asm.feed_bytes(p2.data_frame(1)) == assembler.ST_CONFLICT
    assert conflicts == [2]  # 通知は 1 回だけ
    assert asm.snapshot().conflict_session == 2
    assert asm.switch_to_conflict()
    assert asm.session_id == 2
    for fr in [p2.meta_frame()] + [p2.data_frame(i) for i in range(p2.total)]:
        asm.feed_bytes(fr)
    assert results[-1].ok and results[-1].path.read_bytes() == f2.read_bytes()
    # 切り替え前のセッションはディスクに残っていて、再開できる
    incomplete = assembler.Assembler.list_incomplete(tmp_path / "sessions")
    assert [s.session_id for s in incomplete] == [1]
    assert incomplete[0].received == 1


def test_resume_after_restart(tmp_path, rng):
    f = tmp_path / "big.bin"
    f.write_bytes(os.urandom(30_000))
    plan = packer.make_plan([f], chunk_size=400)
    meta, data = plan_frames(plan)
    asm1, results1 = make_assembler(tmp_path)
    order = list(range(plan.total))
    rng.shuffle(order)
    half = order[: plan.total // 2]
    asm1.feed_bytes(meta)
    for i in half:
        asm1.feed_bytes(data[i])
    asm1.close()  # アプリ終了
    del asm1

    incomplete = assembler.Assembler.list_incomplete(tmp_path / "sessions")
    assert len(incomplete) == 1 and incomplete[0].received == len(half)
    assert incomplete[0].meta["name"] == "big.bin"

    asm2, results2 = make_assembler(tmp_path)
    assert asm2.resume(plan.session_id)
    assert asm2.snapshot().received == len(half)
    assert set(asm2.missing()) == set(order[plan.total // 2:])
    for i in order:
        asm2.feed_bytes(data[i])
    assert results2[0].ok and results2[0].path.read_bytes() == f.read_bytes()
    assert assembler.Assembler.list_incomplete(tmp_path / "sessions") == []


def test_resume_without_meta_and_crash_without_close(tmp_path):
    f = tmp_path / "c.bin"
    f.write_bytes(os.urandom(5000))
    plan = packer.make_plan([f], chunk_size=500)
    meta, data = plan_frames(plan)
    asm1, _ = make_assembler(tmp_path)
    for i in range(0, plan.total, 2):
        asm1.feed_bytes(data[i])
    asm1.flush()  # 定期 flush 相当（close せずに異常終了したとみなす）
    received = asm1.snapshot().received

    asm2, results2 = make_assembler(tmp_path)
    # 同じセッションのフレームが来たら、ディスクの状態から自動的に続きを受信する
    asm2.feed_bytes(data[1])
    assert asm2.snapshot().received == received + 1
    for fr in data + [meta]:
        asm2.feed_bytes(fr)
    assert results2[0].ok and results2[0].path.read_bytes() == f.read_bytes()


def test_tampered_payload_fails(tmp_path):
    f = tmp_path / "t.bin"
    f.write_bytes(os.urandom(3000))
    plan = packer.make_plan([f], chunk_size=1000)
    # CRC は正しいが中身を改ざんしたフレーム
    bad_chunk = bytearray(plan.chunks[1])
    bad_chunk[10] ^= 0xFF
    bad = protocol.build_data_frame(plan.session_id, 1, plan.total, bytes(bad_chunk))
    asm, results = make_assembler(tmp_path)
    for fr in [plan.meta_frame(), plan.data_frame(0), bad, plan.data_frame(2)]:
        asm.feed_bytes(fr)
    r = results[0]
    assert not r.ok and "SHA-256" in r.message
    assert not (tmp_path / "out" / "t.bin").exists()


def test_tampered_raw_hash_fails(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"hello world " * 500)
    plan = packer.make_plan([f], chunk_size=200)
    plan.meta["sha256_raw"] = "0" * 64
    asm, results = make_assembler(tmp_path)
    for fr in [plan.meta_frame()] + [plan.data_frame(i) for i in range(plan.total)]:
        asm.feed_bytes(fr)
    assert not results[0].ok
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_wrong_length_chunk_rejected(tmp_path):
    f = tmp_path / "w.bin"
    f.write_bytes(os.urandom(2000))
    plan = packer.make_plan([f], chunk_size=500)
    asm, _ = make_assembler(tmp_path)
    asm.feed_bytes(plan.meta_frame())
    short = protocol.build_data_frame(plan.session_id, 0, plan.total, b"x" * 499)
    assert asm.feed_bytes(short) == assembler.ST_INVALID
    wrong_total = protocol.build_data_frame(plan.session_id, 0, plan.total + 1, plan.chunks[0])
    assert asm.feed_bytes(wrong_total) == assembler.ST_INVALID


def test_same_name_gets_unique(tmp_path):
    f = tmp_path / "same.txt"
    f.write_bytes(b"one")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "same.txt").write_bytes(b"existing")
    plan = packer.make_plan([f], chunk_size=200)
    asm, results = make_assembler(tmp_path)
    for fr in [plan.meta_frame()] + [plan.data_frame(i) for i in range(plan.total)]:
        asm.feed_bytes(fr)
    assert results[0].path.name == "same (1).txt"
    assert (tmp_path / "out" / "same.txt").read_bytes() == b"existing"


def test_auto_extract_zip(tmp_path):
    import zipfile
    z = tmp_path / "arc.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("in/zip.txt", "zip content")
        zf.writestr("../evil.txt", "evil")
    plan = packer.make_plan([z], chunk_size=200)
    asm, results = make_assembler(tmp_path, auto_extract_zip=True)
    for fr in [plan.meta_frame()] + [plan.data_frame(i) for i in range(plan.total)]:
        asm.feed_bytes(fr)
    r = results[0]
    assert r.ok and r.path.name == "arc.zip"
    assert (tmp_path / "out" / "arc" / "in" / "zip.txt").read_text() == "zip content"
    assert not (tmp_path / "out" / "evil.txt").exists()
    assert r.zip_report.skipped


def test_bitmap():
    bm = assembler.Bitmap(13)
    assert bm.set(0) and bm.set(12) and not bm.set(12)
    assert bm.count == 2 and bm.get(12) and not bm.get(11)
    assert bm.missing() == list(range(1, 12))
    bm2 = assembler.Bitmap(13, bytes(bm.data))
    assert bm2.count == 2
    bm3 = assembler.Bitmap(13, b"\xff\xff")  # 余分なビットは数えない
    assert bm3.count == 13 and bm3.complete
