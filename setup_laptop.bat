@echo off
rem One-time laptop setup - asks for admin rights (see setup\setup_laptop.ps1)
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0setup\setup_laptop.ps1\"'"
