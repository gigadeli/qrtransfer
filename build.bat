@echo off
rem Build QRTransfer.exe (run on Windows with internet access)
rem   build.bat          -> single exe  : dist\QRTransfer.exe
rem   build.bat onedir   -> folder build: dist\QRTransfer\QRTransfer.exe (faster startup)
setlocal
cd /d %~dp0

set PYVER=3.12
py -%PYVER% --version >nul 2>&1
if errorlevel 1 (
  echo [WARN] Python %PYVER% not found. Falling back to the default Python 3.
  set PYVER=3
)

if not exist .venv\Scripts\python.exe (
  py -%PYVER% -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :err
pytest -q || goto :err

set QRT_ONEDIR=
set EXE=dist\QRTransfer.exe
if /i "%~1"=="onedir" (
  set QRT_ONEDIR=1
  set EXE=dist\QRTransfer\QRTransfer.exe
)
pyinstaller --noconfirm QRTransfer.spec || goto :err

echo.
echo Checking that the exe alone can generate and read QR codes...
start "" /wait "%EXE%" --selftest --quiet
if errorlevel 1 (
  echo [NG] Self-test failed. See %LOCALAPPDATA%\QRTransfer\selftest.log
  goto :err
)
type "%LOCALAPPDATA%\QRTransfer\selftest.log"
echo.
echo.
echo Build OK: %EXE%
exit /b 0
:err
echo Build FAILED
exit /b 1
