@echo off
echo ============================================
echo   KRLX Agent - Starting Up
echo ============================================
echo.

:: Create directories
if not exist "%USERPROFILE%\Desktop\KRLX_Agent" mkdir "%USERPROFILE%\Desktop\KRLX_Agent"
if not exist "%USERPROFILE%\Desktop\KRLX_Inbox" mkdir "%USERPROFILE%\Desktop\KRLX_Inbox"

:: Run the agent
echo Starting KRLX Remote Agent...
echo.
python "%~dp0krlx_agent.py"

pause
