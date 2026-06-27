@echo off
title KRLX Sovereign AI Stack — Full Launch
color 0A

echo.
echo  ============================================================
echo   KRLX SOVEREIGN AI STACK — FULL LAUNCH
echo   Whisper Server + Remote Agent + Tailscale Funnel
echo  ============================================================
echo.

:: ── Step 1: Create required folders ──────────────────────────────
echo [1/6] Creating folders...
if not exist "%USERPROFILE%\Desktop\KRLX_Inbox"  mkdir "%USERPROFILE%\Desktop\KRLX_Inbox"
if not exist "%USERPROFILE%\Desktop\KRLX_Agent"  mkdir "%USERPROFILE%\Desktop\KRLX_Agent"
echo       Done.
echo.

:: ── Step 2: Install Python deps if needed ─────────────────────────
echo [2/6] Checking Python dependencies...
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo       Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install --quiet --upgrade fastapi uvicorn[standard] python-multipart faster-whisper 2>nul
echo       Dependencies ready.
echo.

:: ── Step 3: Enable Tailscale Funnel via admin API ─────────────────
echo [3/6] Enabling Tailscale Funnel...
tailscale funnel --bg 8742 >nul 2>&1
if errorlevel 1 (
    echo       Funnel not yet enabled. Opening Tailscale admin page...
    start "" "https://login.tailscale.com/f/funnel?node=n2sp8Mo2FQ11CNTRL"
    echo.
    echo  *** ACTION REQUIRED ***
    echo  Your browser just opened the Tailscale admin page.
    echo  Click ENABLE on that page, then press any key here to continue.
    echo  ************************
    pause >nul
    tailscale funnel --bg 8742 >nul 2>&1
)
echo       Funnel active on port 8742.
echo.

:: ── Step 4: Get the public Funnel URL ─────────────────────────────
echo [4/6] Getting public URL...
for /f "tokens=*" %%i in ('tailscale funnel status 2^>^&1') do (
    echo       %%i
)
echo.

:: ── Step 5: Start Whisper server in background ────────────────────
echo [5/6] Starting Whisper transcription server (port 8741)...
start "KRLX Whisper Server" cmd /k "cd /d %~dp0 && call .venv\Scripts\activate.bat && python app.py"
timeout /t 3 /nobreak >nul
echo       Whisper server started.
echo.

:: ── Step 6: Start Remote Agent in background ──────────────────────
echo [6/6] Starting Remote Agent (port 8742)...
start "KRLX Remote Agent" cmd /k "cd /d %~dp0 && call .venv\Scripts\activate.bat && python krlx_agent.py"
timeout /t 3 /nobreak >nul
echo       Remote Agent started.
echo.

:: ── Done ──────────────────────────────────────────────────────────
echo  ============================================================
echo   ALL SYSTEMS RUNNING
echo  ============================================================
echo.
echo   Port 8741  — Whisper transcription (voice to text)
echo   Port 8742  — Remote agent (Manus command bridge)
echo.
echo   Your Tailscale IP:
tailscale ip -4
echo.
echo   Public Funnel URL (for Manus to connect):
tailscale funnel status 2>&1
echo.
echo   Transcripts saved to: %USERPROFILE%\Desktop\KRLX_Inbox
echo   Agent logs at:        %USERPROFILE%\Desktop\KRLX_Agent\agent.log
echo.
echo   Close this window when done. The two service windows stay running.
echo  ============================================================
echo.
pause
