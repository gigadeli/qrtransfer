"""QSettings（IniFormat）のラッパー。保存先: %APPDATA%\\QRTransfer\\settings.ini"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from . import packer, qrgen, repair

RESOLUTIONS = [(1280, 720), (1920, 1080)]
FPS_MIN, FPS_MAX, FPS_DEFAULT = 1, 15, 6
REPAIR_RATIO_MIN = 10


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "QRTransfer" / "settings.ini"


def default_download_dir() -> str:
    d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return d or str(Path.home() / "Downloads")


class Settings:
    def __init__(self, path: str | os.PathLike | None = None):
        self._q = QSettings(str(path or settings_path()), QSettings.Format.IniFormat)

    def _int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(self._q.value(key, default))
        except (TypeError, ValueError):
            return default
        return v if lo <= v <= hi else default

    # カメラ
    @property
    def camera_index(self) -> int:
        return self._int("receiver/camera_index", 0, 0, 99)

    @camera_index.setter
    def camera_index(self, v: int) -> None:
        self._q.setValue("receiver/camera_index", int(v))

    @property
    def resolution(self) -> tuple[int, int]:
        text = str(self._q.value("receiver/resolution", "1280x720"))
        try:
            w, h = (int(x) for x in text.lower().split("x"))
        except ValueError:
            return RESOLUTIONS[0]
        return (w, h) if (w, h) in RESOLUTIONS else RESOLUTIONS[0]

    @resolution.setter
    def resolution(self, v: tuple[int, int]) -> None:
        self._q.setValue("receiver/resolution", f"{v[0]}x{v[1]}")

    @property
    def autofocus(self) -> bool:
        return str(self._q.value("receiver/autofocus", "true")).lower() in ("true", "1")

    @autofocus.setter
    def autofocus(self, v: bool) -> None:
        self._q.setValue("receiver/autofocus", bool(v))

    @property
    def camera_strategy(self) -> str:
        """カメラの接続方式（"auto" = 実測して最速のものを選ぶ）。"""
        from .camera import STRATEGY_AUTO, STRATEGY_KEYS
        v = str(self._q.value("receiver/camera_strategy", STRATEGY_AUTO))
        return v if v == STRATEGY_AUTO or v in STRATEGY_KEYS else STRATEGY_AUTO

    @camera_strategy.setter
    def camera_strategy(self, v: str) -> None:
        self._q.setValue("receiver/camera_strategy", v)

    def preferred_strategy(self, index: int, width: int, height: int) -> str | None:
        """前回「自動」で選ばれた接続方式（カメラと解像度ごと）。"""
        v = self._q.value(f"camera_modes/cam{index}_{width}x{height}", "")
        return str(v) if v else None

    def set_preferred_strategy(self, index: int, width: int, height: int, strategy: str) -> None:
        self._q.setValue(f"camera_modes/cam{index}_{width}x{height}", strategy)

    @property
    def manual_focus(self) -> int:
        """オートフォーカス OFF のときのフォーカス値（-1 = カメラの値のまま）。"""
        return self._int("receiver/manual_focus", -1, -1, 1023)

    @manual_focus.setter
    def manual_focus(self, v: int) -> None:
        self._q.setValue("receiver/manual_focus", int(v))

    @property
    def exposure(self) -> float | None:
        """固定した露出（2 の累乗秒の指数。None = 自動露出）。"""
        text = str(self._q.value("receiver/exposure", "") or "")
        try:
            v = float(text)
        except ValueError:
            return None
        return v if -16 <= v <= 4 else None

    @exposure.setter
    def exposure(self, v: float | None) -> None:
        self._q.setValue("receiver/exposure", "" if v is None else f"{float(v):g}")

    @property
    def calibrate_on_start(self) -> bool:
        """受信開始時に、QR が映ったらピント・露出を自動調整するか。"""
        return str(self._q.value("receiver/calibrate_on_start", "true")).lower() in ("true", "1")

    @calibrate_on_start.setter
    def calibrate_on_start(self, v: bool) -> None:
        self._q.setValue("receiver/calibrate_on_start", bool(v))

    @property
    def enhance(self) -> bool:
        """読み取れないときに画像補正（シャープ化など）をかけて再試行するか。"""
        return str(self._q.value("receiver/enhance", "true")).lower() in ("true", "1")

    @enhance.setter
    def enhance(self, v: bool) -> None:
        self._q.setValue("receiver/enhance", bool(v))

    # 送信
    @property
    def fps(self) -> int:
        return self._int("sender/fps", FPS_DEFAULT, FPS_MIN, FPS_MAX)

    @fps.setter
    def fps(self, v: int) -> None:
        self._q.setValue("sender/fps", int(v))

    @property
    def chunk_size(self) -> int:
        return self._int("sender/chunk_size", packer.CHUNK_SIZE_DEFAULT, packer.CHUNK_SIZE_MIN, packer.CHUNK_SIZE_MAX)

    @chunk_size.setter
    def chunk_size(self, v: int) -> None:
        self._q.setValue("sender/chunk_size", int(v))

    @property
    def use_repair(self) -> bool:
        """修復用 QR を混ぜて送るか（False なら従来方式: 欠落番号を入力して再送）。"""
        return str(self._q.value("sender/use_repair", "true")).lower() in ("true", "1")

    @use_repair.setter
    def use_repair(self, v: bool) -> None:
        self._q.setValue("sender/use_repair", bool(v))

    @property
    def repair_ratio(self) -> int:
        """修復用 QR の枚数（全チャンク数に対する %）。"""
        return self._int("sender/repair_ratio", repair.RATIO_DEFAULT, REPAIR_RATIO_MIN, repair.RATIO_MAX)

    @repair_ratio.setter
    def repair_ratio(self, v: int) -> None:
        self._q.setValue("sender/repair_ratio", int(v))

    @property
    def qr_fullscreen(self) -> bool:
        """QR 表示を全画面にするか（False ならウィンドウモード）。"""
        return str(self._q.value("sender/qr_fullscreen", "true")).lower() in ("true", "1")

    @qr_fullscreen.setter
    def qr_fullscreen(self, v: bool) -> None:
        self._q.setValue("sender/qr_fullscreen", bool(v))

    @property
    def qr_wait_for_start(self) -> bool:
        """QR 表示を待機状態（最初の QR を静止表示）で開き、Space / Enter で送信を始めるか。"""
        return str(self._q.value("sender/qr_wait_for_start", "true")).lower() in ("true", "1")

    @qr_wait_for_start.setter
    def qr_wait_for_start(self, v: bool) -> None:
        self._q.setValue("sender/qr_wait_for_start", bool(v))

    @property
    def qr_always_on_top(self) -> bool:
        return str(self._q.value("sender/qr_always_on_top", "false")).lower() in ("true", "1")

    @qr_always_on_top.setter
    def qr_always_on_top(self, v: bool) -> None:
        self._q.setValue("sender/qr_always_on_top", bool(v))

    @property
    def qr_window_geometry(self) -> bytes:
        """ウィンドウモードの位置と大きさ（QWidget.saveGeometry の内容を Base64 で保存）。"""
        text = str(self._q.value("sender/qr_window_geometry", "") or "")
        try:
            return base64.b64decode(text.encode("ascii"), validate=True) if text else b""
        except (ValueError, UnicodeEncodeError):
            return b""

    @qr_window_geometry.setter
    def qr_window_geometry(self, v: bytes) -> None:
        self._q.setValue("sender/qr_window_geometry", base64.b64encode(bytes(v)).decode("ascii") if v else "")

    @property
    def ecc(self) -> str:
        v = str(self._q.value("sender/ecc", qrgen.ECC_DEFAULT)).lower()
        return v if v in qrgen.ECC_LEVELS else qrgen.ECC_DEFAULT

    @ecc.setter
    def ecc(self, v: str) -> None:
        self._q.setValue("sender/ecc", v.lower())

    # 受信
    @property
    def output_dir(self) -> str:
        v = str(self._q.value("receiver/output_dir", "") or "")
        return v or default_download_dir()

    @output_dir.setter
    def output_dir(self, v: str) -> None:
        self._q.setValue("receiver/output_dir", str(v))

    @property
    def auto_extract_zip(self) -> bool:
        return str(self._q.value("receiver/auto_extract_zip", "false")).lower() in ("true", "1")

    @auto_extract_zip.setter
    def auto_extract_zip(self, v: bool) -> None:
        self._q.setValue("receiver/auto_extract_zip", bool(v))

    def sync(self) -> None:
        self._q.sync()
