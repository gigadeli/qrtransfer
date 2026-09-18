"""送信モードの画面。"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QListWidget, QMessageBox, QProgressDialog, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from .. import packer, protocol, qrgen
from ..settings import FPS_MAX, FPS_MIN, Settings

WARN_SIZE = 10 * 1024 * 1024


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 時間 {sec % 3600 // 60} 分"


class PrepareWorker(QThread):
    """梱包 → 圧縮 → フレーム作成 → QR 生成（バックグラウンド）。"""
    progress = Signal(str, int, int)
    finished_ok = Signal(object, object, object)  # plan, cache, frame_seqs
    failed = Signal(str)

    def __init__(self, paths: list[str], chunk_size: int, ecc: str, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.chunk_size = chunk_size
        self.ecc = ecc
        self.cancel = threading.Event()

    def run(self) -> None:
        try:
            self.progress.emit("梱包しています…", 0, 0)
            packed = packer.pack_paths(self.paths)
            if self.cancel.is_set():
                return
            self.progress.emit("圧縮方式を判定しています…", 0, 0)
            plan = packer.make_plan_from_packed(packed, self.chunk_size)
            if self.cancel.is_set():
                return
            version = qrgen.choose_version(plan.max_data_frame_size, plan.min_meta_frame_size(), self.ecc)
            cap = qrgen.capacity(version, self.ecc)
            frames = [plan.meta_frame(cap)] + [plan.data_frame(i) for i in range(plan.total)]
            frame_seqs = [packer.CAROUSEL_META] + list(range(plan.total))
            label = f"QR コードを生成しています（バージョン {version}、{len(frames)} 枚）…"
            cache = qrgen.generate_cache(frames, version, self.ecc,
                                         progress=lambda d, t: self.progress.emit(label, d, t),
                                         cancel=self.cancel)
            if cache is None or self.cancel.is_set():
                return
            self.finished_ok.emit(plan, cache, frame_seqs)
        except (packer.PackError, qrgen.QRGenError, ValueError, OSError) as e:
            self.failed.emit(str(e))
        except MemoryError:
            self.failed.emit("メモリが不足しました。ファイルが大きすぎます。")


class SenderView(QWidget):
    back_requested = Signal()
    start_fullscreen = Signal(object, object, object, int)  # plan, cache, frame_seqs, fps

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.paths: list[str] = []
        self.input_size = 0
        self.input_files = 0
        self.worker: PrepareWorker | None = None
        self.progress_dialog: QProgressDialog | None = None
        self._warned_size = -1
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        back = QPushButton("← 戻る")
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        title = QLabel("<b>送信モード</b>")
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        # ファイル選択
        box = QGroupBox("送るファイル（ここにドラッグ＆ドロップもできます）")
        bl = QVBoxLayout(box)
        buttons = QHBoxLayout()
        self.btn_files = QPushButton("ファイルを選択（複数可）")
        self.btn_folder = QPushButton("フォルダを選択")
        self.btn_clear = QPushButton("クリア")
        self.btn_files.clicked.connect(self.choose_files)
        self.btn_folder.clicked.connect(self.choose_folder)
        self.btn_clear.clicked.connect(lambda: self.set_paths([]))
        for b in (self.btn_files, self.btn_folder, self.btn_clear):
            buttons.addWidget(b)
        buttons.addStretch(1)
        bl.addLayout(buttons)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        bl.addWidget(self.list)
        self.lbl_total = QLabel("未選択")
        bl.addWidget(self.lbl_total)
        root.addWidget(box, 1)

        # パラメータ
        pbox = QGroupBox("パラメータ")
        form = QFormLayout(pbox)
        self.spin_chunk = QSpinBox()
        self.spin_chunk.setRange(packer.CHUNK_SIZE_MIN, packer.CHUNK_SIZE_MAX)
        self.spin_chunk.setSingleStep(50)
        self.spin_chunk.setSuffix(" バイト")
        self.spin_chunk.setValue(settings.chunk_size)
        self.combo_ecc = QComboBox()
        for e, desc in (("l", "L（約 7% 復元）"), ("m", "M（約 15% 復元）"), ("q", "Q（約 25% 復元）"),
                        ("h", "H（約 30% 復元）")):
            self.combo_ecc.addItem(desc, e)
        self.combo_ecc.setCurrentIndex(qrgen.ECC_LEVELS.index(settings.ecc))
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(FPS_MIN, FPS_MAX)
        self.spin_fps.setValue(settings.fps)
        self.spin_fps.setSuffix(" fps")
        self.combo_display = QComboBox()
        self.combo_display.addItem("全画面（読み取りやすい）", True)
        self.combo_display.addItem("ウィンドウ（他の操作をしながら送れる）", False)
        self.combo_display.setCurrentIndex(0 if settings.qr_fullscreen else 1)
        self.chk_on_top = QCheckBox("常に手前に表示する（ウィンドウモード向け）")
        self.chk_on_top.setChecked(settings.qr_always_on_top)
        form.addRow("チャンクサイズ", self.spin_chunk)
        form.addRow("誤り訂正レベル (ECC)", self.combo_ecc)
        form.addRow("表示速度", self.spin_fps)
        form.addRow("QR の表示方法", self.combo_display)
        form.addRow("", self.chk_on_top)
        self.lbl_estimate = QLabel("")
        self.lbl_estimate.setWordWrap(True)
        form.addRow("見積もり", self.lbl_estimate)
        root.addWidget(pbox)

        self.btn_start = QPushButton("送信開始")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(self.start)
        root.addWidget(self.btn_start)

        self.spin_chunk.valueChanged.connect(self.update_estimate)
        self.combo_ecc.currentIndexChanged.connect(self.update_estimate)
        self.spin_fps.valueChanged.connect(self.update_estimate)
        self.update_estimate()

    # ------------------------------------------------------------------ 入力
    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "送るファイルを選択")
        if files:
            self.set_paths(files)

    def choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "送るフォルダを選択")
        if d:
            self.set_paths([d])

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.set_paths(paths)
            event.acceptProposedAction()

    def set_paths(self, paths: list[str]) -> None:
        self.paths = [os.path.normpath(p) for p in paths]
        self.list.clear()
        for p in self.paths:
            kind = "📁 " if os.path.isdir(p) else "📄 "
            self.list.addItem(kind + p)
        self.input_size, self.input_files = packer.total_input_size(self.paths)
        if self.paths:
            mode = "file（単一ファイル）" if len(self.paths) == 1 and os.path.isfile(self.paths[0]) else "bundle（tar にまとめる）"
            self.lbl_total.setText(f"{self.input_files} ファイル、合計 {human_size(self.input_size)}　形式: {mode}")
        else:
            self.lbl_total.setText("未選択")
        self.update_estimate()
        if self.input_size > WARN_SIZE and self._warned_size != self.input_size:
            self._warned_size = self.input_size
            QMessageBox.warning(
                self, "ファイルが大きいです",
                f"合計 {human_size(self.input_size)} です。QR による転送は数 MB までが実用範囲です。\n\n"
                f"{self.estimate_text()}\n\n（圧縮が効く場合は短くなります）")

    # ------------------------------------------------------------------ 見積もり
    def estimate_text(self) -> str:
        chunk = self.spin_chunk.value()
        ecc = self.combo_ecc.currentData()
        fps = self.spin_fps.value()
        size = self.input_size
        total = packer.total_chunks(size, chunk)
        frames = packer.estimate_frames_per_cycle(total)
        max_frame = protocol.OVERHEAD + min(chunk, size) if size else protocol.OVERHEAD
        try:
            # META は JSON（約 300 バイト＋ファイル名）なので、その分も考慮する
            version = qrgen.choose_version(max_frame, protocol.OVERHEAD + 330, ecc)
            vtext = f"QR バージョン {version}（{qrgen.modules_for_version(version, 0)}×{qrgen.modules_for_version(version, 0)} モジュール）"
        except qrgen.QRGenError as e:
            return f"⚠ {e}"
        cycle = frames / fps
        rate = chunk * fps * total / max(frames, 1) if total else 0
        return (f"{total} チャンク、1 周 {frames} 枚 ≈ {human_time(cycle)}、{vtext}、"
                f"実効 約 {human_size(rate)}/秒（圧縮前・取りこぼしなしの場合）")

    def update_estimate(self) -> None:
        self.lbl_estimate.setText(self.estimate_text())

    # ------------------------------------------------------------------ 送信
    def save_params(self) -> None:
        self.settings.chunk_size = self.spin_chunk.value()
        self.settings.ecc = self.combo_ecc.currentData()
        self.settings.fps = self.spin_fps.value()
        self.settings.qr_fullscreen = bool(self.combo_display.currentData())
        self.settings.qr_always_on_top = self.chk_on_top.isChecked()
        self.settings.sync()

    def start(self) -> None:
        if not self.paths:
            QMessageBox.information(self, "送信", "送るファイルまたはフォルダを選択してください。")
            return
        missing = [p for p in self.paths if not os.path.exists(p)]
        if missing:
            QMessageBox.warning(self, "送信", "見つからないファイルがあります:\n" + "\n".join(missing))
            return
        self.save_params()
        self.btn_start.setEnabled(False)
        self.worker = PrepareWorker(self.paths, self.spin_chunk.value(), self.combo_ecc.currentData(), self)
        dlg = QProgressDialog("準備しています…", "キャンセル", 0, 0, self)
        dlg.setWindowTitle("送信の準備")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(self.worker.cancel.set)
        self.progress_dialog = dlg
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_ready)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_worker_done)
        dlg.show()
        self.worker.start()

    def _on_progress(self, text: str, done: int, total: int) -> None:
        if self.progress_dialog is None:
            return
        self.progress_dialog.setLabelText(text)
        self.progress_dialog.setMaximum(total)
        self.progress_dialog.setValue(done)

    def _on_ready(self, plan, cache, frame_seqs) -> None:
        self._close_progress()
        self.start_fullscreen.emit(plan, cache, frame_seqs, self.spin_fps.value())

    def _on_failed(self, message: str) -> None:
        self._close_progress()
        QMessageBox.critical(self, "送信の準備に失敗しました", message)

    def _on_worker_done(self) -> None:
        self._close_progress()
        self.btn_start.setEnabled(True)
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    def _close_progress(self) -> None:
        if self.progress_dialog is not None:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait(5000)
