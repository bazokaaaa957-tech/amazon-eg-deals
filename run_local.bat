@echo off
rem Runs the tracker once on this PC (no WhatsApp). Needs Python 3.10+.
rem Examples:
rem   run_local.bat                 scan and update web\data\deals.json
rem   run_local.bat --dry-run       scan and print only
setlocal
set "VENV=%LOCALAPPDATA%\amazon-eg-deals-venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Preparing Python environment, first time only...
  python -m venv "%VENV%" || goto :error
  "%VENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt" || goto :error
)
set PYTHONIOENCODING=utf-8
"%VENV%\Scripts\python.exe" "%~dp0scraper\tracker.py" --no-notify %*
goto :eof
:error
echo Setup failed. Make sure Python is installed from https://www.python.org
exit /b 1
