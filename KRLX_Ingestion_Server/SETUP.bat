@echo off
echo ============================================
echo   KRLX Full Setup - One Time Only
echo ============================================
echo.

:: Create all directories
echo Creating directories...
if not exist "%USERPROFILE%\Desktop\KRLX_Agent" mkdir "%USERPROFILE%\Desktop\KRLX_Agent"
if not exist "%USERPROFILE%\Desktop\KRLX_Inbox" mkdir "%USERPROFILE%\Desktop\KRLX_Inbox"
echo Done.
echo.

:: Create virtual environment
echo Creating Python virtual environment...
python -m venv "%~dp0.venv"
echo Done.
echo.

:: Activate and install
echo Installing dependencies (this may take a few minutes)...
call "%~dp0.venv\Scripts\activate.bat"
pip install --upgrade pip
pip install fastapi uvicorn[standard] python-multipart faster-whisper
echo.
echo ============================================
echo   SETUP COMPLETE
echo ============================================
echo.
echo Next steps:
echo   1. Double-click START_SERVER.bat to run the transcription server
echo   2. Double-click START_AGENT.bat to run the remote agent
echo.
echo Both should be running for full functionality.
echo.
pause
