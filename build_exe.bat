@echo off
chcp 65001 >nul
py -3 -m pip install pyinstaller
py -3 -m PyInstaller --onefile --windowed --name CS2ConfigSaver --add-data "ui.html;." cs2_config_saver.py
echo Готово: dist\CS2ConfigSaver.exe
pause
