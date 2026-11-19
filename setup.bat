@echo off
rem One-time setup: local virtual environment + dependencies (nothing is installed globally).
cd /d "%~dp0"
where uv >nul 2>nul
if %errorlevel%==0 (
  uv venv .venv --python 3.13
  uv pip install --python .venv\Scripts\python.exe -e ".[dev,robinhood-unofficial]"
) else (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -e ".[dev,robinhood-unofficial]"
)
echo.
echo Done. Start the app with StockDesk.bat
pause
