<#
.SYNOPSIS
    Instalador e inicializador transparente do MiniDesk (Host) para suporte remoto.
.DESCRIPTION
    Este script baixa os arquivos do MiniDesk do repositorio GitHub,
    configura um ambiente virtual Python isolado e inicia o host de forma visivel.
    O cliente sempre vera o ID, a senha temporaria e precisara aprovar manualmente a conexao.
#>

param(
    [string]$RepoBaseUrl = "https://raw.githubusercontent.com/guixiss/MiniDesk/main",
    # Se voce tiver um servidor fixo (VPS ou dominio proprio), coloque aqui. Ex: "wss://suporte.seudominio.com"
    # Se usar tunel temporario (cloudflared/ngrok), deixe vazio "" para o script pedir na hora.
    [string]$ServerUrl   = ""
)

$ErrorActionPreference = "Stop"

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "       MiniDesk - Assistente de Suporte Remoto       " -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Este script vai:"
Write-Host " 1. Verificar se o Python esta instalado (ou instalar via winget)."
Write-Host " 2. Baixar 'host.py' e 'requirements.txt' para: $env:USERPROFILE\MiniDesk"
Write-Host " 3. Instalar as dependencias em um ambiente isolado (.venv)."
Write-Host " 4. Iniciar o MiniDesk exibindo seu ID e uma senha temporaria."
Write-Host ""
Write-Host "Aviso: Nenhuma tarefa em segundo plano sera criada. Ao fechar esta janela, o acesso e encerrado." -ForegroundColor Yellow
Write-Host ""

$confirm = Read-Host "Deseja continuar? [S/N]"
if ($confirm -notin @("S", "s", "sim", "SIM")) {
    Write-Host "Operacao cancelada pelo usuario." -ForegroundColor Red
    exit 0
}

# Validar ou solicitar o endereco do servidor (wss://)
if ([string]::IsNullOrWhiteSpace($ServerUrl)) {
    Write-Host ""
    $ServerUrl = (Read-Host "Cole o endereco do servidor informado pelo tecnico (ex: https://xxx.trycloudflare.com)").Trim()
}

if ([string]::IsNullOrWhiteSpace($ServerUrl)) {
    Write-Host "Erro: Endereco do servidor nao informado." -ForegroundColor Red
    exit 1
}

# Converte automaticamente https:// para wss:// (facilita quando o tecnico cola o link do cloudflared/ngrok)
if ($ServerUrl -match "^https://") {
    $ServerUrl = $ServerUrl -replace "^https://", "wss://"
} elseif ($ServerUrl -notmatch "^wss?://") {
    $ServerUrl = "wss://$ServerUrl"
}

# 1. Verificar se o Python esta disponivel
$pythonCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCmd = "py"
}

if (-not $pythonCmd) {
    Write-Host "`n[1/4] Python nao encontrado. Tentando instalar via winget..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        Write-Host "`nPython instalado! Feche e abra o PowerShell novamente para atualizar o PATH e rode o script outra vez." -ForegroundColor Green
        exit 0
    } else {
        Write-Host "Erro: Python nao encontrado e o 'winget' nao esta disponivel." -ForegroundColor Red
        Write-Host "Instale o Python manualmente em https://www.python.org/downloads/ (marque 'Add Python to PATH')."
        exit 1
    }
} else {
    Write-Host "`n[1/4] Python encontrado: $pythonCmd" -ForegroundColor Green
}

# 2. Criar pasta visivel e baixar os arquivos do repositorio
$installDir = Join-Path $env:USERPROFILE "MiniDesk"
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir | Out-Null
}

Write-Host "[2/4] Baixando arquivos do repositorio..." -ForegroundColor Cyan
$files = @("host.py", "requirements.txt")
foreach ($file in $files) {
    $url  = "$RepoBaseUrl/$file"
    $dest = Join-Path $installDir $file
    Write-Host "  -> Baixando $file..."
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

# 3. Criar ambiente virtual (.venv) e instalar dependencias
$venvDir    = Join-Path $installDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[3/4] Criando ambiente virtual Python..." -ForegroundColor Cyan
    & $pythonCmd -m venv $venvDir
}

Write-Host "[3/4] Instalando dependencias (websockets, mss, Pillow, pynput)..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $installDir "requirements.txt") --quiet

# 4. Executar o host.py de forma visivel e com aprovacao manual obrigatoria
Write-Host "`n[4/4] Iniciando MiniDesk em $ServerUrl ..." -ForegroundColor Green
Write-Host "Informe o ID e a SENHA abaixo ao tecnico e confirme com 's' quando ele solicitar acesso.`n" -ForegroundColor Yellow

Set-Location $installDir
& $venvPython host.py --server $ServerUrl
