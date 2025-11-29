@echo off
title Hospital IoT Network Twin - AUTO START
echo ==========================================
echo   🏥 Starting Hospital IoT Network Twin
echo ==========================================
echo.

REM --- Move to project folder ---
cd %~dp0

REM --- Activate Virtual Environment ---
echo 🔄 Activating virtual environment...
call venv\Scripts\activate

REM --- Start Backend Server ---
echo 🚀 Starting Backend Server...
start "BACKEND" cmd /k "cd Network && python network_server.py"

REM --- Wait for backend to start ---
echo ⏳ Waiting 3 seconds for backend to initialize...
timeout /t 3 >nul

REM --- Start Frontend React UI ---
echo 🌐 Starting React Frontend...
start "FRONTEND" cmd /k "cd Frontend\hospital-sim-frontend && npm start"

echo.
echo ==========================================
echo ✔️ All components started successfully!
echo Open your browser after React loads.
echo ==========================================
echo.
pause
