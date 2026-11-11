@echo off
rem Test start. Pick the order source:
rem   1 = real Mobius : same as start_all.bat, and opens the test page (/test) in the browser
rem   2 = fake Mobius : for when Mobius is down - starts fake_order.py (fake Mobius + order menu); see start_all.ps1 -Fake
rem Direct: start_test.bat real   /   start_test.bat fake
set MODE=%1
if /i "%MODE%"=="real" goto real
if /i "%MODE%"=="fake" goto fake
echo.
echo  Which order source?
echo    1) Real Mobius   (platform orders, opens /test page)
echo    2) Fake Mobius   (Mobius is down - send test orders from the fake_order tab)
echo.
choice /c 12 /n /m "Select 1 or 2: "
if errorlevel 2 goto fake
:real
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" -Test
goto :eof
:fake
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" -Test -Fake
