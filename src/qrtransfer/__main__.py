"""エントリポイント（python -m qrtransfer / QRTransfer.exe）。

オプション:
    --selftest   カメラなしで QR 生成 → 読み取り → 組立 → SHA-256 照合を行い、結果を表示する
                 （結果は %LOCALAPPDATA%\\QRTransfer\\selftest.log にも保存。終了コード 0 = 成功）
    --quiet      --selftest と併用。結果のダイアログを出さない
"""

import multiprocessing
import os
import sys

# MSMF（Media Foundation）でカメラを開くのに数十秒かかる既知の問題を避ける（cv2 の読み込み前に設定する）
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")


def main() -> int:
    multiprocessing.freeze_support()  # exe でプロセスプール（QR 並列生成）を使うため、最初に呼ぶ
    if "--selftest" in sys.argv:
        from qrtransfer.selftest import run_selftest

        ok, text = run_selftest()
        if sys.stdout is not None:
            print(text)
        elif "--quiet" not in sys.argv:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox

                app = QApplication(sys.argv[:1])  # noqa: F841
                (QMessageBox.information if ok else QMessageBox.critical)(None, "QRTransfer セルフテスト", text)
            except Exception:  # noqa: BLE001
                pass
        return 0 if ok else 1

    from qrtransfer.ui.main_window import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
