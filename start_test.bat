@echo off
rem Same as start_all.bat, and also opens the test page (/test) in the browser - see start_all.ps1 -Test
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" -Test
