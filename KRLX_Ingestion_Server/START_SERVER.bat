@echo off
echo ============================================
echo   KRLX Ingestion Server - Starting Up
echo ============================================
echo.

:: Activate venv if it exists
if exist "%~dp0.venv\Scripts\activate.bat" (
    call "%~dp0.venv\Scripts\activate.bat"
)

:: Create inbox
if not exist "%USERPROFILE%\Desktop\KRLX_Inbox" mkdir "%USERPROFILE%\Desktop\KRLX_Inbox"

:: Start server
echo Starting Whisper transcription server on port 8741...
echo.
python "%~dp0app.py"

pause
