#Requires -Version 5.1
param()

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$envFile = '.env.devnet'
$composeArgs = @('--env-file', $envFile, '-f', 'docker-compose.yml', '-f', 'docker-compose.devnet.yml')

function Get-PayloadValue {
    param([Parameter(Mandatory = $true)][string] $Key)
    $path = 'release-payload.env'
    if (-not (Test-Path $path)) {
        return $null
    }
    foreach ($line in Get-Content -Path $path) {
        if ($line -match "^$([regex]::Escape($Key))=(.*)$") {
            return $Matches[1]
        }
    }
    return $null
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Key,
        [Parameter(Mandatory = $true)][string] $Value
    )
    $line = "$Key=$Value"
    if (Test-Path $Path) {
        $lines = @(Get-Content -Path $Path)
    } else {
        $lines = @()
    }
    $found = $false
    $updated = foreach ($existing in $lines) {
        if ($existing -match "^$([regex]::Escape($Key))=") {
            $found = $true
            $line
        } else {
            $existing
        }
    }
    if (-not $found) {
        $updated += $line
    }
    Set-Content -Path $Path -Value $updated -Encoding utf8
}

if (-not (Test-Path '.env.devnet.example')) {
    throw 'Missing .env.devnet.example in release payload'
}

if (-not (Test-Path $envFile)) {
    Copy-Item '.env.devnet.example' $envFile
    Write-Host "Created $envFile from .env.devnet.example"
} else {
    Write-Host "Using existing $envFile"
}

$dockerPlatform = Get-PayloadValue 'DOCKER_PLATFORM'
if (-not $dockerPlatform) {
    $arch = Get-PayloadValue 'BDAG_RELEASE_PAYLOAD_ARCH'
    if ($arch) {
        $dockerPlatform = "linux/$arch"
    } else {
        $dockerPlatform = 'linux/amd64'
    }
}

Set-EnvValue $envFile 'DOCKERFILE' 'dockerfile'
Set-EnvValue $envFile 'DOCKER_PLATFORM' $dockerPlatform
Set-EnvValue $envFile 'STACK_SRC_CONTEXT' '.'
Set-EnvValue $envFile 'BLOCKDAG_CORECHAIN_CONTEXT' '.'
Set-EnvValue $envFile 'POOL_SRC_CONTEXT' '.'
Set-EnvValue $envFile 'COLLECTOR_SRC_CONTEXT' './collector'
Set-EnvValue $envFile 'DASHBOARD_SRC_CONTEXT' '.'

if ($env:BDAG_DEVNET_INSTALL_TEST_WRITE_ENV_ONLY -eq '1') {
    Write-Host "Devnet installer smoke test wrote $envFile"
    exit 0
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    throw 'Docker is required. Install Docker Desktop or Docker Engine, then re-run this installer.'
}

& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose v2 is required. Install or update Docker, then re-run this installer.'
}

& docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker is installed, but this user cannot reach the Docker daemon.'
}

& docker compose @composeArgs up -d --build pool-db node pool collector dashboard cpu-miner
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& docker compose @composeArgs ps

Write-Host ''
Write-Host 'Devnet is starting.'
Write-Host ''
Write-Host 'Endpoints:'
Write-Host '  EVM HTTP RPC:   http://127.0.0.1:18545'
Write-Host '  EVM WebSocket:  ws://127.0.0.1:18546'
Write-Host '  Native DAG RPC: http://127.0.0.1:38131'
Write-Host '  Pool Stratum:   stratum+tcp://127.0.0.1:3334'
Write-Host '  Pool metrics:   http://127.0.0.1:9090/metrics'
Write-Host '  Collector API:  http://127.0.0.1:9280/api/status'
Write-Host '  Dashboard:      http://127.0.0.1:8088'
Write-Host ''
Write-Host 'Status:'
Write-Host '  docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml ps'
Write-Host '  docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml logs -f node pool cpu-miner'
