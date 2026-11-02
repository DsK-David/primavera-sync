# Instalador do primavera-sync (Primavera ERP Evolution -> Lonntra). Corre-se com o instalar.bat (como administrador).
#
#  1. copia o sync para C:\primavera-sync (o config.ini e o estado.db que lá estejam ficam)
#  2. instala o Python 3.12 (se faltar) e o pyodbc
#  3. corre o assistente (python -m primavera_sync.instalar): descobre o Primavera, cria o login de leitura,
#     propõe os mapas, escreve o config.ini e verifica tudo
#  4. cria uma tarefa por empresa, que corre o launcher.py (arranca o sync em versoes\<versão>) («PrimaveraLonntra» para a instalação antiga, «PrimaveraLonntra-<empresa>» para as
#     outras; arrancam com o Windows, como SYSTEM, e reiniciam se pararem) e arranca-as
#  5. espera pelo primeiro ciclo de cada uma e mostra o resultado; tudo fica em relatorio-instalacao.txt
param([string]$Destino = "C:\primavera-sync")

$ErrorActionPreference = "Stop"
$Prefixo = "PrimaveraLonntra"
$PythonUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"

function Titulo($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "  OK   $t" -ForegroundColor Green }
function Aviso($t) { Write-Host "  !!   $t" -ForegroundColor Yellow }
function Falha($t) { Write-Host "  ERRO $t" -ForegroundColor Red; Read-Host "Enter para sair"; exit 1 }

$eu = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $eu.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Destino `"$Destino`""
    exit
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Origem = Split-Path -Parent $PSCommandPath

# ---------------------------------------------------------------- 1. ficheiros
Titulo "Ficheiros"
New-Item -ItemType Directory -Force -Path $Destino, "$Destino\logs" | Out-Null
if ((Resolve-Path $Origem).Path -ne (Resolve-Path $Destino).Path) {
    Copy-Item -Recurse -Force "$Origem\primavera_sync" $Destino
    foreach ($f in "config.example.ini", "instalar.bat", "instalar.ps1", "launcher.py", "chaves.json", "estado.bat", "estado.ps1", "desinstalar.bat", "desinstalar.ps1", "cliente.ini") {
        if (Test-Path "$Origem\$f") { Copy-Item -Force "$Origem\$f" $Destino }
    }
}
Get-ChildItem "$Destino\primavera_sync" -Recurse -Filter "__pycache__" -Directory | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# a pasta tem a chave e a palavra-passe: só administradores e o sistema
$adm = (New-Object Security.Principal.SecurityIdentifier "S-1-5-32-544").Translate([Security.Principal.NTAccount]).Value
icacls $Destino /inheritance:r /grant:r "${adm}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
Ok "sync em $Destino (pasta só para administradores)"
if (-not (Test-Path "$Destino\cliente.ini")) { Aviso "sem cliente.ini: o assistente pergunta o endereço e a chave da Lonntra" }

# ---------------------------------------------------------------- 2. Python e pyodbc
Titulo "Python"
function Achar-Python {
    foreach ($c in @("$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python311\python.exe",
                     "$env:ProgramFiles\Python310\python.exe")) { if (Test-Path $c) { return $c } }
    $p = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch "WindowsApps" } | Select-Object -First 1
    if ($p) { return $p.Source }
    return $null
}
$Py = Achar-Python
if ($Py) {
    $versao = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ([version]$versao -lt [version]"3.9") { $Py = $null; Aviso "Python $versao é antigo" }
}
if (-not $Py) {
    Write-Host "  a descarregar e instalar o Python 3.12 (para todos os utilizadores)..."
    $exe = "$env:TEMP\python-instalador.exe"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $exe -UseBasicParsing
    Start-Process $exe -Wait -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
    $Py = Achar-Python
    if (-not $Py) { Falha "o Python não ficou instalado. Instale-o de python.org (para todos os utilizadores) e corra outra vez." }
}
Ok "Python: $Py"
& $Py -m pip install --upgrade --quiet --disable-pip-version-check pyodbc
if ($LASTEXITCODE -ne 0) { Falha "não consegui instalar o pyodbc (precisa de internet)." }
Ok "pyodbc instalado"
# «cryptography» confere a assinatura das versões novas do sync (atualização à distância). Sem ela o sync funciona, mas não se atualiza sozinho.
& $Py -m pip install --upgrade --quiet --disable-pip-version-check --only-binary :all: cryptography
if ($LASTEXITCODE -ne 0) { Aviso "não consegui instalar a biblioteca «cryptography»: o sync funciona, mas não se atualiza à distância" } else { Ok "cryptography instalada (atualização à distância)" }
$drivers = (Get-OdbcDriver -Platform "64-bit" | Where-Object { $_.Name -match "SQL Server" }).Name
if (-not $drivers) { Falha "não há nenhum driver ODBC do SQL Server. Instale o «ODBC Driver 18 for SQL Server» da Microsoft." }
Ok ("drivers ODBC: " + ($drivers -join ", "))

