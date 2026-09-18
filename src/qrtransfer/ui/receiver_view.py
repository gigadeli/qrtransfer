"""受信モードの画面。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy, QSlider, QSplitter, QVBoxLayout, QWidget)

from .. import assembler as asm_mod
from .. import protocol
from ..calibration import CalibrationResult, exposure_label
from ..camera import FOCUS_MAX, FOCUS_MIN, STRATEGIES, STRATEGY_AUTO, CameraMode, CameraThread, probe_cameras
from ..decoder import DecodeThread, Detection
from ..settings import RESOLUTIONS, Settings
from .chunk_map import ChunkMapWidget
from .sender_view import human_size, human_time

MISSING_DISPLAY_ITEMS = 200
DETECTION_TTL_SEC = 0.4


class PreviewWidget(QWidget):
    """カメラ画像と検出枠（正常=緑、不正=赤）を表示する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image: QImage | None = None
        self.detections: list[Detection] = []
        self.det_size = (1, 1)
        self.det_time = 0.0
        self.message = "カメラ停止中"
        self.overlay = ""  # 画像の上部に重ねて表示する文（カメラの自動調整中の案内）
        self.overlay_progress = -1.0
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_overlay(self, text: str, progress: float = -1.0) -> None:
        self.overlay = text
        self.overlay_progress = progress
        self.update()

    def _draw_overlay(self, p: QPainter) -> None:
        if not self.overlay:
            return
        font = p.font()
        font.setPointSizeF(max(font.pointSizeF(), 11))
        p.setFont(font)
        inner = QRectF(18, 14, self.width() - 36, 1000)
        bound = p.boundingRect(inner, Qt.TextFlag.TextWordWrap, self.overlay)
        box = QRectF(8, 8, self.width() - 16, bound.height() + 26)
        p.fillRect(box, QColor(0, 0, 0, 170))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(18, 14, self.width() - 36, bound.height() + 4), Qt.TextFlag.TextWordWrap, self.overlay)
        if self.overlay_progress >= 0:
            bar = QRectF(box.left() + 10, box.bottom() - 9, box.width() - 20, 4)
            p.fillRect(bar, QColor(90, 90, 90))
            p.fillRect(QRectF(bar.left(), bar.top(), bar.width() * self.overlay_progress, bar.height()),
                       QColor(60, 170, 255))

    def set_image(self, img: QImage) -> None:
        self.image = img
        self.update()

    def set_detections(self, dets: list[Detection], w: int, h: int) -> None:
        if dets or time.monotonic() - self.det_time > DETECTION_TTL_SEC:
            self.detections = dets
            self.det_size = (w, h)
            self.det_time = time.monotonic()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if self.image is None:
            p.setPen(QColor(220, 220, 220))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            self._draw_overlay(p)
            return
        iw, ih = self.image.width(), self.image.height()
        s = min(self.width() / iw, self.height() / ih)
        tw, th = iw * s, ih * s
        ox, oy = (self.width() - tw) / 2, (self.height() - th) / 2
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawImage(QRectF(ox, oy, tw, th), self.image)
        self._draw_overlay(p)
        if time.monotonic() - self.det_time > DETECTION_TTL_SEC:
            return
        dw, dh = self.det_size
        sx, sy = tw / max(dw, 1), th / max(dh, 1)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for d in self.detections:
            pen = QPen(QColor(40, 220, 60) if d.ok else QColor(230, 40, 40), 3)
            p.setPen(pen)
            poly = QPolygonF([QPointF(ox + x * sx, oy + y * sy) for x, y in d.points])
            p.drawPolygon(poly)


class _Bridge(QObject):
    """Assembler のコールバック（デコードスレッド上）を UI スレッドのシグナルに変換する。"""
    meta = Signal(object)
    conflict = Signal(object)  # session_id は 32bit 符号なしなので int シグナルだと溢れる
    complete = Signal(object)


