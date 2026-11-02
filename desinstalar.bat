@echo off
rem Para e remove o arranque automático do primavera-sync (de todas as empresas) e termina o sync e o launcher que estejam a correr.
rem Os ficheiros e os config.ini ficam na pasta.
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -File ""%~dp0desinstalar.ps1""'"
