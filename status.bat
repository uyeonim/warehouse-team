@echo off
rem Open the status page in the browser (starts it first if not running) - see start_all.ps1 -StatusOnly
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" -StatusOnly
