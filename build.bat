@echo off
rem Builds dist\ChestCounter.exe (one file, no console window). Needs: pip install pyinstaller
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --windowed --name ChestCounter ^
  --icon assets\icon.ico --add-data "assets;assets" ^
  --exclude-module pygame --exclude-module matplotlib --exclude-module PIL --exclude-module scipy ^
  --exclude-module pandas --exclude-module IPython ^
  chest_counter.py
