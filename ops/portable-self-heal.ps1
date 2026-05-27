param(
    [switch]$Loop,
    [switch]$Once
)

# Portable compose self-healing for Windows release installs. This script only
# brings configured Docker Compose services back up; it never replaces chain
# data, deletes volumes, or restores snapshots.

$ErrorActionPreference = 'Continue'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = if ($env:BDAG_PROJECT_ROOT) { $env:BDAG_PROJECT_ROOT } else { Split-Path -Parent $scriptDir }
$envFile = if ($env:BDAG_ENV_FILE) { $env:BDAG_ENV_FILE } else { Join-Path $projectRoot '.env' }
$composeFile = if ($env:BDAG_COMPOSE_FILE) { $env:BDAG_COMPOSE_FILE } else { Join-Path $projectRoot 'docker-compose.yml' }
$runtimeDir = if ($env:BDAG_RUNTIME_DIR) { $env:BDAG_RUNTIME_DIR } else { Join-Path $projectRoot 'ops\runtime' }
$logFile = if ($env:BDAG_SELF_HEAL_LOG) { $env:BDAG_SELF_HEAL_LOG } else { Join-Path $runtimeDir 'logs\portable-self-heal.log' }
$interval = if ($env:BDAG_SELF_HEAL_INTERVAL) { [int]$env:BDAG_SELF_HEAL_INTERVAL } else { 60 }
$stateFile = if ($env:BDAG_SYNC_COORDINATOR_STATE_FILE) { $env:BDAG_SYNC_COORDINATOR_STATE_FILE } else { Join-Path $runtimeDir 'sync-coordinator-state.json' }
$lockFile = Join-Path $runtimeDir 'portable-self-heal.lock'

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null

function Write-SelfHealLog([string]$Message) {
    $line = "[{0}] portable-self-heal: {1}" -f (Get-Date).ToString('o'), $Message
    Add-Content -Path $logFile -Value $line
    Write-Host $line
}

function Get-EnvFileValue([string]$Key) {
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in [System.IO.File]::ReadLines($envFile)) {
        if ($line -match "^\s*#") { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $name = $line.Substring(0, $idx).Trim()
        if ($name -ne $Key) { continue }
        return $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
    }
    return $null
}

function Invoke-Compose([string[]]$Args) {
    $base = @('compose')
    if (Test-Path $envFile) {
        $base += @('--env-file', $envFile)
    }
    $base += @('-f', $composeFile)
    $output = & docker @base @Args 2>&1
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($output -join [Environment]::NewLine)
    }
}

function Get-AvailableServices {
    $result = Invoke-Compose @('config', '--services')
    if ($result.ExitCode -ne 0) { return @() }
    return @($result.Output -split "\r?\n" | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
}

function Get-ConfiguredServiceCsv {
    if ($env:BDAG_SELF_HEAL_SERVICES) { return $env:BDAG_SELF_HEAL_SERVICES }
    if ($env:BDAG_STACK_SERVICES) { return $env:BDAG_STACK_SERVICES }
    $value = Get-EnvFileValue 'BDAG_SELF_HEAL_SERVICES'
    if ($value) { return $value }
    return (Get-EnvFileValue 'BDAG_STACK_SERVICES')
}

function Get-PlannedPausedFollower {
    if (-not (Test-Path $stateFile)) { return '' }
    try {
        $state = Get-Content -Raw -Path $stateFile | ConvertFrom-Json
        if ($state.mode -eq 'leader_catchup' -and $state.paused_follower) {
            return [string]$state.paused_follower
        }
    } catch {
        return ''
    }
    return ''
}

function Get-ServiceList {
    $available = @(Get-AvailableServices)
    if ($available.Count -eq 0) { return @() }

    $configured = Get-ConfiguredServiceCsv
    if (-not $configured) {
        $configured = ($available -join ',')
    }

    $paused = Get-PlannedPausedFollower
    $selected = New-Object System.Collections.Generic.List[string]
    foreach ($item in ($configured -split '[,\s]+' | Where-Object { $_ })) {
        if ($item -eq $paused) { continue }
        if ($available -contains $item) {
            $selected.Add($item)
        }
    }

    if ($selected.Count -eq 0) {
        foreach ($item in $available) {
            if ($item -ne $paused) {
                $selected.Add($item)
            }
        }
    }
    return @($selected.ToArray())
}

function Invoke-SelfHealOnce {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-SelfHealLog 'Docker is not reachable; scheduler will retry'
        return
    }
    if (-not (Test-Path $composeFile)) {
        Write-SelfHealLog "compose file missing: $composeFile"
        return
    }

    $services = @(Get-ServiceList)
    if ($services.Count -eq 0) {
        Write-SelfHealLog 'no compose services resolved; skipping'
        return
    }

    Write-SelfHealLog "ensuring compose services are up: $($services -join ' ')"
    $result = Invoke-Compose (@('up', '-d') + $services)
    if ($result.Output) {
        Add-Content -Path $logFile -Value $result.Output
    }
    if ($result.ExitCode -eq 0) {
        Write-SelfHealLog 'compose self-heal pass completed'
    } else {
        Write-SelfHealLog 'compose self-heal pass failed; see log above'
    }
}

try {
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $lock = [System.IO.File]::Open($lockFile, 'CreateNew', 'Write', 'None')
} catch {
    Write-SelfHealLog 'another portable self-heal worker is already running'
    exit 0
}

try {
    Write-SelfHealLog "started mode=$(if ($Loop) { 'loop' } else { 'once' }) project=$projectRoot"
    Invoke-SelfHealOnce
    if ($Loop) {
        while ($true) {
            Start-Sleep -Seconds $interval
            Invoke-SelfHealOnce
        }
    }
} finally {
    if ($lock) { $lock.Dispose() }
    Remove-Item -Path $lockFile -Force -ErrorAction SilentlyContinue
}
