@echo off
rem Instala e configura o primavera-sync neste servidor (pede permissões de administrador).
rem Pôr o cliente.ini (endereço e chave da Lonntra) na mesma pasta antes de correr.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1"
