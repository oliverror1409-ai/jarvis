<#
.SYNOPSIS
    Stops then starts one or more Jarvis components, waiting for each health
    endpoint to actually come up before proceeding.

.PARAMETER Component
    bridge | backend | daemon | ui | all (default: all)

.PARAMETER TimeoutSec
    How long to wait for each component's health endpoint after starting it.
    Default 60s.

.DESCRIPTION
    Only stops/starts the component(s) requested — never touches the rest of
    the stack, so a partial restart can never leave unrelated services down.
    On failure, reports exactly which component didn't come back up and exits
    with code 1; the other components are left running/healthy.
#>
[CmdletBinding()]
param(
    [ValidateSet('bridge', 'backend', 'daemon', 'ui', 'all')]
    [string]$Component = 'all',

    [int]$TimeoutSec = 60,

    # Restart even if Jarvis is mid-conversation. Stopping the daemon silences
    # speech first, so a restart during a reply cuts it off mid-sentence: the
    # answer is generated and logged, but the user never hears it and Jarvis
    # looks like he ignored the question.
    [switch]$Force,

    # How recently the user or Jarvis must have spoken for a restart to be
    # judged an interruption.
    [int]$ConversationIdleSec = 45
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$repo = Join-Path $repoRoot 'OpenJarvis'
$voice = Join-Path $repoRoot 'VoiceJarvisSrc'
$logsDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }

# ---------------------------------------------------------------------------
# Maintenance lock — tells watchdog-jarvis.ps1 to stand down while this
# script is actively stopping/starting components, so it cannot race in and
# start a second daemon while a human is deliberately restarting one.
# Always released via try/finally, even on failure.
# ---------------------------------------------------------------------------

$maintenanceLockPath = Join-Path $logsDir 'maintenance.lock'

function Enter-MaintenanceLock {
    param([string]$Reason = 'restart-jarvis.ps1')
    $payload = [ordered]@{ timestamp = (Get-Date).ToString('o'); pid = $PID; reason = $Reason }
    $json = $payload | ConvertTo-Json
    [System.IO.File]::WriteAllText($maintenanceLockPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Exit-MaintenanceLock {
    if (Test-Path -LiteralPath $maintenanceLockPath) {
        Remove-Item -LiteralPath $maintenanceLockPath -Force -Confirm:$false -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Health check helper — reports progress periodically instead of appearing
# hung, since the daemon can legitimately take 60-120s to boot (Whisper load).
# ---------------------------------------------------------------------------

function Wait-ForHealth {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][int]$TimeoutSec,
        [string]$Label = $Url,
        [int]$ProgressEverySec = 15
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $start = Get-Date
    $lastProgress = Get-Date
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            return $true
        } catch {
            Start-Sleep -Seconds 2
        }
        if (((Get-Date) - $lastProgress).TotalSeconds -ge $ProgressEverySec) {
            $elapsed = [int]((Get-Date) - $start).TotalSeconds
            Write-Host ("    ... still waiting for {0} ({1}s elapsed, timeout {2}s)" -f $Label, $elapsed, $TimeoutSec) -ForegroundColor DarkGray
            $lastProgress = Get-Date
        }
    }
    return $false
}

# ---------------------------------------------------------------------------
# Daemon-specific helpers (item 13: no overlapping voices on restart)
# ---------------------------------------------------------------------------

function Send-DaemonStopSpeech {
    # Silence any in-progress speech immediately. Best-effort: the daemon may
    # already be dead, in which case this just fails and we move on.
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8123/stop' -Method Post -Body '{}' `
            -ContentType 'application/json' -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop | Out-Null
    } catch { }
}

function Test-ConversationInFlight {
    <#
        True when Jarvis is speaking, or when anything was said in either
        direction within $ConversationIdleSec. Restarting then throws away a
        reply the user is waiting on.
    #>
    try {
        $state = Invoke-RestMethod -Uri 'http://127.0.0.1:8123/face-state' -TimeoutSec 3 -ErrorAction Stop
    } catch {
        return $false   # No daemon to interrupt.
    }
    if ($state.speaking) { return $true }
    $last = $state.feed | Select-Object -Last 1
    if (-not $last -or -not $last.t) { return $false }
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int]$last.t
    return ($age -lt $ConversationIdleSec)
}

function Wait-ForProcessCountZero {
    param([Parameter(Mandatory)][scriptblock]$Finder, [int]$TimeoutSec = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $procs = & $Finder
        if (-not $procs -or @($procs).Count -eq 0) { return $true }
        Start-Sleep -Seconds 1
    }
    $procs = & $Finder
    return (-not $procs -or @($procs).Count -eq 0)
}

