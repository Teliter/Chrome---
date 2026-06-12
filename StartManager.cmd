@echo off
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
  start "" "%%P" "%~dp0ChromeManager.pyw"
  exit /b 0
)
echo Python 3.11 or newer is required.
pause
exit /b 1
