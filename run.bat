@echo off
chcp 65001 >nul
py -3 cs2_config_saver.py || python cs2_config_saver.py
if errorlevel 1 pause
