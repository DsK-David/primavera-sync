# Para e remove as tarefas PrimaveraLonntra* e termina o sync e o launcher que estejam a correr.
Get-ScheduledTask -TaskName "PrimaveraLonntra*" -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "Removido $($_.TaskName)"
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "primavera_sync|launcher\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Terminado o processo $($_.ProcessId)" }
Start-Sleep 3
