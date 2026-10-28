@echo off
rem Para e remove o arranque automático do primavera-sync (de todas as empresas). Os ficheiros e os config.ini ficam na pasta.
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command Get-ScheduledTask -TaskName PrimaveraLonntra* | ForEach-Object { Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false; Write-Host Removido $_.TaskName }; Start-Sleep 3'"
