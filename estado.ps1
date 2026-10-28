# Estado do primavera-sync de cada empresa instalada neste servidor (ver instalacao.json).
$Pasta = Split-Path -Parent $PSCommandPath
Set-Location $Pasta
$json = Join-Path $Pasta "instalacao.json"
if (Test-Path $json) { $empresas = @(Get-Content $json -Raw -Encoding UTF8 | ConvertFrom-Json) }
else { $empresas = @([pscustomobject]@{ nome = "(instalação antiga)"; tarefa = "PrimaveraLonntra"; config = "config.ini"; log = "primavera-sync.log" }) }
foreach ($e in $empresas) {
    Write-Host ""; Write-Host "######## $($e.nome)" -ForegroundColor Cyan
    Write-Host "== Tarefa"
    Get-ScheduledTask -TaskName $e.tarefa -ErrorAction SilentlyContinue | Select-Object TaskName, State | Format-Table -HideTableHeaders
    Write-Host "== Verificacao"
    python -m primavera_sync.sincronizador $e.config --verificar
    Write-Host ""; Write-Host "== Enviados e erros"
    python -m primavera_sync.sincronizador $e.config --estado
    Write-Host ""; Write-Host "== Ultimas linhas do log"
    if (Test-Path $e.log) { Get-Content $e.log -Tail 15 -Encoding UTF8 }
}
Write-Host ""
Read-Host "Enter para fechar"
