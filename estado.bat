@echo off
rem Estado do primavera-sync de cada empresa: verifica a ligação e mostra o que foi enviado e os erros.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0estado.ps1"
