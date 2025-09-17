@echo off
setlocal

echo ==================================================
echo 🚀 Starting Rocky Soulmode API (Firestore Backend)...
echo ==================================================

:: Set Google credentials for Firestore
set GOOGLE_APPLICATION_CREDENTIALS=C:\Users\kawshik\Documents\Chatgpt\chatgpt-kaapav-472214-894825d83fbb.json

:: Enable FastAPI server + background worker
set ROCKY_ALLOW_SERVER=1
set ROCKY_AUTONOMOUS=1

:: Go to project directory
cd C:\Users\kawshik\Documents\Chatgpt

:: Check if port 8000 is already in use
echo 🔍 Checking if port 8000 is already in use...
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo ⚠️ Port 8000 already in use. Maybe Rocky Soulmode API is already running.
    pause
    exit /b
)

:: Start FastAPI server in background (logs go to uvicorn.log)
echo 🟢 Launching backend server (Firestore mode)...
start "" /min C:\Users\kawshik\AppData\Local\Programs\Python\Python313\python.exe -m uvicorn rocky_soulmode_api:app --reload --port 8000 > uvicorn.log 2>&1

:: Wait a bit for the server to start
echo ⏳ Waiting for server to start...
timeout /t 6 /nobreak >nul

:: Open Swagger UI in default browser
echo 🟢 Opening API docs in browser...
start http://127.0.0.1:8000/docs

:: Open Chat Memory UI in Chrome
echo 🟢 Opening Chat Memory UI in Chrome...
start "" "chrome.exe" --new-window --app="http://127.0.0.1:8000/chat_ui.html" --class="ChatMemory"

echo --------------------------------------------------
echo ✅ Rocky Soulmode API is running on port 8000
echo 🔍 Logs saved to uvicorn.log
echo --------------------------------------------------
pause
