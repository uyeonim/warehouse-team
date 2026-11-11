@echo off
rem Stop the robot programs (gateway, board main, fake Mobius) with Ctrl+C - see stop_all.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_all.ps1"
