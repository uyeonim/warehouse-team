@echo off
rem Rehearsal: robot programs fake the motion, reports are real (see start_all.ps1 -Rehearsal)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" -Rehearsal
