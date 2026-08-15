@echo off
REM ===================================================================
REM  Attendance ^& Payroll - start the system on the Windows laptop.
REM
REM  Double-click this file. It sets everything up on the first run and
REM  just starts the server on every run after that.
REM
REM  Leave this window open while the shops are working. Closing it
REM  stops the server; the kiosks keep queueing punches and upload them
REM  the next time it is running.
REM ===================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo.
echo   Attendance ^& Payroll
echo   ====================
echo.

REM --- find Python ---------------------------------------------------
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if !errorlevel!==0 (
    set "PY=python"
  ) else (
    echo   Python is not installed.
    echo.
    echo   Install Python 3.12 from https://www.python.org/downloads/
    echo   and TICK "Add python.exe to PATH" during setup. Then run this
    echo   file again.
    echo.
    pause
    exit /b 1
  )
)

REM --- first run: create the environment ------------------------------
if not exist "backend\.venv\Scripts\python.exe" (
  echo   First run - setting up. This takes a few minutes.
  echo.
  %PY% -m venv backend\.venv
  if errorlevel 1 goto :failed
  backend\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
  backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt --quiet
  if errorlevel 1 goto :failed
  echo   Setup done.
  echo.
)

set "VENV_PY=backend\.venv\Scripts\python.exe"

REM --- sign the session cookie with a secret unique to this machine ----
if not exist ".env" (
  echo   Creating .env with a new secret key...
  for /f "delims=" %%K in ('%VENV_PY% -c "import secrets;print(secrets.token_urlsafe(48))"') do set "KEY=%%K"
  > .env echo ATTENDANCE_SECRET_KEY=!KEY!
  >> .env echo ATTENDANCE_BUSINESS_NAME=Dry Cleaning Franchise
  echo   Done. Edit .env to change the business name on payslips.
  echo.
)

REM --- first run: create the database and the admin account -----------
if not exist "data\attendance.db" (
  echo   Creating the database...
  %VENV_PY% backend\scripts\seed.py
  echo.
  echo   ^>^> Write down the admin password shown above. ^<^<
  echo.
  pause
)

REM --- face verification models --------------------------------------
REM  Roughly 39 MB, downloaded once. Without them the system still runs,
REM  but punches are recorded without confirming who made them, so this
REM  is not treated as a fatal error if the download fails.
if not exist "data\models\face_recognition_sface_2021dec.onnx" (
  echo   Downloading the face check models. This happens once.
  %VENV_PY% backend\scripts\fetch_face_models.py
  if errorlevel 1 (
    echo.
    echo   The download did not finish. Attendance will still work, but
    echo   punches will not be face-checked until you run this again:
    echo     backend\.venv\Scripts\python.exe backend\scripts\fetch_face_models.py
    echo.
    pause
  )
  echo.
)

REM --- nightly backup of yesterday's work ------------------------------
echo   Backing up before starting...
%VENV_PY% backend\scripts\backup.py --keep 30
echo.

echo   Starting the server.
echo.
echo     On this laptop:  http://127.0.0.1:8080/admin
echo     On the kiosks:   your https address, then /kiosk
echo.
echo   Leave this window open. Press Ctrl+C to stop.
echo.

%VENV_PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --app-dir backend
goto :eof

:failed
echo.
echo   Setup failed. Check the messages above.
pause
exit /b 1
