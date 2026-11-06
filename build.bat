@echo off
python -m pip install --upgrade pyinstaller pillow
python -m PyInstaller --onefile --noconsole --name gamma_toggle gamma_toggle.py
echo.
echo Готово: dist\gamma_toggle.exe
pause