# ---------------------------------------------------------------------------
# Start actions (mirrors start-jarvis-app.ps1's launch commands)
# ---------------------------------------------------------------------------

function Start-BridgeComponent {
    # NOTE: claude-bridge.py owns its own RotatingFileHandler writing directly
    # to logs\bridge.log (2MB x3 backups). Do NOT also redirect this wrapper's
    # stdout there — two writers on the same path causes a file-lock
    # PermissionError that crashes the bridge on startup (seen firsthand while
    # testing this script). The app's own log already covers diagnosability.
    # `uv run python` was the launch here for months and could never have
    # worked: there is no pyproject.toml in this directory, so uv resolves an
    # interpreter that has none of the bridge's dependencies and it dies on
    # `import requests` before it binds a port. It only ever looked fine
    # because a bridge started by hand was already listening. .venv is the one
    # environment on this machine with fastapi, uvicorn and requests in it.
    $bridgePython = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $bridgePython)) {
        Write-Warning "No .venv at $bridgePython - the bridge cannot start."
        return
    }
    # stderr MUST be redirected: a detached pythonw has no valid stderr
    # handle, so the first library warning written to it kills the process
    # before the app's own logging even starts (seen firsthand: FastAPI's
    # on_event DeprecationWarning made every launch from this script die
    # silently). bridge.err.log has one writer — this start — so there is no
    # file-lock clash with the app's own bridge.log.
    Start-Process -FilePath $bridgePython -WindowStyle Hidden `
        -ArgumentList "`"$repoRoot\claude-bridge.py`"" -WorkingDirectory $repo `
        -RedirectStandardError (Join-Path $logsDir 'bridge.err.log')
}

# Move a log aside so the next run starts on a clean file without throwing the
# last run away. The backend deliberately TRUNCATES its log on start (one
# writer per start, see the trap below), and it holds that file open for as
# long as it runs -- so roll-logs.ps1 can never rotate it, and every restart
# silently discarded the history. On 16 Sep that was 25.7 MB. This is the one
# moment the file is not held, so this is where the roll belongs.
function Move-LogAside {
    param([string]$Path, [int]$Keep = 2)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) { return }
    $oldest = "{0}.{1}" -f $Path, $Keep
    if (Test-Path -LiteralPath $oldest) {
        Remove-Item -LiteralPath $oldest -Force -ErrorAction SilentlyContinue
    }
    for ($i = $Keep - 1; $i -ge 1; $i--) {
        $from = "{0}.{1}" -f $Path, $i
        $to = "{0}.{1}" -f $Path, ($i + 1)
        if (Test-Path -LiteralPath $from) {
            Move-Item -LiteralPath $from -Destination $to -Force -ErrorAction SilentlyContinue
        }
    }
    Move-Item -LiteralPath $Path -Destination ("{0}.1" -f $Path) -Force -ErrorAction SilentlyContinue
}

function Start-BackendComponent {
    Move-LogAside -Path "$logsDir\backend.log"
    Move-LogAside -Path "$logsDir\backend.err.log"
    # Same trap as the bridge above, and it cost a morning: the wrapper used to
    # append to backend.log, so a start racing a stale writer died on the file
    # lock. The log kept the last healthy run's lines, the port never opened,
    # and every reply came back "connection refused" while the script reported
    # a restart. Start-Process owns the handles here, one writer per start.
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", `
        "Set-Location '$repo'; uv run jarvis serve --port 8000" `
        -RedirectStandardOutput "$logsDir\backend.log" `
        -RedirectStandardError "$logsDir\backend.err.log"
}

function Start-DaemonComponent {
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-Command", `
        "Set-Location '$voice'; `$env:PYTHONPATH = '$voice\src'; & '.\.mamba_env\python.exe' -m jarvis.daemon *>&1 | Out-File -FilePath '$logsDir\daemon.log' -Append -Encoding utf8"
}

function Start-UIComponent {
    # Its own windows rather than a browser's: run through Edge, the taskbar
    # wore Edge's logo and the second screen came up as another tab in the
    # same window instead of a window on the other monitor.
    $hostScript = Join-Path $repoRoot 'jarvis-window.py'
    $pythonw = Join-Path $repoRoot 'VoiceJarvisSrc\.mamba_env\pythonw.exe'
    if ((Test-Path $hostScript) -and (Test-Path $pythonw)) {
        Start-Process -FilePath $pythonw -ArgumentList "`"$hostScript`"" -WorkingDirectory $repoRoot
    } else {
        Start-Process "msedge" -ArgumentList "--app=http://127.0.0.1:8100/ui", "--window-size=1180,720"
    }

    # Captions belong to the voice, not to the HUD: he asked for them because
    # the window is behind whatever he is working in, so they have to come up
    # whether or not the HUD is open and survive a restart of it.
    $captions = Join-Path $repoRoot 'jarvis-subtitles.py'
    if ((Test-Path $captions) -and (Test-Path $pythonw)) {
        Start-Process -FilePath $pythonw -ArgumentList "`"$captions`"" -WorkingDirectory $repoRoot
    }
}

