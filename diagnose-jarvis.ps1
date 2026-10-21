<#
.SYNOPSIS
    Full troubleshooting report for the Jarvis stack: status, recent logs,
    port conflicts, GPU, CLI tooling, and resolved config values.

.DESCRIPTION
    Everything status-jarvis.ps1 reports, plus:
      - last 40 lines of each log file
      - port conflict detection on 8000/8100/8123 (who's actually listening)
      - GPU presence via nvidia-smi
      - whether `claude` is on PATH and its version
      - python/uv availability
      - the resolved config values the daemon will actually use

    Writes the full report to logs\diagnose-<timestamp>.txt as well as
    printing it to the console.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$repoRoot = $PSScriptRoot
$logsDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }

$reportLines = New-Object System.Collections.Generic.List[string]
function Add-Report {
    param([string]$Text = '')
    $reportLines.Add($Text)
    Write-Host $Text
}

Add-Report ''
Add-Report '🩺 === Jarvis Diagnostic Report ==='
Add-Report ("Generated: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Add-Report ''

# ---------------------------------------------------------------------------
# 1. Status (reuse status-jarvis.ps1's pretty output)
# ---------------------------------------------------------------------------

Add-Report '--- 1. Stack Status ---'
$statusScript = Join-Path $repoRoot 'status-jarvis.ps1'
if (Test-Path $statusScript) {
    $statusOutput = & $statusScript 2>&1 | Out-String
    $reportLines.Add($statusOutput.TrimEnd())
    Write-Host $statusOutput.TrimEnd()
} else {
    Add-Report '  ⚠️  status-jarvis.ps1 not found'
}
Add-Report ''

# ---------------------------------------------------------------------------
# 2. Recent logs
# ---------------------------------------------------------------------------

Add-Report '--- 2. Recent Logs (last 40 lines each) ---'
$logFiles = @('bridge.log', 'backend.log', 'daemon.log', 'watchdog.log')
foreach ($lf in $logFiles) {
    $lp = Join-Path $logsDir $lf
    Add-Report ("  📄 {0}" -f $lp)
    if (Test-Path -LiteralPath $lp) {
        $tail = Get-Content -LiteralPath $lp -Tail 40 -ErrorAction SilentlyContinue
        if ($tail) {
            foreach ($line in $tail) { Add-Report ("    {0}" -f $line) }
        } else {
            Add-Report '    (empty)'
        }
    } else {
        Add-Report '    ⚪ not found'
    }
    Add-Report ''
}

# also check the older ad-hoc root-level debug logs if present
foreach ($lf in @('bridge-debug.log', 'daemon-debug.log')) {
    $lp = Join-Path $repoRoot $lf
    if (Test-Path -LiteralPath $lp) {
        Add-Report ("  📄 {0} (legacy debug log)" -f $lp)
        $tail = Get-Content -LiteralPath $lp -Tail 40 -ErrorAction SilentlyContinue
        if ($tail) { foreach ($line in $tail) { Add-Report ("    {0}" -f $line) } }
        Add-Report ''
    }
}

# ---------------------------------------------------------------------------
# 3. Port conflict detection
# ---------------------------------------------------------------------------

Add-Report '--- 3. Port Conflict Detection ---'
$expectedPorts = @{ 8100 = 'bridge'; 8000 = 'backend'; 8123 = 'daemon' }
foreach ($port in $expectedPorts.Keys | Sort-Object) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) {
        Add-Report ("  ⚪ Port {0} ({1}): nothing listening" -f $port, $expectedPorts[$port])
        continue
    }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    $procName = 'unknown'
    $procPath = ''
    if ($proc) {
        $procName = $proc.ProcessName
        try { $procPath = $proc.Path } catch { $procPath = '' }
    }
    $looksLikeJarvis = $procName -match 'python|powershell|uv'
    if ($looksLikeJarvis) {
        Add-Report ("  ✅ Port {0} ({1}): owned by pid={2} ({3})" -f $port, $expectedPorts[$port], $conn.OwningProcess, $procName)
    } else {
        Add-Report ("  ❌ Port {0} ({1}): CONFLICT — owned by unexpected process pid={2} ({3}) {4}" -f $port, $expectedPorts[$port], $conn.OwningProcess, $procName, $procPath)
    }
}
Add-Report ''

