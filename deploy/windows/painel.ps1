<#
Painel de Controle no Windows.

Clique com o botão direito neste arquivo e escolha "Executar com o PowerShell".
Sem parâmetro, ele instala (ou atualiza) o painel e o deixa ligando sozinho com o Windows.
Pode rodar de novo quantas vezes quiser.

Outros usos, num PowerShell aberto nesta pasta:
  .\painel.ps1 atualizar   baixa a versão mais nova do GitHub e reinstala
  .\painel.ps1 status      mostra se o painel está rodando
  .\painel.ps1 reiniciar   reinicia o painel
  .\painel.ps1 parar       para o painel (ele volta no próximo boot)
  .\painel.ps1 iniciar     liga o painel
  .\painel.ps1 log         mostra o log ao vivo (Ctrl+C para sair)
  .\painel.ps1 remover     tira o painel da inicialização (não apaga nada da pasta)
#>
param(
    [ValidateSet("instalar", "atualizar", "status", "iniciar", "parar", "reiniciar", "log", "remover")]
    [string]$Acao = "instalar"
)

$ErrorActionPreference = "Stop"
$Tarefa = "Painel de Controle"
$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Rodar = Join-Path $PSScriptRoot "rodar.cmd"
$Venv = Join-Path $Raiz ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Config = Join-Path $Raiz "config.toml"
$Log = Join-Path $Raiz "minerador.log"
$Zip = "https://github.com/marconecerqueira/Painel-de-Controle/archive/refs/heads/main.zip"

