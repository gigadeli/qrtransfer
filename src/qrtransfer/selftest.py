"""exe 単体での動作確認（`QRTransfer.exe --selftest`）。

QR の生成 → 画像化 → zxing-cpp による読み取り → 組立 → SHA-256 照合までを、カメラなしで実行する。
結果は %LOCALAPPDATA%\\QRTransfer\\selftest.log にも書き出す。終了コード 0 = 成功。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "QRTransfer" / "selftest.log"


def run_selftest() -> tuple[bool, str]:
    lines: list[str] = []
    ok = False
    t0 = time.monotonic()
    try:
        import cv2
        import numpy as np
        import zxingcpp

        from . import __version__, assembler, decoder, packer, qrgen

        lines.append(f"QRTransfer {__version__} / Python {sys.version.split()[0]}")
        lines.append(f"zxing-cpp: {getattr(zxingcpp, '__version__', '?')}, OpenCV: {cv2.__version__}")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "src"
            src.mkdir()
            content = bytes(range(256)) * 20 + os.urandom(4000)
            (src / "テスト.bin").write_bytes(content)
            plan = packer.make_plan([src / "テスト.bin"], chunk_size=800)
            v = qrgen.choose_version(plan.max_data_frame_size, plan.min_meta_frame_size(), "m")
            cap = qrgen.capacity(v, "m")
            frames = [plan.meta_frame(cap)] + [plan.data_frame(i) for i in range(plan.total)]
            cache = qrgen.generate_cache(frames, v, "m", workers=2)
            lines.append(f"frames={len(frames)} version={v} generated")
            results: list = []
            asm = assembler.Assembler(tmp / "out", tmp / "sessions",
                                      callbacks=assembler.AssemblerCallbacks(on_complete=results.append))
            for i in range(len(frames)):
                gray = cache.gray(i, scale=4)
                # カメラ画像を模して BGR・余白付きにする
                canvas = np.full((gray.shape[0] + 80, gray.shape[1] + 120), 255, np.uint8)
                canvas[40:40 + gray.shape[0], 60:60 + gray.shape[1]] = gray
                bgr = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
                dets, _ = decoder.process_image(bgr, asm)
                if not dets or not all(d.ok for d in dets):
                    raise RuntimeError(f"frame {i} could not be decoded")
            if not results or not results[0].ok:
                raise RuntimeError("assembly failed: " + (results[0].message if results else "not completed"))
            if results[0].path.read_bytes() != content:
                raise RuntimeError("content mismatch")
            lines.append("decode + assemble + SHA-256: OK")

            # ボケた画像を補正付きで読めること（補正に使う OpenCV の関数が exe に含まれているかの確認も兼ねる）
            # （1 セル 5px に標準偏差 3px のボケ。補正なしでは読めず、強いシャープ化で読める条件）
            blurred = cv2.GaussianBlur(cache.gray(1, scale=5), (0, 0), 3.0)
            blurred = np.pad(blurred, 60, constant_values=255)
            robust = decoder.RobustDecoder()
            if frames[1] in [d for _, d in decoder.decode_image(blurred)]:
                raise RuntimeError("blurred test image was readable without enhancement (test is too easy)")
            if frames[1] not in [d for _, d in robust.decode(blurred)]:
                raise RuntimeError("enhanced decoding of a blurred image failed")
            lines.append(f"enhanced decoding (blurred image): OK via {robust.last_variant}")

            # 並列生成（プロセスプール）が exe でも動くこと
            frames2 = [plan.data_frame(0)] * 70
            par = qrgen.generate_cache(frames2, v, "m", workers=2)
            if not par.parallel:
                raise RuntimeError("parallel QR generation (process pool) did not work")
            if par.packed[0] != cache.packed[1]:
                raise RuntimeError("parallel QR generation produced different output")
            lines.append("parallel QR generation: OK")
        ok = True
    except Exception:  # noqa: BLE001
        lines.append(traceback.format_exc())
    lines.append(f"{'SELFTEST OK' if ok else 'SELFTEST FAILED'} ({time.monotonic() - t0:.1f}s)")
    text = "\n".join(lines)
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        text += f"\nlog: {p}"
    except OSError:
        pass
    return ok, text
