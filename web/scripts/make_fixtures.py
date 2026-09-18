"""Web 版のテスト用データを、デスクトップ版（Python）の送信処理で作る。

出力: web/test/fixtures/
  frames.json       … いろいろな送信内容（圧縮なし / zlib / lzma / フォルダ / 空ファイル / 危険なパス）のフレーム列と期待値
  qr_XX.png         … 1 つの送信内容を QR 画像にしたもの（zxing-wasm で読めるかの確認）
  blur.json/.bin    … 撮影を模した少しボケた画像（RGBA）。シャープ化で読めるかの確認

Python 版の変更で Web 版が読めなくなっていないかを、CI で毎回確かめるために使う。
    python web/scripts/make_fixtures.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from qrtransfer import packer, protocol, qrgen  # noqa: E402

OUT = ROOT / "web" / "test" / "fixtures"
SESSION = 0x89ABCDEF


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def plan_for(packed: packer.Packed, compression: str | None, chunk_size: int, session_id: int) -> packer.TransferPlan:
    if compression is None:
        return packer.make_plan_from_packed(packed, chunk_size, session_id)
    payload = packer.compress(compression, packed.raw)
    chunks = packer.split_chunks(payload, chunk_size)
    meta = {
        "name": packed.name, "mode": packed.mode, "compression": compression, "raw_size": len(packed.raw),
        "payload_size": len(payload), "chunk_size": chunk_size, "total": len(chunks),
        "sha256_raw": packer.sha256_hex(packed.raw), "sha256_payload": packer.sha256_hex(payload),
        "mtime": packed.mtime, "app_version": "fixture",
    }
    return packer.TransferPlan(session_id, meta, payload, chunk_size, chunks)


def case(case_id: str, packed: packer.Packed, compression: str | None, chunk_size: int = 300,
         session_id: int = SESSION, files: dict[str, str] | None = None, skipped: int = 0) -> dict:
    plan = plan_for(packed, compression, chunk_size, session_id)
    return {
        "id": case_id,
        "sessionId": session_id,
        "compression": plan.meta["compression"],
        "mode": packed.mode,
        "name": packed.name,
        "rawSha256": hashlib.sha256(packed.raw).hexdigest(),
        "files": files,
        "skipped": skipped,
        "meta": b64(plan.meta_frame()),
        "data": [b64(plan.data_frame(i)) for i in range(plan.total)],
    }


def make_tar(members: list[tuple[str, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.mtime = 1_700_000_000
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    text = ("QRTransfer のテストです。日本語と English が混ざったテキスト。\n" * 400).encode("utf-8")
    random_bytes = rng.integers(0, 256, 5000, dtype=np.uint8).tobytes()
    long_name = "とても長いファイル名" * 12 + ".txt"
    bundle_members = [
        ("docs", None),
        ("docs/readme.txt", b"hello\n"),
        ("docs/日本語 フォルダ/メモ.md", "# メモ\n".encode("utf-8")),
        (f"docs/{long_name}", b"long name\n"),  # 100 バイトを超える名前（PAX の path）
        ("empty", None),
        ("data.bin", random_bytes[:1234]),
    ]
    bundle = make_tar(bundle_members)
    evil = make_tar([("ok.txt", b"ok"), ("../evil.txt", b"x"), ("/abs.txt", b"x"), ("a/../../b.txt", b"x")])

    def files_of(members):
        return {n: hashlib.sha256(d).hexdigest() for n, d in members if d is not None}

    cases = [
        case("text_zlib", packer.Packed("メモ.txt", "file", text, 1_700_000_000), "zlib"),
        case("text_lzma", packer.Packed("memo.txt", "file", text, None), "lzma", chunk_size=800),
        case("random_none", packer.Packed("random.bin", "file", random_bytes, None), "none", chunk_size=200),
        case("auto", packer.Packed("auto.txt", "file", text[:3000], None), None),
        case("bundle", packer.Packed("フォルダ", "bundle", bundle, None), "zlib", chunk_size=500,
             files=files_of(bundle_members)),
        case("evil", packer.Packed("evil", "bundle", evil, None), "none", chunk_size=200,
             files={"ok.txt": hashlib.sha256(b"ok").hexdigest()}, skipped=3),
        case("empty", packer.Packed("empty.txt", "file", b"", None), "none"),
        case("other_session", packer.Packed("other.txt", "file", b"other session" * 50, None), "none",
             session_id=0x12345678),
    ]
    # 伸長爆弾: 伸長すると 50MB になるのに、META では 1MB と名乗る（伸長を途中で打ち切れるかの確認）
    for method in ("lzma", "zlib"):
        c = case(f"bomb_{method}", packer.Packed("bomb.bin", "file", b"\0" * 50_000_000, None), method,
                 chunk_size=2000, session_id=0x0B0B0000 + len(method))
        meta_frame = protocol.parse_frame(base64.b64decode(c["meta"]))
        meta = packer.parse_meta(meta_frame.payload)
        meta["raw_size"] = 1_000_000
        c["meta"] = b64(protocol.build_meta_frame(meta_frame.session_id, meta_frame.total, packer.meta_payload(meta)))
        cases.append(c)
    (OUT / "frames.json").write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")

    # QR 画像（text_zlib を 1 モジュール 4px で）
    from PIL import Image

    qr_raw = random_bytes.hex().encode("ascii")[:2500]  # 圧縮が効かない内容で、QR を複数枚にする
    plan = plan_for(packer.Packed("メモ.txt", "file", qr_raw, None), "none", 300, SESSION)
    frames = [plan.meta_frame()] + [plan.data_frame(i) for i in range(plan.total)]
    version = qrgen.choose_version(max(map(len, frames)), 0, "m")
    cache = qrgen.generate_cache(frames, version, "m", workers=1)
    for old in OUT.glob("qr_*.png"):
        old.unlink()
    for i in range(len(frames)):
        Image.fromarray(cache.gray(i, scale=4)).save(OUT / f"qr_{i:02d}.png")
    (OUT / "qr.json").write_text(json.dumps({
        "count": len(frames), "rawSha256": hashlib.sha256(qr_raw).hexdigest(), "name": "メモ.txt",
    }, ensure_ascii=False), encoding="utf-8")

    # 撮影を模した、少しボケた画像（1 フレーム分）
    import cv2

    g = cache.gray(1, scale=3)
    canvas = np.full((g.shape[0] + 80, g.shape[1] + 80), 255, np.uint8)
    canvas[40:40 + g.shape[0], 40:40 + g.shape[1]] = g
    blurred = cv2.GaussianBlur(canvas, (0, 0), float(os.environ.get("QRT_BLUR", "1.7")))
    shot = np.clip(blurred.astype(np.float32) * 0.6 + 50, 0, 255).astype(np.uint8)
    rgba = np.dstack([shot, shot, shot, np.full_like(shot, 255)])
    (OUT / "blur.bin").write_bytes(rgba.tobytes())
    (OUT / "blur.json").write_text(json.dumps({
        "width": int(shot.shape[1]), "height": int(shot.shape[0]), "frame": b64(frames[1]),
    }), encoding="utf-8")
    print(f"wrote {len(cases)} cases, {len(frames)} QR images to {OUT}")


if __name__ == "__main__":
    main()
