@echo off
rem Para e remove o arranque automático do primavera-sync. Os ficheiros e o config.ini ficam na pasta.
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command Stop-ScheduledTask -TaskName PrimaveraLonntra -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName PrimaveraLonntra -Confirm:$false; Write-Host Removido; Start-Sleep 3'"
