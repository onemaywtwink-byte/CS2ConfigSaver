@echo off
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller || python -m pip install --upgrade pyinstaller
py -3 -m PyInstaller --noconfirm --onefile --windowed --name CS2ConfigSaver --add-data "ui.html;." cs2_config_saver.py || python -m PyInstaller --noconfirm --onefile --windowed --name CS2ConfigSaver --add-data "ui.html;." cs2_config_saver.py
if errorlevel 1 (echo BUILD FAILED) else (echo Done: dist\CS2ConfigSaver.exe)
pause
