@echo off
echo Building Zwoosh.exe ...
pyinstaller --onefile --windowed --name Zwoosh --icon=assets/icon.ico gui.py
echo.
echo Done! Output: dist\Zwoosh.exe
pause
