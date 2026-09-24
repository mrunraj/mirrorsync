# Builds a standalone MirrorSync.exe with PyInstaller.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
python -m pip install --user pyinstaller pystray Pillow
python -m PyInstaller --onefile --windowed --name MirrorSync mirrorsync.py
Write-Host ""
Write-Host "Done. Output: dist\MirrorSync.exe"
