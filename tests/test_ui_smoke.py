"""UI の結合テスト（offscreen）。送信側の全画面表示を画面キャプチャし、カメラ画像の代わりに受信側へ流す。"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

cv2 = pytest.importorskip("cv2")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from qrtransfer import packer  # noqa: E402
from qrtransfer.camera import FrameSource  # noqa: E402
from qrtransfer.decoder import DecodeThread, process_image  # noqa: E402

from conftest import read_tree, write_tree  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, app):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    from qrtransfer.settings import Settings
    s = Settings(tmp_path / "settings.ini")
    s.output_dir = str(tmp_path / "out")
    s.chunk_size = 300
    s.ecc = "m"
    return s


def qimage_to_bgr(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    arr = np.frombuffer(img.constBits(), np.uint8, count=img.bytesPerLine() * h).reshape(h, img.bytesPerLine())
    arr = arr[:, : w * 3].reshape(h, w, 3)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def camera_like(bgr: np.ndarray) -> np.ndarray:
    """画面を撮影したような劣化（縮小・ぼかし・コントラスト低下・ノイズ）。"""
    small = cv2.resize(bgr, (1280, int(bgr.shape[0] * 1280 / bgr.shape[1])), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (3, 3), 0.8)
    noisy = small.astype(np.int16) * 0.8 + 25 + np.random.default_rng(0).normal(0, 4, small.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def prepare(sender, paths, repair_ratio: int = 0):
    from qrtransfer.ui.sender_view import PrepareWorker
    sender.set_paths([str(p) for p in paths])
    w = PrepareWorker(sender.paths, sender.spin_chunk.value(), sender.combo_ecc.currentData(), repair_ratio)
    got = {}
    w.finished_ok.connect(lambda plan, cache, seqs: got.update(plan=plan, cache=cache, seqs=seqs))
    w.failed.connect(lambda m: got.update(error=m))
    w.run()  # 同じスレッドで同期実行
    assert "error" not in got, got.get("error")
    return got["plan"], got["cache"], got["seqs"]


def test_send_screen_to_receiver(env, tmp_path, app):
    from qrtransfer.ui.main_window import MainWindow
    src = tmp_path / "src" / "送信フォルダ"
    files = {"a.txt": "日本語テキスト\n".encode() * 200, "sub/b.bin": os.urandom(2500)}
    write_tree(src, files, empty_dirs=["empty"])

    win = MainWindow(env)
    win.show_sender()
    plan, cache, seqs = prepare(win.sender, [src])
    receiver = win.show_receiver()

    from qrtransfer.ui.fullscreen_qr import FullscreenQR
    fs = FullscreenQR(plan, cache, seqs, fps=15)
    fs.resize(1600, 900)
    fs.show()
    app.processEvents()

    # キー操作
    QTest.keyClick(fs, Qt.Key.Key_Space)
    assert fs.paused
    pos = fs.pos
    QTest.keyClick(fs, Qt.Key.Key_Right)
    assert fs.pos == pos + 1
    QTest.keyClick(fs, Qt.Key.Key_Left)
    assert fs.pos == pos
    QTest.keyClick(fs, Qt.Key.Key_Minus)
    assert fs.fps == 14
    QTest.keyClick(fs, Qt.Key.Key_Plus)
    assert fs.fps == 15

    # QR が短辺の約 90% 以内で、ステータス表示と重ならない
    r = fs.qr_rect()
    assert r.height() <= fs.height() * 0.9 and r.bottom() < fs.height() - 36

    # 1 周目は欠落を作る
    skipped = set()
    for i in range(len(fs.order)):
        seq = fs.current_seq()
        img = camera_like(qimage_to_bgr(fs.grab().toImage()))
        if seq != packer.CAROUSEL_META and seq % 3 == 1:
            skipped.add(seq)
        else:
            dets, _ = process_image(img, receiver.assembler)
            assert dets and all(d.ok for d in dets), f"seq {seq} not decoded"
        fs.advance()
    app.processEvents()
    receiver.refresh()
    assert set(receiver.assembler.missing()) == skipped
    assert receiver.txt_missing.toPlainText()
    receiver.copy_missing()
    missing_text = QtWidgets.QApplication.clipboard().text()
    assert missing_text == packer.protocol.format_ranges(skipped)

    # 再送モード: 欠落番号だけを表示
    fs.set_resend(packer.protocol.parse_ranges(missing_text, limit=plan.total))
    assert [s for s in fs.order if s != packer.CAROUSEL_META] == sorted(skipped)
    for _ in range(len(fs.order)):
        process_image(camera_like(qimage_to_bgr(fs.grab().toImage())), receiver.assembler)
        fs.advance()
    app.processEvents()
    assert receiver.done_bar.isVisibleTo(receiver)
    assert "OK" in receiver.lbl_done.text()
    result = receiver.last_result
    got, dirs = read_tree(result.path)
    assert got == files and "empty" in dirs
    fs.close()
    win.close()


def test_send_with_repair_qr(env, tmp_path, app):
    """修復用 QR を混ぜた送信: DATA を 3 分の 1 取りこぼしても、再送の指示なしに 1 周のうちに完了する。"""
    from qrtransfer.ui.fullscreen_qr import FullscreenQR
    from qrtransfer.ui.main_window import MainWindow
    src = tmp_path / "src" / "修復.bin"
    src.parent.mkdir()
    content = os.urandom(6000)
    src.write_bytes(content)

    win = MainWindow(env)
    win.show_sender()
    assert win.sender.combo_method.currentData() is True  # 既定は修復用 QR を混ぜる
    win.sender.combo_method.setCurrentIndex(1)
    assert not win.sender.spin_repair.isEnabled() and win.sender.repair_ratio() == 0
    win.sender.combo_method.setCurrentIndex(0)
    plan, cache, seqs = prepare(win.sender, [src], win.sender.repair_ratio())
    assert "修復用" in win.sender.lbl_estimate.text()
    repairs = [s for s in seqs if packer.repair_index(s) is not None]
    assert len(repairs) == packer.repair.repair_count(plan.total, win.sender.spin_repair.value())
    receiver = win.show_receiver()

    fs = FullscreenQR(plan, cache, seqs, fps=15)
    fs.resize(1600, 900)
    fs.show()
    app.processEvents()
    assert fs.repairs == len(repairs)
    shown = 0
    checked_ui = False
    for _ in range(len(fs.order)):
        seq = fs.current_seq()
        if packer.repair_index(seq) is not None and not checked_ui and receiver.assembler.snapshot().repair:
            # 修復用 QR を受け取ったら、欠落番号（再送用）は出さない。進み具合は修復用の式も含めて進む
            receiver.refresh()
            snap = receiver.assembler.snapshot()
            assert not receiver.txt_missing.isVisibleTo(receiver) and not receiver.btn_copy.isVisibleTo(receiver)
            assert receiver.btn_discard.isVisibleTo(receiver)
            assert receiver.progress.value() == int((snap.received + snap.pending) / snap.total * 1000)
            checked_ui = True
        if seq == packer.CAROUSEL_META or packer.repair_index(seq) is not None or seq % 3 != 1:
            _, statuses = process_image(camera_like(qimage_to_bgr(fs.grab().toImage())), receiver.assembler)
            shown += 1
            if "complete" in statuses:
                break
        fs.advance()
    app.processEvents()
    assert shown < len(fs.order)  # 1 周を待たずに完了した
    assert checked_ui
    result = receiver.last_result
    assert result is not None and result.ok, result and result.message
    assert result.path.read_bytes() == content
    # 再送（番号指定）はこの方式でも使える
    fs.set_resend({0, 1})
    assert [s for s in fs.order if s != packer.CAROUSEL_META] == [0, 1]
    fs.set_resend(None)
    assert len(fs.order) == len(packer.carousel_order(plan.total, repairs=len(repairs)))
    fs.close()
    win.close()


def test_decode_thread_with_fake_camera(env, tmp_path, app):
    from qrtransfer.ui.fullscreen_qr import FullscreenQR
    from qrtransfer.ui.receiver_view import ReceiverView
    from qrtransfer.ui.sender_view import SenderView
    f = tmp_path / "one.bin"
    f.write_bytes(os.urandom(6000))
    sender = SenderView(env)
    plan, cache, seqs = prepare(sender, [f])
    receiver = ReceiverView(env)
    fs = FullscreenQR(plan, cache, seqs, fps=15)
    fs.resize(1280, 720)
    fs.show()
    app.processEvents()
    images = []
    for _ in range(len(fs.order)):
        images.append(camera_like(qimage_to_bgr(fs.grab().toImage())))
        fs.advance()
    fs.close()

    source = FrameSource()
    th = DecodeThread(source, receiver.assembler)
    dets_seen = []
    th.detections.connect(lambda d, w, h: dets_seen.append(d))
    th.start()
    loop = QEventLoop()
    receiver.bridge.complete.connect(lambda _r: loop.quit())
    idx = [0]

    def feed():
        source.put(images[idx[0] % len(images)])
        idx[0] += 1

    t = QTimer()
    t.timeout.connect(feed)
    t.start(40)
    QTimer.singleShot(60_000, loop.quit)
    loop.exec()
    t.stop()
    th.stop()
    th.wait(5000)
    assert receiver.last_result is not None and receiver.last_result.ok
    assert receiver.last_result.path.read_bytes() == f.read_bytes()
    assert any(dets_seen)
    receiver.refresh()
    receiver.shutdown()


def test_resume_dialog_and_settings(env, tmp_path, app, monkeypatch):
    from qrtransfer.assembler import Assembler
    from qrtransfer.ui.main_window import MainWindow, ResumeDialog, SettingsDialog
    f = tmp_path / "r.bin"
    f.write_bytes(os.urandom(3000))
    plan = packer.make_plan([f], chunk_size=300)
    asm = Assembler(tmp_path / "out")  # 既定の %LOCALAPPDATA%（テストでは tmp）
    asm.feed_bytes(plan.meta_frame())
    for i in range(0, plan.total, 2):
        asm.feed_bytes(plan.data_frame(i))
    asm.close()
    sessions = Assembler.list_incomplete()
    assert len(sessions) == 1

    dlg = ResumeDialog(sessions)
    dlg._resume()
    assert dlg.choice.session_id == plan.session_id

    win = MainWindow(env)
    monkeypatch.setattr(ResumeDialog, "exec", lambda self: (setattr(self, "choice", self.sessions[0]), 1)[1])
    win.check_incomplete_sessions()
    rv = win.receiver
    assert rv is not None and rv.assembler.snapshot().received == (plan.total + 1) // 2
    for i in range(plan.total):
        rv.assembler.feed_bytes(plan.data_frame(i))
    app.processEvents()
    assert rv.last_result.ok and rv.last_result.path.read_bytes() == f.read_bytes()

    sd = SettingsDialog(env)
    sd.spin_fps.setValue(9)
    sd.combo_ecc.setCurrentIndex(0)
    sd.accept()
    from qrtransfer.settings import Settings
    s2 = Settings(tmp_path / "settings.ini")
    assert s2.fps == 9 and s2.ecc == "l" and s2.output_dir == str(tmp_path / "out")
    win.close()


def test_conflict_signal_with_large_session_id(env, tmp_path, app):
    """session_id >= 2^31 でもシグナルが溢れず、「別の転送を検出」が表示されること。"""
    from qrtransfer.ui.receiver_view import ReceiverView
    f = tmp_path / "c.bin"
    f.write_bytes(os.urandom(2000))
    p1 = packer.make_plan([f], chunk_size=300, session_id=0xF0000001)
    p2 = packer.make_plan([f], chunk_size=300, session_id=0xFFFFFFFF)
    rv = ReceiverView(env)
    seen = []
    rv.bridge.conflict.connect(seen.append)
    rv.assembler.feed_bytes(p1.data_frame(0))
    rv.assembler.feed_bytes(p2.data_frame(0))
    app.processEvents()
    rv.refresh()
    assert seen == [0xFFFFFFFF]
    assert rv.conflict_bar.isVisibleTo(rv) and "ffffffff" in rv.lbl_conflict.text()
    rv.switch_session()
    assert rv.assembler.session_id == 0xFFFFFFFF
    rv.shutdown()


def test_window_mode_and_toggle(env, tmp_path, app):
    """ウィンドウモードで表示でき、F / T キーで切り替えられ、状態が設定に保存されること。"""
    from qrtransfer.ui.fullscreen_qr import FullscreenQR, TEXT_AREA
    from qrtransfer.ui.main_window import MainWindow
    from qrtransfer.settings import Settings

    f = tmp_path / "w.bin"
    f.write_bytes(os.urandom(4000))

    env.qr_fullscreen = False
    env.qr_always_on_top = False
    win = MainWindow(env)
    win.show_sender()
    assert win.sender.combo_display.currentData() is False
    plan, cache, seqs = prepare(win.sender, [f])

    states = []
    win.settings.sync()
    win.sender.start_fullscreen.connect(lambda *a: None)
    win.start_fullscreen(plan, cache, seqs, 6)
    fs = win.fullscreen
    assert fs is not None and not fs.want_fullscreen
    assert not fs.isFullScreen()
    fs.resize(700, 760)
    app.processEvents()

    # ウィンドウモードでは、QR は余白を除いてほぼ全幅
    r = fs.qr_rect()
    assert r.width() >= 700 - 2 * 8 - fs.cache.size and r.bottom() < fs.height() - TEXT_AREA + 1
    # 表示中の QR は正しく読める（縮小されても）
    dets, _ = process_image(camera_like(qimage_to_bgr(fs.grab().toImage())), win.show_receiver().assembler)
    assert dets and all(d.ok for d in dets)

    # F で全画面、もう一度 F でウィンドウに戻る
    QTest.keyClick(fs, Qt.Key.Key_F)
    app.processEvents()
    assert fs.want_fullscreen
    QTest.keyClick(fs, Qt.Key.Key_F)
    app.processEvents()
    assert not fs.want_fullscreen
    # T で「常に手前」
    QTest.keyClick(fs, Qt.Key.Key_T)
    app.processEvents()
    assert fs.always_on_top and fs.state()["always_on_top"]

    fs.closed.connect(states.append)
    fs.close()
    app.processEvents()
    assert states and states[0]["always_on_top"] and states[0]["fullscreen"] is False
    assert states[0]["geometry"]

    # 設定に保存され、次回も同じモードで開く
    s2 = Settings(tmp_path / "settings.ini")
    assert s2.qr_fullscreen is False and s2.qr_always_on_top is True and s2.qr_window_geometry
    win2 = MainWindow(s2)
    win2.start_fullscreen(plan, cache, seqs, 6)
    assert not win2.fullscreen.want_fullscreen and win2.fullscreen.always_on_top
    win2.fullscreen.close()
    win2.close()
    win.close()


def test_fullscreen_mode_still_default(env, tmp_path, app):
    from qrtransfer.ui.fullscreen_qr import FullscreenQR
    from qrtransfer.settings import Settings
    s = Settings(tmp_path / "fresh.ini")
    assert s.qr_fullscreen is True and s.qr_always_on_top is False and s.qr_window_geometry == b""
    f = tmp_path / "x.bin"
    f.write_bytes(os.urandom(1000))
    plan = packer.make_plan([f], chunk_size=300)
    from qrtransfer import qrgen
    v = qrgen.choose_version(plan.max_data_frame_size, plan.min_meta_frame_size(), "m")
    frames = [plan.meta_frame(qrgen.capacity(v, "m"))] + [plan.data_frame(i) for i in range(plan.total)]
    cache = qrgen.generate_cache(frames, v, "m", workers=1)
    fs = FullscreenQR(plan, cache, [packer.CAROUSEL_META] + list(range(plan.total)), 6)
    fs.start()
    app.processEvents()
    assert fs.want_fullscreen and fs.isFullScreen()
    r = fs.qr_rect()
    assert r.height() <= min(fs.width(), fs.height()) * 0.9
    fs.close()


def test_wait_for_start(env, tmp_path, app):
    """待機状態で開き、最初の QR（META）を静止表示し、Space / Enter / ダブルクリックで送信が始まること。"""
    from qrtransfer.settings import Settings
    from qrtransfer.ui.main_window import MainWindow

    assert Settings(tmp_path / "fresh.ini").qr_wait_for_start is True  # 既定は待機する

    f = tmp_path / "wait.bin"
    f.write_bytes(os.urandom(5000))
    env.qr_fullscreen = False
    win = MainWindow(env)
    win.show_sender()
    assert win.sender.chk_wait.isChecked()
    plan, cache, seqs = prepare(win.sender, [f])
    receiver = win.show_receiver()

    win.start_fullscreen(plan, cache, seqs, 15)
    fs = win.fullscreen
    fs.resize(800, 860)
    app.processEvents()
    assert fs.waiting and not fs.timer.isActive()
    assert fs.current_seq() == packer.CAROUSEL_META

    # 待機中は時間が経っても QR が進まない
    loop = QEventLoop()
    QTimer.singleShot(300, loop.quit)
    loop.exec()
    assert fs.pos == 0 and fs.waiting

    # 待機中の QR（META）は読めるので、受信側はファイル名を確認しながらカメラを調整できる
    process_image(camera_like(qimage_to_bgr(fs.grab().toImage())), receiver.assembler)
    snap = receiver.assembler.snapshot()
    assert snap.meta is not None and snap.meta["name"] == "wait.bin" and snap.received == 0

    # 待機中の ←/→ や fps 変更では開始しない
    QTest.keyClick(fs, Qt.Key.Key_Right)
    QTest.keyClick(fs, Qt.Key.Key_Plus)
    assert fs.waiting and fs.pos == 0 and not fs.timer.isActive()

    # Enter で開始
    QTest.keyClick(fs, Qt.Key.Key_Return)
    assert not fs.waiting and fs.timer.isActive() and not fs.paused
    # 開始後の Space は従来どおり一時停止
    QTest.keyClick(fs, Qt.Key.Key_Space)
    assert fs.paused and not fs.timer.isActive()
    fs.close()

    # Space とダブルクリックでも開始できる
    win.start_fullscreen(plan, cache, seqs, 15)
    fs = win.fullscreen
    QTest.keyClick(fs, Qt.Key.Key_Space)
    assert not fs.waiting and not fs.paused and fs.timer.isActive()
    fs.close()
    win.start_fullscreen(plan, cache, seqs, 15)
    fs = win.fullscreen
    QTest.mouseDClick(fs, Qt.MouseButton.LeftButton)
    assert not fs.waiting and fs.timer.isActive()
    fs.close()

    # 待機しない設定にすると、すぐに始まる
    win.sender.chk_wait.setChecked(False)
    win.sender.save_params()
    assert Settings(tmp_path / "settings.ini").qr_wait_for_start is False
    win.start_fullscreen(plan, cache, seqs, 15)
    fs = win.fullscreen
    assert not fs.waiting and fs.timer.isActive()
    fs.close()
    win.close()


def test_ecc_follows_send_method(env, tmp_path, app):
    """修復用 QR を混ぜるときの既定は ECC L。以前の既定値（M）は L に移し、送信方式を切り替えるとおすすめに合わせる。"""
    from qrtransfer.settings import FPS_MAX, Settings
    from qrtransfer.ui.main_window import SettingsDialog
    from qrtransfer.ui.sender_view import SenderView
    assert FPS_MAX == 30
    assert Settings(tmp_path / "fresh.ini").ecc == "l"
    legacy = Settings(tmp_path / "legacy.ini")
    legacy.use_repair = False
    assert legacy.ecc == "m"
    # 以前の版で保存された設定: M（旧既定）は L へ、ユーザーが選んだ Q はそのまま、移行は一度だけ
    from PySide6.QtCore import QSettings
    for name, old, new in (("old_m.ini", "m", "l"), ("old_q.ini", "q", "q")):
        q = QSettings(str(tmp_path / name), QSettings.Format.IniFormat)
        q.setValue("sender/ecc", old)
        q.sync()
        s = Settings(tmp_path / name)
        assert s.ecc == new
        s.ecc = "m"
        s.sync()
        assert Settings(tmp_path / name).ecc == "m"

    view = SenderView(env)
    view.combo_method.setCurrentIndex(1)  # 従来
    assert view.combo_ecc.currentData() == "m"
    view.combo_method.setCurrentIndex(0)  # 修復用 QR
    assert view.combo_ecc.currentData() == "l"
    assert view.spin_fps.maximum() == 30
    dlg = SettingsDialog(env)
    dlg.combo_method.setCurrentIndex(1)
    assert dlg.combo_ecc.currentData() == "m" and not dlg.spin_repair.isEnabled()
    dlg.combo_method.setCurrentIndex(0)
    assert dlg.combo_ecc.currentData() == "l" and dlg.spin_repair.isEnabled()
