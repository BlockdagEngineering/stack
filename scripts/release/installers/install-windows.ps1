#Requires -Version 5.1
param()

$ErrorActionPreference = 'Stop'

$installerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageRoot = Split-Path -Parent $installerDir
Set-Location -Path $packageRoot

$arch = if ($env:BDAG_INSTALL_ARCH) { $env:BDAG_INSTALL_ARCH } else {
    switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
        'X64'   { 'amd64' }
        'Arm64' { 'arm64' }
        default { throw "Unsupported CPU architecture: $([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" }
    }
}
$dockerPlatform = 'linux/amd64'

Write-Host "=== BlockDAG Pool Stack Installer (windows/$arch) ===" -ForegroundColor Cyan
Write-Host ""

if ($arch -eq 'arm64') {
    Write-Host "This release contains linux/amd64 service binaries."
    Write-Host "Docker will run the stack with platform $dockerPlatform; amd64 emulation must be enabled."
    Write-Host ""
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $Hint"
    }
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $text = [System.IO.File]::ReadAllText((Get-Item $Path).FullName)
    $escaped = [regex]::Escape($Key)
    $line = "$Key=$Value"
    if ($text -match "(?m)^$escaped=") {
        $text = [regex]::Replace($text, "(?m)^$escaped=.*", { param($match) $line })
    } else {
        $text = $text.TrimEnd() + "`n$line`n"
    }
    $text = $text -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $Path), $text, [System.Text.Encoding]::UTF8)
}

Require-Command docker "Install Docker Desktop, then re-run this installer."
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required. Install/update Docker Desktop."
}

if (-not (Test-Path .env.example) -or -not (Test-Path node.conf.example) -or -not (Test-Path docker-compose.yml)) {
    throw "Run this installer from the extracted pool-stack-docker release folder."
}

$snapshotPath = 'docker/no-snapshot.marker'
if (Test-Path latest.bdsnap) {
    Write-Host "Found snapshot: latest.bdsnap"
    $snapshotPath = './latest.bdsnap'
} else {
    $snap = Get-ChildItem -File -Filter '*.bdsnap' | Select-Object -First 1
    if ($snap) {
        Write-Host "Found snapshot: $($snap.Name)"
        Move-Item -Path $snap.FullName -Destination (Join-Path (Get-Location) 'latest.bdsnap') -Force
        $snapshotPath = './latest.bdsnap'
    } else {
        Write-Host "No local snapshot found. Downloading latest.bdsnap from snapshot.bdagdev.xyz." -ForegroundColor Yellow
        try {
            $ProgressPreference = 'Continue'
            Invoke-WebRequest -Uri 'https://snapshot.bdagdev.xyz/latest.bdsnap' -OutFile 'latest.bdsnap' -UseBasicParsing
            Write-Host "Snapshot downloaded."
            $snapshotPath = './latest.bdsnap'
        } catch {
            Write-Host "Warning: snapshot download failed. The node will sync from genesis/P2P." -ForegroundColor Yellow
            Remove-Item -Path 'latest.bdsnap' -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ""
Write-Host "=== Configuration ===" -ForegroundColor Cyan
Write-Host ""

function Read-PlainPassword([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

do {
    $pgPassword = Read-PlainPassword "Postgres password (required, hidden)"
    if (-not $pgPassword) { Write-Host "  Password cannot be empty. Try again." }
} while (-not $pgPassword)

$miningAddr = Read-Host "Mining/earnings wallet address (0x...)"
$poolPrivateKey = Read-PlainPassword "Pool operator private key (optional, hidden; press Enter to skip)"

Copy-Item .env.example .env -Force
Set-EnvValue .env POSTGRES_PASSWORD $pgPassword
Set-EnvValue .env MINING_POOL_ADDRESS $miningAddr
Set-EnvValue .env DOCKER_PLATFORM $dockerPlatform
Set-EnvValue .env SNAPSHOT_PATH $snapshotPath
if ($poolPrivateKey) {
    Set-EnvValue .env POOL_PRIVATE_KEY $poolPrivateKey
}

Copy-Item node.conf.example node.conf -Force
$nodeText = [System.IO.File]::ReadAllText((Get-Item node.conf).FullName)
if ($nodeText -match '(?m)^miningaddr=') {
    $nodeText = [regex]::Replace($nodeText, '(?m)^miningaddr=.*', "miningaddr=$miningAddr")
} else {
    $nodeText = $nodeText.TrimEnd() + "`nminingaddr=$miningAddr`n"
}

Write-Host ""
Write-Host "Detecting external IP address..."
try {
    $externalIp = (Invoke-WebRequest -Uri 'https://api.ipify.org' -UseBasicParsing -TimeoutSec 5).Content.Trim()
} catch {
    try {
        $externalIp = (Invoke-WebRequest -Uri 'https://ifconfig.me' -UseBasicParsing -TimeoutSec 5).Content.Trim()
    } catch {
        $externalIp = ''
    }
}

if ($externalIp) {
    Write-Host "  Detected: $externalIp"
    if ($nodeText -match '(?m)^# externalip=') {
        $nodeText = [regex]::Replace($nodeText, '(?m)^# externalip=.*', "externalip=$externalIp")
    } elseif ($nodeText -match '(?m)^externalip=') {
        $nodeText = [regex]::Replace($nodeText, '(?m)^externalip=.*', "externalip=$externalIp")
    } else {
        $nodeText = $nodeText.TrimEnd() + "`nexternalip=$externalIp`n"
    }
} else {
    Write-Host "  Warning: could not detect external IP. Node will operate outbound-only." -ForegroundColor Yellow
}

$nodeText = $nodeText -replace "`r`n", "`n"
[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'node.conf'), $nodeText, [System.Text.Encoding]::UTF8)

New-Item -ItemType Directory -Force -Path 'dashboard\logs' | Out-Null
$env:DOCKER_DEFAULT_PLATFORM = $dockerPlatform

Write-Host ""
Write-Host "=== Building Docker images ($dockerPlatform) ===" -ForegroundColor Cyan
& docker compose build
if ($LASTEXITCODE -ne 0) { throw "docker compose build failed." }

Write-Host ""
Write-Host "=== Starting services ===" -ForegroundColor Cyan
& docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed." }

Write-Host ""
Write-Host "=================================================" -ForegroundColor Green
Write-Host "  BlockDAG Pool Stack is running." -ForegroundColor Green
Write-Host "=================================================" -ForegroundColor Green
Write-Host "  Dashboard:  http://localhost:9280"
Write-Host "  Stratum:    stratum+tcp://localhost:3334"
Write-Host "  EVM RPC:    http://localhost:18545"
Write-Host ""
Write-Host "  View logs:  docker compose logs -f"
Write-Host "  Stop:       docker compose down"
Write-Host "=================================================" -ForegroundColor Green

Start-Process powershell -WorkingDirectory $packageRoot
