@echo off
rem Estado do primavera-sync: verifica a ligação e mostra o que foi enviado e os erros.
cd /d "%~dp0"
echo == Tarefa
powershell -NoProfile -Command "Get-ScheduledTask -TaskName PrimaveraLonntra -ErrorAction SilentlyContinue | Select-Object TaskName,State | Format-Table -HideTableHeaders"
echo == Verificacao
python -m primavera_sync.sincronizador config.ini --verificar
echo.
echo == Enviados e erros
python -m primavera_sync.sincronizador config.ini --estado
echo.
echo == Ultimas linhas do log
powershell -NoProfile -Command "Get-Content primavera-sync.log -Tail 15 -Encoding UTF8"
echo.
pause