function Titulo($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)     { Write-Host "   OK  $t" -ForegroundColor Green }
function Aviso($t)  { Write-Host "   !   $t" -ForegroundColor Yellow }
function Pausa      { Write-Host ""; Read-Host "Aperte Enter para fechar" | Out-Null }
function EditarConfig {
    # o Bloco de Notas do Windows 11 não "espera" de verdade: pede o Enter depois de salvar
    Start-Process notepad.exe -ArgumentList "`"$Config`""
    Read-Host "   Salve o config.toml no Bloco de Notas e depois aperte Enter aqui" | Out-Null
}
function Falha($t)  { Write-Host ""; Write-Host "ERRO: $t" -ForegroundColor Red; Pausa; exit 1 }

# ------------------------------------------------------------------ administrador
$eu = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$admin = $eu.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin -and $Acao -notin @("status", "log")) {
    Write-Host "Pedindo permissão de administrador..."
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", $Acao)
    exit
}

# ------------------------------------------------------------------ utilidades
function Porta {
    # lê [server] port do config.toml (padrão 8080)
    $p = 8080
    if (Test-Path $Config) {
        $secao = ""
        foreach ($l in Get-Content $Config -Encoding UTF8) {
            if ($l -match '^\s*\[([^\]]+)\]') { $secao = $Matches[1].Trim() }
            elseif ($secao -eq "server" -and $l -match '^\s*port\s*=\s*(\d+)') { $p = [int]$Matches[1] }
        }
    }
    return $p
}

function VersaoPython($exe) {
    $ErrorActionPreference = "Continue"
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return [version]("$v".Trim()) }
    } catch {}
    return $null
}

function AcharPython {
    # Python 3.11 ou mais novo; prefere o instalado "para todos os usuários"
    $ErrorActionPreference = "Continue"
    $cands = @()
    $cands += Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
              Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try { $cands += "$(& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null)".Trim() } catch {}
    }
    # o "python.exe" de WindowsApps é só um atalho para a Microsoft Store
    $cands += Get-Command python.exe -All -ErrorAction SilentlyContinue |
              Where-Object { $_.Source -notlike "*WindowsApps*" } | ForEach-Object { $_.Source }
    $cands += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
              Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c)) {
            $v = VersaoPython $c
            if ($v -and $v -ge [version]"3.11") { return $c }
        }
    }
    return $null
}

function Tailscale {
    $c = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (Test-Path $c) { return $c }
    $g = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return $null
}

function PainelResponde([int]$segundos) {
    $url = "http://127.0.0.1:$(Porta)/api/status"
    $fim = (Get-Date).AddSeconds($segundos)
    do {
        try {
            Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5 | Out-Null
            return $true
        } catch { Start-Sleep -Seconds 2 }
    } while ((Get-Date) -lt $fim)
    return $false
}

function Parar {
    Stop-ScheduledTask -TaskName $Tarefa -ErrorAction SilentlyContinue
    # o laço do rodar.cmd primeiro (senão ele liga o painel de novo), depois o Python
    Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
        Where-Object { $_.CommandLine -like "*rodar.cmd*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*-m minerador*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

function Iniciar {
    if (-not (Get-ScheduledTask -TaskName $Tarefa -ErrorAction SilentlyContinue)) {
        Falha "O painel ainda não foi instalado. Rode este script sem parâmetro primeiro."
    }
    Start-ScheduledTask -TaskName $Tarefa
    if (PainelResponde 40) { Ok "Painel no ar em http://127.0.0.1:$(Porta)" }
    else { Aviso "O painel não respondeu em 40 s. Veja o fim do log:"; Get-Content $Log -Tail 25 -ErrorAction SilentlyContinue }
}

function Status {
    $t = Get-ScheduledTask -TaskName $Tarefa -ErrorAction SilentlyContinue
    if ($t) { Ok "Tarefa de inicialização instalada (estado: $($t.State))" }
    else    { Aviso "Tarefa de inicialização não instalada" }
    if (PainelResponde 3) { Ok "Painel respondendo em http://127.0.0.1:$(Porta)" }
    else                  { Aviso "Painel não responde em http://127.0.0.1:$(Porta)" }
    $ts = Tailscale
    if ($ts) { Write-Host ""; & $ts serve status }
}

# ------------------------------------------------------------------ instalação
function Instalar {
    Write-Host "Painel de Controle: instalação no Windows" -ForegroundColor Cyan
    Write-Host "Pasta: $Raiz"

    Titulo "1/6  Python"
    $python = AcharPython
    if (-not $python) {
        $wg = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $wg) {
            Falha ("Python 3.11 ou mais novo não encontrado, e o winget não está disponível para instalá-lo.`n" +
                   "Baixe em https://www.python.org/downloads/windows/ e, na instalação, marque `"Add python.exe to PATH`".`n" +
                   "Depois rode este script de novo.")
        }
        Write-Host "   Instalando o Python 3.13 (pode levar alguns minutos)..."
        & $wg.Source install -e --id Python.Python.3.13 --scope machine --silent `
            --accept-package-agreements --accept-source-agreements
        $python = AcharPython
        if (-not $python) { Falha "O Python não ficou disponível. Feche esta janela, abra de novo e rode o script outra vez." }
    }
    Ok "$python ($(VersaoPython $python))"

    Titulo "2/6  Parando o painel (se estiver rodando)"
    Parar
    Ok "parado"

    Titulo "3/6  Ambiente do painel (.venv) e dependências"
    if ((Test-Path $Py) -and -not (VersaoPython $Py)) {
        Aviso ".venv quebrado (pasta movida ou Python trocado): recriando"
        Remove-Item -Recurse -Force $Venv
    }
    if (-not (Test-Path $Py)) {
        & $python -m venv $Venv
        if ($LASTEXITCODE -ne 0) { Falha "Não consegui criar o .venv." }
    }
    & $Py -m pip install --disable-pip-version-check -q --upgrade pip
    & $Py -m pip install --disable-pip-version-check -q $Raiz
    if ($LASTEXITCODE -eq 0) {
        # mesma versão no pyproject: força a troca do código pelo da pasta
        & $Py -m pip install --disable-pip-version-check -q --no-deps --force-reinstall $Raiz
    }
    if ($LASTEXITCODE -ne 0) { Falha "A instalação das dependências falhou (veja as mensagens acima). Confira a internet do PC." }
    Ok "painel e dependências instalados"

    Titulo "4/6  Configuração (config.toml)"
    if (-not (Test-Path $Config)) {
        Copy-Item (Join-Path $Raiz "config.example.toml") $Config
        Aviso "Não havia config.toml: criei um a partir do exemplo."
        Aviso "Ele vai abrir no Bloco de Notas: preencha IPs e endereços e salve."
        EditarConfig
    }
    while ($true) {
        $env:PAINEL_CONFIG = $Config
        $ErrorActionPreference = "Continue"
        $erro = & $Py -c "import os, sys
from minerador.config import ConfigError, load_config
try:
    load_config(os.environ['PAINEL_CONFIG'])
except ConfigError as e:
    print(e); sys.exit(1)" 2>&1
        $codigo = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($codigo -eq 0) { break }
        Aviso "Problema no config.toml: $erro"
        $r = Read-Host "   Abrir o config.toml no Bloco de Notas para corrigir? (S/n)"
        if ($r -match '^[nN]') { Falha "Corrija o config.toml e rode o script de novo." }
        EditarConfig
    }
    Ok "config.toml válido (porta $(Porta))"

    Titulo "5/6  Ligar sozinho com o Windows"
    $acao = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$Rodar`"`"" -WorkingDirectory $Raiz
    $gatilho = New-ScheduledTaskTrigger -AtStartup
    $conta = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Tarefa -Action $acao -Trigger $gatilho -Principal $conta -Settings $ajustes `
        -Description "Painel de Controle dos mineradores (deploy\windows\painel.ps1)" -Force | Out-Null
    Ok "tarefa `"$Tarefa`" criada no Agendador de Tarefas (roda sem ninguém logado)"
    # PC ligado na tomada: nunca suspender nem hibernar
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Ok "Windows configurado para não suspender nem hibernar na tomada"
    Iniciar

    Titulo "6/6  Acesso de fora pela Tailscale"
    $ts = Tailscale
    if (-not $ts) {
        $wg = Get-Command winget.exe -ErrorAction SilentlyContinue
        $r = if ($wg) { Read-Host "   A Tailscale não está instalada. Instalar agora? (S/n)" } else { "n" }
        if ($r -notmatch '^[nN]') {
            & $wg.Source install -e --id Tailscale.Tailscale --silent --accept-package-agreements --accept-source-agreements
            $ts = Tailscale
        }
    }
    if (-not $ts) {
        Aviso "Instale a Tailscale (https://tailscale.com/download/windows) e rode este script de novo."
    } else {
        $ErrorActionPreference = "Continue"
        & $ts status *> $null
        $logado = ($LASTEXITCODE -eq 0)
        if ($logado) {
            & $ts serve --bg (Porta)
            Write-Host ""
            & $ts serve status
        }
        $ErrorActionPreference = "Stop"
        if ($logado) {
            Ok "painel publicado só para os seus dispositivos da Tailscale (endereço https acima)"
        } else {
            Aviso "Entre na Tailscale (ícone perto do relógio > Log in) e rode este script de novo."
        }
        Aviso "Confira no ícone da Tailscale: Preferences > Run unattended (ligado)."
        Aviso "Em login.tailscale.com/admin > Machines > este PC > Disable key expiry."
    }

    Write-Host ""
    Write-Host "Pronto. O painel liga sozinho junto com o Windows, mesmo sem ninguém logado." -ForegroundColor Green
    Write-Host "Log: $Log"
}