class ReceiverView(QWidget):
    back_requested = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.bridge = _Bridge(self)
        callbacks = asm_mod.AssemblerCallbacks(
            on_meta=self.bridge.meta.emit, on_conflict=self.bridge.conflict.emit,
            on_complete=self.bridge.complete.emit)
        self.assembler = asm_mod.Assembler(settings.output_dir, auto_extract_zip=settings.auto_extract_zip,
                                           callbacks=callbacks)
        self.bridge.conflict.connect(self._on_conflict)
        self.bridge.complete.connect(self._on_complete)
        self.bridge.meta.connect(lambda _m: self.refresh())
        self.camera: CameraThread | None = None
        self.decoder: DecodeThread | None = None
        self._missing_full = ""
        self._missing_key = None
        self.last_result: asm_mod.CompletionResult | None = None

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        back = QPushButton("← 戻る")
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        header.addWidget(QLabel("<b>受信モード</b>"))
        header.addStretch(1)
        root.addLayout(header)

        # カメラ設定
        cam_box = QGroupBox("カメラ")
        cam_rows = QVBoxLayout(cam_box)
        cl = QHBoxLayout()
        cam_rows.addLayout(cl)
        self.combo_cam = QComboBox()
        self.combo_cam.setMinimumWidth(160)
        self.combo_cam.addItem(f"カメラ {settings.camera_index}", settings.camera_index)
        self.btn_probe = QPushButton("カメラを検索")
        self.btn_probe.clicked.connect(self.probe)
        self.combo_res = QComboBox()
        for w, h in RESOLUTIONS:
            self.combo_res.addItem(f"{w}×{h}", (w, h))
        self.combo_res.setCurrentIndex(RESOLUTIONS.index(settings.resolution))
        self.chk_af = QCheckBox("オートフォーカス")
        self.chk_af.setChecked(settings.autofocus)
        self.btn_camera = QPushButton("受信開始")
        self.btn_camera.setMinimumWidth(110)
        self.btn_camera.clicked.connect(self.toggle_camera)
        self.lbl_cam = QLabel("")
        for w in (self.combo_cam, self.btn_probe, QLabel("解像度"), self.combo_res, self.chk_af, self.btn_camera):
            cl.addWidget(w)
        cl.addWidget(self.lbl_cam, 1)

        # 2 行目: 読み取り精度（ボケ対策）
        cl2 = QHBoxLayout()
        cam_rows.addLayout(cl2)
        self.combo_strategy = QComboBox()
        self.combo_strategy.addItem("自動（実測して最速を選ぶ）", STRATEGY_AUTO)
        for key, label in STRATEGIES:
            self.combo_strategy.addItem(label, key)
        self.combo_strategy.setToolTip(
            "カメラとの接続方式。カメラによって出せる fps が変わります。\n"
            "「自動」は受信開始時にいくつかの方式を試して fps を実測し、最も速いものを使います\n"
            "（初回は数秒かかります。選ばれた方式は保存され、次回からは速く開きます）。")
        i = self.combo_strategy.findData(settings.camera_strategy)
        self.combo_strategy.setCurrentIndex(max(0, i))
        self.combo_strategy.currentIndexChanged.connect(self._apply_strategy)
        cl2.addWidget(QLabel("接続方式"))
        cl2.addWidget(self.combo_strategy)
        self.chk_enhance = QCheckBox("画像補正（ボケ・低コントラスト対策）")
        self.chk_enhance.setToolTip("読み取れないときに、シャープ化・拡大・コントラスト強調をかけて再試行します。\n"
                                    "効いた補正を学習して、次からはそれを先に試します。")
        self.chk_enhance.setChecked(settings.enhance)
        self.chk_enhance.toggled.connect(self._apply_enhance)
        self.lbl_focus = QLabel("フォーカス")
        self.slider_focus = QSlider(Qt.Orientation.Horizontal)
        self.slider_focus.setRange(FOCUS_MIN, FOCUS_MAX)
        self.slider_focus.setSingleStep(5)
        self.slider_focus.setPageStep(25)
        self.slider_focus.setMinimumWidth(160)
        self.slider_focus.setToolTip("オートフォーカスを OFF にすると、手動でピントを合わせられます。\n"
                                     "右の「ピント」の値が大きくなる位置に合わせてください。\n"
                                     "（カメラによっては対応していません）")
        self.slider_focus.setValue(max(FOCUS_MIN, min(FOCUS_MAX, settings.manual_focus))
                                   if settings.manual_focus >= 0 else 0)
        self.slider_focus.valueChanged.connect(self._apply_focus)
        self.lbl_focus_value = QLabel("")
        self.lbl_focus_value.setMinimumWidth(36)
        self.lbl_sharp = QLabel("ピント: -")
        self.lbl_sharp.setToolTip("QR 付近の輪郭のくっきり度合い（目安）。大きいほどピントが合っています。")
        self.chk_af.toggled.connect(self._apply_autofocus)
        for w in (self.chk_enhance, self.lbl_focus, self.slider_focus, self.lbl_focus_value, self.lbl_sharp):
            cl2.addWidget(w)
        cl2.addStretch(1)
        # 3 行目: ピント・露出の自動調整（受信を始める前に 1 回だけ行い、値を固定する）
        cl3 = QHBoxLayout()
        cam_rows.addLayout(cl3)
        self.btn_calib = QPushButton("ピント・露出を自動調整")
        self.btn_calib.setToolTip(
            "送信側の待機画面（最初の QR）をカメラに映した状態で押してください。\n"
            "フォーカスと露光時間を順に振って、最も読み取りやすい値に固定します（10〜20 秒ほど）。\n"
            "受信中にピントや明るさが勝手に変わって読み取りが途切れるのを防ぎます。")
        self.btn_calib.setEnabled(False)
        self.btn_calib.clicked.connect(self.toggle_calibration)
        self.chk_calib_start = QCheckBox("受信開始時に自動調整する")
        self.chk_calib_start.setToolTip("受信開始後、QR がカメラに映ったら自動で調整を始めます。")
        self.chk_calib_start.setChecked(settings.calibrate_on_start)
        self.chk_calib_start.toggled.connect(self._apply_calibrate_on_start)
        self.btn_calib_reset = QPushButton("自動に戻す")
        self.btn_calib_reset.setToolTip("固定したピント・露出をやめて、オートフォーカス・自動露出に戻します。")
        self.btn_calib_reset.clicked.connect(self.reset_calibration)
        self.lbl_exposure = QLabel("")
        self.lbl_calib = QLabel("")
        self.lbl_calib.setWordWrap(True)
        for w in (self.btn_calib, self.chk_calib_start, self.btn_calib_reset, self.lbl_exposure):
            cl3.addWidget(w)
        cl3.addWidget(self.lbl_calib, 1)
        self._update_exposure_label()
        self._update_focus_enabled()
        root.addWidget(cam_box)

        # 読み取りがうまくいっていないときの助言
        self.hint_bar = QLabel("")
        self.hint_bar.setObjectName("hintBar")
        self.hint_bar.setWordWrap(True)
        self.hint_bar.setStyleSheet("#hintBar{border:2px solid #d9a400;border-radius:4px;padding:6px}")
        self.hint_bar.hide()
        root.addWidget(self.hint_bar)

        # 保存先
        out_box = QHBoxLayout()
        out_box.addWidget(QLabel("保存先"))
        self.edit_out = QLineEdit(settings.output_dir)
        self.edit_out.editingFinished.connect(self._apply_output_dir)
        btn_out = QPushButton("参照…")
        btn_out.clicked.connect(self.choose_output_dir)
        self.chk_zip = QCheckBox("ZIP を自動で展開する")
        self.chk_zip.setChecked(settings.auto_extract_zip)
        self.chk_zip.toggled.connect(self._apply_zip)
        out_box.addWidget(self.edit_out, 1)
        out_box.addWidget(btn_out)
        out_box.addWidget(self.chk_zip)
        root.addLayout(out_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.preview = PreviewWidget()
        splitter.addWidget(self.preview)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        # 別の転送を検出
        self.conflict_bar = QFrame()
        self.conflict_bar.setObjectName("conflictBar")
        self.conflict_bar.setStyleSheet("#conflictBar{border:2px solid #d9a400;border-radius:4px}")
        cbl = QHBoxLayout(self.conflict_bar)
        self.lbl_conflict = QLabel("別の転送を検出")
        self.lbl_conflict.setWordWrap(True)
        btn_switch = QPushButton("切り替え")
        btn_switch.clicked.connect(self.switch_session)
        cbl.addWidget(self.lbl_conflict, 1)
        cbl.addWidget(btn_switch)
        self.conflict_bar.hide()
        rl.addWidget(self.conflict_bar)

        # 完了表示
        self.done_bar = QFrame()
        self.done_bar.setObjectName("doneBar")
        dbl = QVBoxLayout(self.done_bar)
        self.lbl_done = QLabel("")
        self.lbl_done.setWordWrap(True)
        self.lbl_done.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dbtns = QHBoxLayout()
        self.btn_open = QPushButton("フォルダを開く")
        self.btn_open.clicked.connect(self.open_folder)
        self.btn_next = QPushButton("次の受信へ")
        self.btn_next.clicked.connect(self.next_transfer)
        dbtns.addWidget(self.btn_open)
        dbtns.addWidget(self.btn_next)
        dbtns.addStretch(1)
        dbl.addWidget(self.lbl_done)
        dbl.addLayout(dbtns)
        self.done_bar.hide()
        rl.addWidget(self.done_bar)

        info = QGroupBox("進捗")
        grid = QGridLayout(info)
        self.lbl_name = QLabel("メタ情報待ち")
        self.lbl_name.setWordWrap(True)
        self.lbl_session = QLabel("-")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("%p%")
        self.lbl_rate = QLabel("-")
        self.lbl_eta = QLabel("-")
        grid.addWidget(QLabel("ファイル"), 0, 0)
        grid.addWidget(self.lbl_name, 0, 1)
        grid.addWidget(QLabel("セッション"), 1, 0)
        grid.addWidget(self.lbl_session, 1, 1)
        grid.addWidget(QLabel("受信率"), 2, 0)
        grid.addWidget(self.progress, 2, 1)
        grid.addWidget(QLabel("受信速度"), 3, 0)
        grid.addWidget(self.lbl_rate, 3, 1)
        grid.addWidget(QLabel("残り時間"), 4, 0)
        grid.addWidget(self.lbl_eta, 4, 1)
        grid.setColumnStretch(1, 1)
        rl.addWidget(info)

        self.chunk_map = ChunkMapWidget()
        rl.addWidget(self.chunk_map, 1)

        miss_box = QGroupBox("欠落番号（送信側で R キーを押して入力すると再送モードになります）")
        ml = QVBoxLayout(miss_box)
        self.txt_missing = QPlainTextEdit()
        self.txt_missing.setReadOnly(True)
        self.txt_missing.setMaximumHeight(80)
        mbtns = QHBoxLayout()
        self.btn_copy = QPushButton("コピー")
        self.btn_copy.clicked.connect(self.copy_missing)
        self.btn_discard = QPushButton("この受信を破棄")
        self.btn_discard.clicked.connect(self.discard_session)
        mbtns.addWidget(self.btn_copy)
        mbtns.addStretch(1)
        mbtns.addWidget(self.btn_discard)
        ml.addWidget(self.txt_missing)
        ml.addLayout(mbtns)
        rl.addWidget(miss_box)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        self.status = QLabel("")
        root.addWidget(self.status)

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self._decode_fps = 0.0
        self._camera_fps = 0.0
        self._decode_stats: dict | None = None
        self._camera_warning = ""
        self._reading_hint = ""

    # ------------------------------------------------------------------ 設定
    def _apply_output_dir(self) -> None:
        d = self.edit_out.text().strip()
        if d:
            self.settings.output_dir = d
            self.assembler.output_dir = Path(d)
            self.settings.sync()

    def choose_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "保存先フォルダ", self.edit_out.text())
        if d:
            self.edit_out.setText(os.path.normpath(d))
            self._apply_output_dir()

    def _apply_zip(self, on: bool) -> None:
        self.settings.auto_extract_zip = on
        self.assembler.auto_extract_zip = on
        self.settings.sync()

    def _apply_strategy(self, _index: int) -> None:
        self.settings.camera_strategy = self.combo_strategy.currentData()
        self.settings.sync()
        if self.camera is not None:  # 受信中なら新しい方式で開き直す
            self.stop_camera()
            self.start_camera()

    def _on_mode_selected(self, mode: CameraMode) -> None:
        cam = self.camera
        if cam is not None and cam.strategy == STRATEGY_AUTO:
            self.settings.set_preferred_strategy(cam.index, cam.width, cam.height, mode.strategy)
            self.settings.sync()
        self._camera_warning = self.camera_fps_warning(mode)
        self._update_hint()

    @staticmethod
    def camera_fps_warning(mode: CameraMode) -> str:
        if mode.fps >= 20:
            return ""
        return (f"⚠ カメラの実測が {mode.fps:.0f} fps（{mode.backend}, {mode.width}×{mode.height}, {mode.fourcc}）と低めです。"
                "「接続方式」を変える・解像度を 1280×720 にする・部屋や画面を明るくする"
                "（暗いと露光時間が延びて fps が落ちるカメラがあります）を試してください。")

    def _update_hint(self) -> None:
        reading = "" if self.is_calibrating() else self._reading_hint  # 調整中はピントを振るので読めなくて当然
        parts = [t for t in (self._camera_warning, reading) if t]
        self.hint_bar.setText("\n".join(parts))
        self.hint_bar.setVisible(bool(parts))

    def _apply_enhance(self, on: bool) -> None:
        self.settings.enhance = on
        self.settings.sync()
        if self.decoder is not None:
            self.decoder.set_enhance(on)

    def _update_focus_enabled(self) -> None:
        manual = not self.chk_af.isChecked()
        busy = self.is_calibrating()
        self.slider_focus.setEnabled(manual and not busy)
        self.lbl_focus.setEnabled(manual)
        self.chk_af.setEnabled(not busy)
        self.lbl_focus_value.setText(str(self.slider_focus.value()) if manual else "自動")

    def _apply_autofocus(self, on: bool) -> None:
        self.settings.autofocus = on
        if not on:
            self.settings.manual_focus = self.slider_focus.value()
        self.settings.sync()
        self._update_focus_enabled()
        if self.camera is not None:
            self.camera.manual_focus = self.slider_focus.value()
            self.camera.set_autofocus(on)

    def _apply_focus(self, value: int) -> None:
        self._update_focus_enabled()
        if self.chk_af.isChecked():
            return
        self.settings.manual_focus = value
        self.settings.sync()
        if self.camera is not None:
            self.camera.set_focus(value)

    def _on_focus_value(self, value: int) -> None:
        """カメラを開いたときの現在のフォーカス値をスライダーに反映する（手動値が未設定の場合）。"""
        if value >= 0 and self.settings.manual_focus < 0:
            self.slider_focus.blockSignals(True)
            self.slider_focus.setValue(max(FOCUS_MIN, min(FOCUS_MAX, value)))
            self.slider_focus.blockSignals(False)
            self._update_focus_enabled()

    def _on_decode_stats(self, info: dict) -> None:
        self._decode_stats = info
        sharp = info.get("sharpness")
        self.lbl_sharp.setText(f"ピント: {sharp:.0f}" if sharp is not None else "ピント: -")
        self._reading_hint = self.reading_hint(info["window"])
        self._update_hint()

    @staticmethod
    def reading_hint(w) -> str:
        """直近の読み取り状況から、利用者への助言を作る（問題がなければ空文字）。"""
        seen = w.frames_ok + w.frames_detected_bad
        if w.frames < 10 or seen < 5:
            return ""
        bad_ratio = w.frames_detected_bad / seen
        if bad_ratio < 0.4:
            return ""
        return ("⚠ QR は見えていますが、読み取りに失敗することが多いです（直近 {:.0f}%）。次を試してください:\n"
                "・オートフォーカスを OFF にして、「フォーカス」を「ピント」の値が最大になる位置に合わせる\n"
                "・カメラを画面に近づける／解像度を 1920×1080 にする／画面の映り込みを避ける\n"
                "・送信側でチャンクサイズを小さくする（QR のセルが大きくなりボケに強くなる）か、"
                "ウィンドウ表示なら全画面にする").format(bad_ratio * 100)

    # ------------------------------------------------------------------ 自動調整
    def is_calibrating(self) -> bool:
        return getattr(self, "_calibrating", False)

    def _update_exposure_label(self) -> None:
        e = self.settings.exposure
        self.lbl_exposure.setText("露出: 自動" if e is None else f"露出: {exposure_label(e)}（固定）")

    def _apply_calibrate_on_start(self, on: bool) -> None:
        self.settings.calibrate_on_start = on
        self.settings.sync()

    def toggle_calibration(self) -> None:
        if self.camera is None:
            return
        if self.is_calibrating():
            self.camera.cancel_calibration()
            self.preview.set_overlay("調整を中止しています…")
        else:
            self._set_calibrating(True)
            self.camera.request_calibration()

    def _set_calibrating(self, on: bool) -> None:
        self._calibrating = on
        self.btn_calib.setText("調整を中止" if on else "ピント・露出を自動調整")
        self.btn_calib_reset.setEnabled(not on)
        self.combo_strategy.setEnabled(not on)
        self.combo_res.setEnabled(not on)
        self._update_focus_enabled()
        self._update_hint()
        if on:
            self.lbl_calib.setText("")
            self.preview.set_overlay("カメラの自動調整を準備しています…", 0.0)
        else:
            self.preview.set_overlay("")

    def _on_calibration_progress(self, text: str, frac: float) -> None:
        if not self.is_calibrating():
            self._set_calibrating(True)  # 受信開始時の自動調整
        self.preview.set_overlay(text + "\n（調整が終わるまで送信を始めないでください）", frac)

    def _on_calibration_done(self, result: CalibrationResult) -> None:
        self._set_calibrating(False)
        s = result.state
        if result.ok:
            self.settings.autofocus = s.autofocus
            if not s.autofocus and s.focus >= 0:
                self.settings.manual_focus = s.focus
            self.settings.exposure = s.exposure
            self.settings.sync()
            self.chk_af.blockSignals(True)
            self.chk_af.setChecked(s.autofocus)
            self.chk_af.blockSignals(False)
            if not s.autofocus and s.focus >= 0:
                self.slider_focus.blockSignals(True)
                self.slider_focus.setValue(max(FOCUS_MIN, min(FOCUS_MAX, s.focus)))
                self.slider_focus.blockSignals(False)
            if result.best_variant and self.decoder is not None:
                self.decoder.prefer_variant(result.best_variant)
            self.lbl_calib.setText("✔ 調整完了: " + result.message.replace("\n", "　"))
            self.status.setText("カメラの調整が終わりました。送信側で Space / Enter を押して送信を始めてください")
        else:
            self.lbl_calib.setText("⚠ " + result.message)
        self._update_exposure_label()
        self._update_focus_enabled()

    def reset_calibration(self) -> None:
        """固定したピント・露出をやめて、自動に戻す。"""
        self.settings.exposure = None
        self.settings.sync()
        self._update_exposure_label()
        if self.camera is not None:
            self.camera.set_exposure(None)
        self.lbl_calib.setText("")
        if self.chk_af.isChecked():
            self._apply_autofocus(True)
        else:
            self.chk_af.setChecked(True)  # _apply_autofocus で保存・カメラへの反映も行う

    def probe(self) -> None:
        was_running = self.camera is not None
        if was_running:
            self.stop_camera()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = probe_cameras(5)
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.combo_cam.clear()
        for idx, backend in found:
            self.combo_cam.addItem(f"カメラ {idx}（{backend}）", idx)
        if not found:
            self.combo_cam.addItem("カメラ 0", 0)
            QMessageBox.warning(self, "カメラ", "使えるカメラが見つかりませんでした。\n"
                                "Windows の設定 > プライバシーとセキュリティ > カメラ で、"
                                "デスクトップアプリのアクセスが許可されているか確認してください。")
        else:
            i = self.combo_cam.findData(self.settings.camera_index)
            self.combo_cam.setCurrentIndex(max(0, i))
        if was_running:
            self.start_camera()

    # ------------------------------------------------------------------ カメラ
    def toggle_camera(self) -> None:
        if self.camera is None:
            self.start_camera()
        else:
            self.stop_camera()

    def start_camera(self) -> None:
        self._apply_output_dir()
        idx = self.combo_cam.currentData()
        idx = 0 if idx is None else int(idx)
        w, h = self.combo_res.currentData()
        self.settings.camera_index = idx
        self.settings.resolution = (w, h)
        self.settings.autofocus = self.chk_af.isChecked()
        self.settings.sync()
        manual_focus = self.slider_focus.value() if self.settings.manual_focus >= 0 else -1
        strategy = self.combo_strategy.currentData()
        self.camera = CameraThread(idx, w, h, self.chk_af.isChecked(), manual_focus, strategy=strategy,
                                   preferred=self.settings.preferred_strategy(idx, w, h),
                                   exposure=self.settings.exposure,
                                   calibrate_on_start=self.chk_calib_start.isChecked(), parent=self)
        self.camera.mode_selected.connect(self._on_mode_selected)
        self.camera.calibration_progress.connect(self._on_calibration_progress)
        self.camera.calibration_done.connect(self._on_calibration_done)
        self.decoder = DecodeThread(self.camera.source, self.assembler, enhance=self.chk_enhance.isChecked(),
                                    parent=self)
        self.camera.preview.connect(self.preview.set_image)
        self.camera.opened.connect(self._on_camera_opened)
        self.camera.fps_measured.connect(lambda f: setattr(self, "_camera_fps", f))
        self.camera.focus_value.connect(self._on_focus_value)
        self.decoder.detections.connect(self.preview.set_detections)
        self.decoder.decode_fps.connect(lambda f: setattr(self, "_decode_fps", f))
        self.decoder.stats.connect(self._on_decode_stats)
        self.decoder.error.connect(lambda m: self.status.setText(f"⚠ 読み取り処理でエラー: {m}"))
        opening = ("カメラに接続しています（接続方式を調べて fps を実測するため、数秒かかることがあります）…"
                   if strategy == STRATEGY_AUTO else "カメラを開いています…")
        self.preview.message = opening
        self.preview.image = None
        self.preview.update()
        self.lbl_cam.setText("カメラに接続しています…")
        self.btn_camera.setText("受信停止")
        self.camera.start()
        self.decoder.start()

    def stop_camera(self) -> None:
        for t in (self.camera, self.decoder):
            if t is not None:
                t.stop()
        for t in (self.camera, self.decoder):
            if t is not None:
                t.wait(5000)
                t.deleteLater()
        self.camera = self.decoder = None
        self.btn_calib.setEnabled(False)
        if self.is_calibrating():
            self._set_calibrating(False)
        self.assembler.flush()
        self.btn_camera.setText("受信開始")
        self.lbl_cam.setText("")
        self.preview.image = None
        self.preview.message = "カメラ停止中"
        self._decode_stats = None
        self._camera_warning = self._reading_hint = ""
        self.hint_bar.hide()
        self.lbl_sharp.setText("ピント: -")
        self.preview.update()

    def _on_camera_opened(self, ok: bool, text: str) -> None:
        self.lbl_cam.setText(text)
        self.btn_calib.setEnabled(ok)
        if not ok:
            self.stop_camera()
            self.preview.message = text
            QMessageBox.warning(self, "カメラ", text + "\n\n別のカメラを選ぶか、「カメラを検索」を押してください。"
                                "Windows の設定でカメラへのアクセスが許可されているかも確認してください。")

    # ------------------------------------------------------------------ 表示更新
    def refresh(self) -> None:
        snap = self.assembler.snapshot()
        if snap.session_id is None:
            self.lbl_name.setText("転送待ち（QR コードをカメラに映してください）")
            self.lbl_session.setText("-")
            self.progress.setValue(0)
            self.lbl_rate.setText("-")
            self.lbl_eta.setText("-")
            self.chunk_map.set_state(0, b"")
            self._missing_key = None
            self._set_missing([])
        else:
            if snap.meta:
                m = snap.meta
                self.lbl_name.setText(f"{m['name']}　{human_size(m['raw_size'])}（{m['mode']}, {m['compression']}）")
            else:
                self.lbl_name.setText("メタ情報待ち")
            self.lbl_session.setText(f"{snap.session_id:08x}　{snap.received} / {snap.total} チャンク")
            frac = snap.received / snap.total if snap.total else (1.0 if snap.meta else 0.0)
            self.progress.setValue(int(frac * 1000))
            self.lbl_rate.setText(f"{snap.rate_chunks:.1f} チャンク/秒　{snap.rate_bytes / 1024:.1f} KB/秒")
            self.lbl_eta.setText(human_time(snap.eta_sec) if snap.eta_sec is not None else "-")
            self.chunk_map.set_state(snap.total, snap.bitmap)
            key = (snap.session_id, snap.received, snap.finished, bool(snap.meta))
            if key != self._missing_key:
                self._missing_key = key
                if not snap.finished and snap.total and snap.received < snap.total:
                    self._set_missing(self.assembler.missing())
                elif not snap.finished and snap.total == snap.received and not snap.meta:
                    self._set_missing(None, "（全チャンク受信済み。META を待っています）")
                else:
                    self._set_missing([])
            r = getattr(self, "last_result", None)
            if self.done_bar.isVisible() and r is not None and r.session_id != snap.session_id:
                self.done_bar.hide()  # 完了表示中に次の転送が始まった
        if snap.conflict_session is not None and not snap.finished:
            self.lbl_conflict.setText(f"⚠ 別の転送を検出しました（session {snap.conflict_session:08x}）。"
                                      "「切り替え」を押すと、そちらを受信します（今の受信は後で再開できます）")
            self.conflict_bar.show()
        else:
            self.conflict_bar.hide()
        if self.camera is not None:
            text = f"カメラ {self._camera_fps:.0f} fps　読み取り {self._decode_fps:.0f} 回/秒"
            if self._decode_stats is not None:
                total = self._decode_stats["total"]
                seen = total.frames_ok + total.frames_detected_bad
                if seen:
                    text += f"　読み取り成功 {total.frames_ok / seen * 100:.0f}%（QR が映っていた画像のうち）"
                if total.rescued:
                    text += f"　うち補正で読めた {total.rescued} 枚"
            self.status.setText(text)

    def _set_missing(self, values: list[int] | None, note: str = "") -> None:
        if values:
            full = protocol.format_ranges(values)
            display = protocol.format_ranges(values, max_items=MISSING_DISPLAY_ITEMS)
        else:
            full, display = "", note
        self._missing_full = full
        if self.txt_missing.toPlainText() != display:
            self.txt_missing.setPlainText(display)

    def copy_missing(self) -> None:
        if self._missing_full:
            QGuiApplication.clipboard().setText(self._missing_full)
            self.status.setText("欠落番号をコピーしました")

    # ------------------------------------------------------------------ セッション操作
    def _on_conflict(self, sid: int) -> None:
        self.refresh()

    def switch_session(self) -> None:
        if self.assembler.switch_to_conflict():
            self.done_bar.hide()
            self.refresh()

    def discard_session(self) -> None:
        if self.assembler.session_id is None:
            return
        if QMessageBox.question(self, "破棄", "受信中のデータを破棄しますか？（この転送のフレームは以後無視します）") \
                == QMessageBox.StandardButton.Yes:
            self.assembler.discard_current()
            self.done_bar.hide()
            self.refresh()

    def resume_session(self, session_id: int) -> bool:
        ok = self.assembler.resume(session_id)
        self.refresh()
        return ok

    def _on_complete(self, result: asm_mod.CompletionResult) -> None:
        self.refresh()
        self.last_result = result
        if result.ok:
            self.done_bar.setStyleSheet("#doneBar{border:2px solid #2ea043;border-radius:4px}")
            extra = ""
            if result.extract_report and result.extract_report.skipped:
                extra = "<br>スキップした項目: " + ", ".join(n for n, _ in result.extract_report.skipped[:10])
            self.lbl_done.setText(
                f"<b style='color:#2ea043'>受信完了（照合結果: OK）</b><br>保存先: {result.path}<br>"
                f"サイズ: {human_size(result.raw_size)}（転送 {human_size(result.payload_size)}）"
                f"　所要時間: {human_time(result.elapsed_sec)}<br>{result.message}{extra}")
            self.btn_open.setEnabled(True)
        else:
            self.done_bar.setStyleSheet("#doneBar{border:2px solid #d9534f;border-radius:4px}")
            self.lbl_done.setText(f"<b style='color:#d9534f'>受信失敗（照合結果: NG）</b><br>{result.name}<br>{result.message}<br>"
                                  "データは保存していません。送信をやり直してください。")
            self.btn_open.setEnabled(False)
        self.done_bar.show()
        QApplicationBeep()

    def open_folder(self) -> None:
        r = getattr(self, "last_result", None)
        if r is None or r.path is None:
            return
        target = r.path if r.path.is_dir() else r.path.parent
        if os.name == "nt" and r.path.is_file():
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(r.path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def next_transfer(self) -> None:
        self.assembler.reset()
        self.done_bar.hide()
        self.refresh()

    def shutdown(self) -> None:
        self.stop_camera()
        self.assembler.close()


def QApplicationBeep() -> None:
    from PySide6.QtWidgets import QApplication
    QApplication.beep()
