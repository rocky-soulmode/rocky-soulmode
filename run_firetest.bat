@echo off
set GOOGLE_APPLICATION_CREDENTIALS=C:\Users\kawshik\Documents\Chatgpt\chatgpt-kaapav-472214-894825d83fbb.json
REM --- Start Rocky Soulmode API on port 8090 ---
start "" "C:\Users\kawshik\AppData\Local\Programs\Python\Python313\python.exe" -m uvicorn rocky_soulmode_api:app --host 127.0.0.1 --port 8090 --reload

REM --- Give server time to start ---
timeout /t 5 /nobreak >nul

REM --- Test inserting memory into Firestore ---
curl -X POST http://127.0.0.1:8090/remember -H "Content-Type: application/json" -d "{\"account\":\"kaapav\",\"key\":\"firestore_debug\",\"value\":\"hello_firestore\"}"

pause