# ---------------------------------------------------------------------------
# 4. GPU presence
# ---------------------------------------------------------------------------

Add-Report '--- 4. GPU (nvidia-smi) ---'
$nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvidiaSmi) {
    try {
        $gpuInfo = & nvidia-smi --query-gpu=name,utilization.gpu,power.draw,memory.used,memory.total,temperature.gpu --format=csv 2>$null
        foreach ($line in $gpuInfo) { Add-Report ("  {0}" -f $line) }
    } catch {
        Add-Report ("  ⚠️  nvidia-smi present but failed to run: {0}" -f $_.Exception.Message)
    }
} else {
    Add-Report '  ❌ nvidia-smi not found on PATH (no NVIDIA GPU or driver not installed)'
}
Add-Report ''

# ---------------------------------------------------------------------------
# 5. claude CLI
# ---------------------------------------------------------------------------

Add-Report '--- 5. claude CLI ---'
$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCmd) {
    Add-Report ("  ✅ Found: {0}" -f $claudeCmd.Source)
    try {
        $verOut = & claude --version 2>$null
        Add-Report ("  ℹ️  Version: {0}" -f ($verOut -join ' '))
    } catch {
        Add-Report ("  ⚠️  Failed to run 'claude --version': {0}" -f $_.Exception.Message)
    }
} else {
    Add-Report '  ❌ claude CLI not found on PATH'
}
Add-Report ''

# ---------------------------------------------------------------------------
# 6. python / uv availability
# ---------------------------------------------------------------------------

Add-Report '--- 6. python / uv ---'
foreach ($tool in @('python', 'uv')) {
    $cmd = Get-Command $tool -ErrorAction SilentlyContinue
    if ($cmd) {
        Add-Report ("  ✅ {0}: {1}" -f $tool, $cmd.Source)
        try {
            $verOut = & $tool --version 2>$null
            Add-Report ("     Version: {0}" -f ($verOut -join ' '))
        } catch { }
    } else {
        Add-Report ("  ❌ {0}: not found on PATH" -f $tool)
    }
}
Add-Report ''

# ---------------------------------------------------------------------------
# 7. Resolved config values
# ---------------------------------------------------------------------------

Add-Report '--- 7. Resolved Config (~/.config/jarvis/config.json) ---'
$configPath = Join-Path $env:USERPROFILE '.config\jarvis\config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    Add-Report ("  ❌ Config file not found at {0} — daemon is running on built-in defaults" -f $configPath)
} else {
    $bytes = [System.IO.File]::ReadAllBytes($configPath)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if ($hasBom) {
        Add-Report '  ❌ CRITICAL: config.json has a UTF-8 BOM! The daemon silently ignores this file and falls back to (slow) defaults.'
        Add-Report '     Fix: re-save without BOM — see status-jarvis.ps1 output for the exact command.'
    } else {
        Add-Report '  ✅ No BOM'
    }
    try {
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        $cfg = $text | ConvertFrom-Json -ErrorAction Stop
        Add-Report '  ✅ Valid JSON. Resolved values:'
        $cfg.PSObject.Properties | Sort-Object Name | ForEach-Object {
            $val = $_.Value
            if ($val -is [System.Array]) { $val = '[' + ($val -join ', ') + ']' }
            Add-Report ("     {0,-28} = {1}" -f $_.Name, $val)
        }
    } catch {
        Add-Report ("  ❌ INVALID JSON: {0} — daemon will fall back to defaults" -f $_.Exception.Message)
    }
}
Add-Report ''

Add-Report '=== End of report ==='
Add-Report ''

# ---------------------------------------------------------------------------
# Write to file
# ---------------------------------------------------------------------------

$timestamp = Get-Date -Format 'yyyy-MM-dd-HHmmss'
$reportPath = Join-Path $logsDir ("diagnose-{0}.txt" -f $timestamp)
$fullText = ($reportLines -join "`r`n")
[System.IO.File]::WriteAllText($reportPath, $fullText, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
Write-Host ("📝 Full report written to: {0}" -f $reportPath) -ForegroundColor Cyan
