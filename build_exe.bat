@echo off
REM Build ZeroToken.exe — run this from the project root.
REM Requires the .venv to exist: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt

echo Installing PyInstaller...
.venv\Scripts\python.exe -m pip install pyinstaller -q

echo Building ZeroToken.exe...
.venv\Scripts\python.exe -m PyInstaller _launcher_entry.py ^
    --onefile --noconsole --name ZeroToken ^
    --distpath . --workpath build\pyinstaller --specpath build

echo.
if exist ZeroToken.exe (
    echo Done! ZeroToken.exe is ready.
) else (
    echo Build failed — check output above.
)
pause