# ---------------------------------------------------------------------------
# Stop helpers (same broad process-tree match as stop-jarvis.ps1)
# ---------------------------------------------------------------------------

function Get-BridgeProcesses {
    # Python only: the broad command-line match also caught any PowerShell
    # whose arguments merely mentioned the file — including the shell running
    # this very script from an automation, which then killed itself mid-stop.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -like '*claude-bridge*' }
}
function Get-BackendProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*jarvis*serve*' -and $_.CommandLine -notlike '*claude-bridge*' }
}
function Get-DaemonProcesses {
    # The daemon is named by what it runs, never by which interpreter ran it.
    # Everything else Jarvis owns comes out of the same environment -- the
    # HUD, the caption bubble, the nightly file index -- so matching the
    # executable path killed those too: a restart used to close his interface,
    # and a rebuild of the index died halfway through and reported failure
    # for a reason that had nothing to do with the index.
    # Two ways it gets started and both are the daemon: `python -m jarvis.daemon`
    # from this script, and `jarvis.exe serve` from start-jarvis.ps1. Matching
    # only the first meant a restart found nothing to stop, left the old
    # process running, and then passed its own health check against it -- so
    # every restart reported success and changed nothing.
    # And the backend is not the daemon, however alike the two command lines
    # read. `uv run jarvis serve --port 8000` in OpenJarvis becomes a
    # `jarvis.exe serve` child, which the match above swallowed: the script
    # started the backend, reported it healthy, then killed it again three
    # lines later while stopping the daemon. Every reply after that came back
    # "connection refused" from port 8000, and the restart looked clean.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { ($_.CommandLine -like '*jarvis.daemon*' -or
                        $_.CommandLine -like '*jarvis.exe* serve*' -or
                        $_.CommandLine -like '*jarvis.exe" serve*') -and
                       $_.CommandLine -notlike '*OpenJarvis*' -and
                       $_.CommandLine -notlike '*--port 8000*' }
}
function Get-UIWindowProcesses {
    # Three things, all "the UI": the native window it uses now, the caption
    # bubble (a tool window, so it has no taskbar button and no findable
    # title from outside), and an Edge app window left over from before.
    # Matched by command line where a title will not do, so a restart never
    # leaves a second HUD or a stale caption behind.
    $byTitle = Get-Process pythonw, msedge -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -like '*JARVIS*' }
    $byCommand = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'jarvis-window\.py|jarvis-subtitles\.py' } |
        ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
    @($byTitle) + @($byCommand) | Where-Object { $_ } | Sort-Object Id -Unique
}

function Stop-ProcessSet {
    param([Parameter(Mandatory)][string]$Name, [scriptblock]$Finder)
    $procs = & $Finder
    if (-not $procs -or @($procs).Count -eq 0) { return }
    foreach ($p in @($procs)) {
        $targetId = $p.Id
        if (-not $targetId) { $targetId = $p.ProcessId }
        try { Stop-Process -Id $targetId -Force -Confirm:$false -ErrorAction Stop } catch { }
    }
    Start-Sleep -Milliseconds 500
}

# ---------------------------------------------------------------------------
# Per-component restart routine
# ---------------------------------------------------------------------------

