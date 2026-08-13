@echo off
REM ===================================================================
REM  Give the kiosks an address they can reach.
REM
REM  Run this in a SECOND window, after start-windows.bat is running.
REM
REM  Why this is needed: your laptop has no fixed address on the
REM  internet, and phone browsers refuse to switch the camera on unless
REM  the page is served over https. Cloudflare Tunnel solves both.
REM
REM  Install once:  winget install --id Cloudflare.cloudflared
REM ===================================================================

where cloudflared >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo   cloudflared is not installed.
  echo.
  echo   Open PowerShell and run:
  echo       winget install --id Cloudflare.cloudflared
  echo.
  echo   Then run this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting the tunnel to the attendance server...
echo.
echo   A https://....trycloudflare.com address will appear below.
echo   Open it on each kiosk tablet and add /kiosk to the end.
echo.
echo   NOTE: this quick tunnel gives a NEW address every time it starts.
echo   For a permanent address, set up a named tunnel - see RUNBOOK.md.
echo.

cloudflared tunnel --url http://127.0.0.1:8080
