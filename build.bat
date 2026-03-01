@echo off
setlocal

:: Always run from the script's own directory
cd /d "%~dp0"

:: Read version from hush.py
for /f "delims=" %%i in ('python _get_version.py') do set VERSION=%%i
if "%VERSION%"=="" (
    echo ERROR: Could not read version from hush.py
    pause
    exit /b 1
)
echo Building Hush v%VERSION%...

:: Activate venv
if not exist venv\Scripts\activate.bat (
    echo ERROR: venv not found. Run: python -m venv venv ^&^& venv\Scripts\activate ^&^& pip install pyinstaller pystray Pillow numpy sounddevice soundfile
    pause
    exit /b 1
)
call venv\Scripts\activate.bat

:: Clean previous build
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist hush.spec del hush.spec

:: Build exe
pyinstaller --noconsole --onefile ^
  --icon "pink_circle.ico" ^
  --add-data "pink_noise.ogg;." ^
  --add-data "brown_noise.ogg;." ^
  --add-data "grey_noise.ogg;." ^
  hush.py

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. See output above.
    pause
    exit /b 1
)

:: Package into zip
set ZIPNAME=hush-v%VERSION%.zip
if exist %ZIPNAME% del %ZIPNAME%
powershell -Command "Compress-Archive -Path 'dist\hush.exe' -DestinationPath '%ZIPNAME%' -Force"

if errorlevel 1 (
    echo ERROR: Failed to create zip.
    pause
    exit /b 1
)

echo.
echo Done: %ZIPNAME%
echo Upload this file to GitHub Releases as v%VERSION%
pause