function Restart-Component {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$StopFinder,
        [Parameter(Mandatory)][scriptblock]$StartAction,
        [string]$HealthUrl,
        [int]$HealthTimeoutSec = $TimeoutSec,
        [scriptblock]$PreStopAction,
        [int]$PostStopWaitSec = 0
    )
    if ($PreStopAction) {
        Write-Host ("  🔇 {0}: silencing before stop..." -f $Name) -ForegroundColor Cyan
        & $PreStopAction
    }

    Write-Host ("  🔄 {0}: stopping..." -f $Name) -ForegroundColor Cyan
    Stop-ProcessSet -Name $Name -Finder $StopFinder

    if ($PostStopWaitSec -gt 0) {
        Write-Host ("  ⏳ {0}: waiting for process count to reach zero (timeout {1}s) before starting a new one..." -f $Name, $PostStopWaitSec) -ForegroundColor Cyan
        $zero = Wait-ForProcessCountZero -Finder $StopFinder -TimeoutSec $PostStopWaitSec
        if ($zero) {
            Write-Host ("  ✅ {0}: confirmed fully stopped (process count = 0)." -f $Name) -ForegroundColor Green
        } else {
            Write-Host ("  ⚠️  {0}: process(es) still present after {1}s — starting a new instance anyway. Watch for overlapping voices." -f $Name, $PostStopWaitSec) -ForegroundColor Yellow
        }
    }

    Write-Host ("  🔄 {0}: starting..." -f $Name) -ForegroundColor Cyan
    & $StartAction

    if (-not $HealthUrl) {
        Start-Sleep -Seconds 2
        Write-Host ("  ✅ {0}: launched (no health endpoint to confirm)" -f $Name) -ForegroundColor Green
        return $true
    }

    Write-Host ("  ⏳ {0}: waiting for {1} to answer (timeout {2}s)..." -f $Name, $HealthUrl, $HealthTimeoutSec) -ForegroundColor Cyan
    $ok = Wait-ForHealth -Url $HealthUrl -TimeoutSec $HealthTimeoutSec -Label $Name
    if ($ok) {
        Write-Host ("  ✅ {0}: up and healthy" -f $Name) -ForegroundColor Green
        return $true
    } else {
        Write-Host ("  ❌ {0}: FAILED to become healthy within {1}s (checked {2})" -f $Name, $HealthTimeoutSec, $HealthUrl) -ForegroundColor Red
        return $false
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '🔄 === Restarting Jarvis ===' -ForegroundColor Cyan
Write-Host ''

$targets = @()
if ($Component -eq 'all') {
    $targets = @('bridge', 'backend', 'daemon', 'ui')
} else {
    $targets = @($Component)
}

# Restarting the daemon mid-conversation silences the reply the user is
# waiting on. The answer still reaches the log, so it looks afterwards as
# though Jarvis said something he never got to speak.
if (($targets -contains 'daemon') -and (-not $Force) -and (Test-ConversationInFlight)) {
    Write-Host ''
    Write-Host '  🗣️  Jarvis is mid-conversation — restart refused.' -ForegroundColor Yellow
    Write-Host ("     Nothing was stopped. Wait {0}s for quiet, or re-run with -Force." -f $ConversationIdleSec) -ForegroundColor Yellow
    Write-Host ''
    exit 2
}

# The daemon takes 60-120s to boot (Whisper load) — always wait at least
# 180s for its health check, regardless of -TimeoutSec, so a slow-but-fine
# boot never gets reported as a failure.
$daemonTimeoutSec = [Math]::Max($TimeoutSec, 180)

$results = @{}
$exitCode = 0

try {
    Enter-MaintenanceLock -Reason ("restart-jarvis.ps1 -Component {0}" -f $Component)

    foreach ($t in $targets) {
        switch ($t) {
            'bridge' {
                $results['bridge'] = Restart-Component -Name 'Bridge' -StopFinder { Get-BridgeProcesses } -StartAction { Start-BridgeComponent } -HealthUrl 'http://127.0.0.1:8100/health' -HealthTimeoutSec $TimeoutSec
            }
            'backend' {
                $results['backend'] = Restart-Component -Name 'Backend' -StopFinder { Get-BackendProcesses } -StartAction { Start-BackendComponent } -HealthUrl 'http://127.0.0.1:8000/health' -HealthTimeoutSec $TimeoutSec
            }
            'daemon' {
                # item 13: silence speech first, stop, WAIT for the process
                # count to hit zero, only then start the replacement — this is
                # what prevents the dying instance from overlapping the new one.
                $results['daemon'] = Restart-Component -Name 'Daemon' -StopFinder { Get-DaemonProcesses } -StartAction { Start-DaemonComponent } -HealthUrl 'http://127.0.0.1:8123/health' -HealthTimeoutSec $daemonTimeoutSec -PreStopAction { Send-DaemonStopSpeech } -PostStopWaitSec 20
            }
            'ui' {
                $results['ui'] = Restart-Component -Name 'UI' -StopFinder { Get-UIWindowProcesses } -StartAction { Start-UIComponent } -HealthUrl $null
            }
        }
    }

    Write-Host ''
    $failed = @($results.Keys | Where-Object { -not $results[$_] })
    if ($failed.Count -gt 0) {
        Write-Host ("❌ Restart finished with failures: {0}" -f ($failed -join ', ')) -ForegroundColor Red
        $exitCode = 1
    } else {
        Write-Host '✅ Restart finished — all requested components healthy.' -ForegroundColor Green
    }
} finally {
    Exit-MaintenanceLock
}

Write-Host ''
Write-Host '--- Post-restart status ---' -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'status-jarvis.ps1')

exit $exitCode
