@echo off
setlocal

:: Read version from hush.py
for /f "delims=" %%i in ('python -c "import re; print(re.search(r\"__version__ = '(.+?)'\", open('hush.py').read()).group(1))"') do set VERSION=%%i
echo Building Hush v%VERSION%...

:: Activate venv
call venv\Scripts\activate.bat

:: Clean previous build
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist hush.spec del hush.spec

:: Build exe
pyinstaller --noconsole --onefile ^
  --icon "pink_circle.ico" ^
  --add-data "pink_noise.flac;." ^
  --add-data "brown_noise.flac;." ^
  --add-data "grey_noise.flac;." ^
  hush.py

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

:: Package into zip
set ZIPNAME=hush-v%VERSION%.zip
if exist %ZIPNAME% del %ZIPNAME%
powershell -Command "Compress-Archive -Path 'dist\hush.exe','pink_noise.flac','brown_noise.flac','grey_noise.flac' -DestinationPath '%ZIPNAME%' -Force"

echo.
echo Done: %ZIPNAME%
echo Upload this file to GitHub Releases as v%VERSION%
pause
