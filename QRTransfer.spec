# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: pyinstaller --noconfirm QRTransfer.spec
#   既定は --onefile 相当（dist\QRTransfer.exe）。
#   環境変数 QRT_ONEDIR=1 を指定すると --onedir 相当（dist\QRTransfer\QRTransfer.exe）でビルドする。

import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ONEDIR = os.environ.get("QRT_ONEDIR") == "1"

# zxing-cpp のネイティブ拡張（.pyd）と依存 DLL を確実に同梱する
binaries = collect_dynamic_libs("zxingcpp")
hiddenimports = ["zxingcpp"] + collect_submodules("qrtransfer")

excludes = [
    # 使わない Qt モジュール（サイズ削減）
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
    "PySide6.QtHttpServer", "PySide6.QtNetworkAuth", "PySide6.QtConcurrent", "PySide6.QtXml",
    "PySide6.QtAxContainer", "PySide6.QtUiTools", "PySide6.QtCanvasPainter",
    # テスト・開発用
    "pytest", "_pytest", "PIL", "tkinter", "matplotlib", "IPython",
    # 使わない標準ライブラリ・ビルドツール（PYZ の削減）
    # 注: segno.writers → xml.sax.saxutils → urllib.request → email/http と連鎖するので、これらは除外しないこと
    "setuptools", "pkg_resources", "pydoc", "pydoc_data", "doctest", "unittest",
    "xmlrpc", "asyncio", "ssl", "_ssl", "_hashlib", "sqlite3", "curses", "pdb",
    "distutils", "lib2to3", "test", "idlelib", "turtledemo",
]

a = Analysis(
    ["src/qrtransfer/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=[("assets/app.ico", "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# Qt の不要なプラグイン・翻訳・大きな DLL を取り除く
_DROP = (
    "qtwebengine", "Qt6WebEngine", "Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6Multimedia", "Qt6VirtualKeyboard",
    "Qt6Designer", "Qt6Charts", "Qt6Graphs", "Qt6DataVisualization", "Qt63D", "Qt6Location", "Qt6Svg",
    "opengl32sw.dll", "Qt6ShaderTools", "Qt6Sql", "Qt6Help", "Qt6Bluetooth", "Qt6SerialPort",
    "opencv_videoio_ffmpeg",  # DSHOW/MSMF を使うので FFmpeg バックエンドは不要（約 30MB）
    "/translations/", "\\translations\\", "plugins/qmltooling", "plugins\\qmltooling",
    "plugins/multimedia", "plugins\\multimedia", "plugins/sqldrivers", "plugins\\sqldrivers",
    "plugins/position", "plugins\\position", "plugins/sensors", "plugins\\sensors",
    "plugins/networkinformation", "plugins\\networkinformation", "plugins/tls", "plugins\\tls",
    "plugins/generic", "plugins\\generic", "plugins/iconengines", "plugins\\iconengines",
    "qdirect2d.dll",  # 既定の qwindows プラットフォームプラグインだけを使う
    "libcrypto-", "libssl-",  # _ssl / _hashlib を除外したので不要（hashlib は Python 内蔵実装を使う）
)
# 画像フォーマットのプラグインは、アプリアイコン (.ico) 用の qico だけ残す
_KEEP_IMAGEFORMATS = ("qico.dll",)
_IMAGEFORMATS = ("plugins/imageformats", "plugins\\imageformats")
def _keep(dest: str) -> bool:
    low = dest.lower()
    if any(d.lower() in low for d in _DROP):
        return False
    if any(p in low for p in _IMAGEFORMATS):
        return any(k in low for k in _KEEP_IMAGEFORMATS)
    return True


a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure)

exe_kwargs = dict(
    name="QRTransfer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="assets/app.ico",
    version="version_info.txt",
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="QRTransfer")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **exe_kwargs)