# ---------------------------------------------------------------- 3. assistente
$antes = Get-ScheduledTask -TaskName "$Prefixo*" -ErrorAction SilentlyContinue
$aCorrer = @($antes | Where-Object { $_.State -eq "Running" } | ForEach-Object { $_.TaskName })
if ($antes) { $antes | Stop-ScheduledTask -ErrorAction SilentlyContinue; Aviso "as tarefas que já existiam foram paradas para reconfigurar" }
# parar uma tarefa pode deixar o sync órfão a correr (instalações sem launcher): terminam-se, para não ficarem dois
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "primavera_sync|launcher\.py" -and $_.CommandLine -notmatch "primavera_sync\.instalar" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item "$Destino\instalacao.json" -ErrorAction SilentlyContinue
Push-Location $Destino
& $Py -m primavera_sync.instalar
$resultado = $LASTEXITCODE
Pop-Location
if ($resultado -eq 1 -or -not (Test-Path "$Destino\instalacao.json")) {
    # a configuração não ficou escrita: as tarefas que já corriam voltam a correr, para o sync não ficar parado
    foreach ($nome in $aCorrer) { Start-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue }
    if ($aCorrer) { Aviso "as tarefas que já corriam foram retomadas: $($aCorrer -join ', ')" }
    Falha "a configuração não ficou completa (ver acima e $Destino\relatorio-instalacao.txt). Corrija e corra o instalar.bat outra vez."
}
if ($resultado -ne 0) { Aviso "alguma empresa teve erros na verificação ou no teste de envio (ver acima). As tarefas arrancam na mesma: o sync repete e avisa a Lonntra. Confirme em $Destino\relatorio-instalacao.txt." }
$empresas = @(Get-Content "$Destino\instalacao.json" -Raw -Encoding UTF8 | ConvertFrom-Json)

# ---------------------------------------------------------------- 4. arranque automático (uma tarefa por empresa)
Titulo "Arranque automático"
$gatilho = New-ScheduledTaskTrigger -AtStartup
$gatilho.Delay = "PT2M"   # dá tempo ao SQL Server de arrancar
$definicoes = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$quem = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
foreach ($e in $empresas) {
    # a tarefa corre o launcher, que arranca o sync na versão certa, recua nos reinícios e reverte versões que falham
    $acao = New-ScheduledTaskAction -Execute $Py -Argument "launcher.py `"$($e.config)`"" -WorkingDirectory $Destino
    & $Py "$Destino\launcher.py" --autoteste "$Destino\$($e.config)"
    if ($LASTEXITCODE -ne 0) { Aviso "o launcher não conseguiu arrancar o sync de $($e.nome) (ver acima); a tarefa fica criada e o launcher avisa a Lonntra" }
    Register-ScheduledTask -TaskName $e.tarefa -Action $acao -Trigger $gatilho -Settings $definicoes -Principal $quem `
        -Description "primavera-sync ($($e.nome)): envia documentos, artigos e stock do Primavera para a Lonntra (só leitura)." -Force | Out-Null
    Start-ScheduledTask -TaskName $e.tarefa
    Ok "tarefa «$($e.tarefa)» ($($e.nome)): arranca com o Windows, como SYSTEM, e volta a arrancar se parar"
}
# tarefas de empresas que já não estão na instalação (ex.: PrimaveraLonntra de antes, agora com outro nome)
Get-ScheduledTask -TaskName "$Prefixo*" -ErrorAction SilentlyContinue | Where-Object { $empresas.tarefa -notcontains $_.TaskName } | ForEach-Object {
    Aviso "a tarefa antiga «$($_.TaskName)» não está na instalação e foi removida"
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
}

# ---------------------------------------------------------------- 5. primeiro ciclo de cada empresa
Titulo "Primeiro ciclo"
$linhasRel = @()
foreach ($e in $empresas) {
    Write-Host "  -- $($e.nome)"
    $log = Join-Path $Destino $e.log
    $visto = $false
    for ($i = 0; $i -lt 36 -and -not $visto; $i++) {
        Start-Sleep -Seconds 5
        if (Test-Path $log) {
            $fim = Get-Content $log -Tail 40 -Encoding UTF8
            if ($fim -match "Ciclo terminado|Nada de novo|ERROR") { $visto = $true }
        }
    }
    $estado = (Get-ScheduledTask -TaskName $e.tarefa).State
    if (-not $visto) {
        Aviso "ao fim de 3 minutos ainda não há um ciclo completo no log (a tarefa está: $estado). Pode estar a enviar muitos dados: ver $log"
    } elseif ($fim -match "ERROR") {
        Aviso "o primeiro ciclo teve erros (a tarefa está: $estado). Últimas linhas:"
        $fim | Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" }
    } else {
        Ok "a funcionar (tarefa: $estado). Últimas linhas do log:"
        $fim | Select-Object -Last 6 | ForEach-Object { Write-Host "    $_" }
    }
    $linhasRel += "  tarefa $($e.tarefa) ($estado), empresa $($e.nome), log: $log"
}

$rel = "$Destino\relatorio-instalacao.txt"
Add-Content -Path $rel -Encoding UTF8 -Value ("`n== Arranque automático`n  Python $Py`n" + ($linhasRel -join "`n"))
Write-Host ""
Write-Host "Instalação terminada. Relatório: $rel" -ForegroundColor Cyan
Write-Host "Na Lonntra: php yii integracao/estado mostra o último contacto de cada empresa."
Write-Host "Para ver o estado mais tarde: estado.bat (nesta pasta)."
Read-Host "Enter para fechar"