function Atualizar {
    # baixa o main do GitHub e copia por cima; config.toml, .venv, bancos e log não vêm no zip
    Titulo "Baixando a versão mais nova do GitHub"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $tmp = Join-Path $env:TEMP "painel-atualizacao"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        Invoke-WebRequest $Zip -OutFile (Join-Path $tmp "main.zip") -UseBasicParsing
        Expand-Archive (Join-Path $tmp "main.zip") -DestinationPath $tmp
    } catch { Falha "Não consegui baixar a atualização: $($_.Exception.Message)" }
    $novo = Get-ChildItem $tmp -Directory | Select-Object -First 1
    robocopy $novo.FullName $Raiz /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Falha "Não consegui copiar os arquivos novos (robocopy $LASTEXITCODE)." }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Ok "arquivos atualizados (config.toml e histórico mantidos)"
    # reinstala com o script novo, que acabou de chegar
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath instalar
    exit
}

switch ($Acao) {
    "instalar"  { Instalar }
    "atualizar" { Atualizar }
    "status"    { Status }
    "iniciar"   { Iniciar }
    "parar"     { Parar; Ok "Painel parado. Ele volta a ligar no próximo boot (ou: .\painel.ps1 iniciar)." }
    "reiniciar" { Parar; Iniciar }
    "remover"   {
        Parar
        Unregister-ScheduledTask -TaskName $Tarefa -Confirm:$false -ErrorAction SilentlyContinue
        Ok "Painel parado e fora da inicialização. A pasta, o config.toml e o histórico continuam aqui."
    }
    "log"       { Get-Content $Log -Tail 40 -Wait }
}
if ($Acao -ne "log") { Pausa }
