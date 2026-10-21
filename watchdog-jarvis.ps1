<#
.SYNOPSIS
    Supervision loop for the Jarvis stack: checks bridge/backend/daemon health
    and restarts whichever component is down.

.PARAMETER IntervalSec
    Seconds between checks when looping (default 30).

.PARAMETER Once
    Run a single check-and-act pass then exit. Use this from Task Scheduler.

.DESCRIPTION
    - A component is only acted on after 2 CONSECUTIVE failed health checks
      (5s timeout each) — a single slow response never triggers a restart.
    - If a component fails to come back up 3 restart attempts in a row, the
      watchdog backs off: it stops trying that component and logs loudly,
      rather than spinning forever. Backoff state persists across -Once runs
      in logs\watchdog-state.json so Task Scheduler invocations remember it.
    - Every action is timestamped into logs\watchdog.log.
#>
[CmdletBinding()]
param(
    [int]$IntervalSec = 30,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$repo = Join-Path $repoRoot 'OpenJarvis'
$voice = Join-Path $repoRoot 'VoiceJarvisSrc'
$logsDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
$logPath = Join-Path $logsDir 'watchdog.log'
$statePath = Join-Path $logsDir 'watchdog-state.json'
$maintenanceLockPath = Join-Path $logsDir 'maintenance.lock'

$MAX_RESTART_ATTEMPTS = 3
$FAILS_BEFORE_ACTION = 2
$HEALTH_TIMEOUT_SEC = 5
$MAINTENANCE_LOCK_MAX_AGE_MIN = 10

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

function Write-WatchdogLog {
    param([Parameter(Mandatory)][string]$Message, [string]$Level = 'INFO')
    $emoji = 'ℹ️'
    if ($Level -eq 'WARN') { $emoji = '⚠️' }
    if ($Level -eq 'ERROR') { $emoji = '❌' }
    $line = "{0} [{1}] {2} {3}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $emoji, $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        default { Write-Host $line }
    }
}

# ---------------------------------------------------------------------------
# State persistence (consecutive-failure and backoff counters)
# ---------------------------------------------------------------------------

function Get-DefaultState {
    [ordered]@{
        bridge  = [ordered]@{ consecutiveHealthFails = 0; consecutiveRestartFails = 0; backedOff = $false }
        backend = [ordered]@{ consecutiveHealthFails = 0; consecutiveRestartFails = 0; backedOff = $false }
        daemon  = [ordered]@{ consecutiveHealthFails = 0; consecutiveRestartFails = 0; backedOff = $false }
        # Per-HUD-service start attempts, so a service that can never bind is
        # given up on rather than retried every five minutes forever.
        services = [ordered]@{}
    }
}

function Read-WatchdogState {
    if (-not (Test-Path -LiteralPath $statePath)) { return Get-DefaultState }
    try {
        $raw = Get-Content -LiteralPath $statePath -Raw -Encoding utf8
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        $state = Get-DefaultState
        foreach ($name in @('bridge', 'backend', 'daemon')) {
            if ($obj.$name) {
                $state[$name].consecutiveHealthFails = [int]$obj.$name.consecutiveHealthFails
                $state[$name].consecutiveRestartFails = [int]$obj.$name.consecutiveRestartFails
                $state[$name].backedOff = [bool]$obj.$name.backedOff
            }
        }
        # Carry the service counters back. Without this they were saved every
        # pass and read by none of them, so "start attempt 1 of 3" was all the
        # log ever said however many times a service had failed.
        if ($obj.services) {
            foreach ($svc in $obj.services.PSObject.Properties) {
                $state['services'][$svc.Name] = [ordered]@{
                    failedStarts = [int]$svc.Value.failedStarts
                    gaveUp       = [bool]$svc.Value.gaveUp
                }
            }
        }
        return $state
    } catch {
        Write-WatchdogLog -Message ("State file unreadable ({0}), starting fresh." -f $_.Exception.Message) -Level 'WARN'
        return Get-DefaultState
    }
}

function Save-WatchdogState {
    param($State)
    $json = $State | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($statePath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------------------
# Maintenance lock (item 14) — restart-jarvis.ps1, stop-jarvis.ps1, and
# start-jarvis-app.ps1 write logs\maintenance.lock while they work. While a
# FRESH lock exists, the watchdog must not touch anything, or it will "helpfully"
# restart a component a human is deliberately taking down mid-maintenance —
# which is exactly how a duplicate daemon gets created. A lock older than
# $MAINTENANCE_LOCK_MAX_AGE_MIN is treated as stale (the script that wrote it
# probably crashed) and is IGNORED, loudly, so a crashed script can never
# disable supervision forever.
# ---------------------------------------------------------------------------

function Test-MaintenanceLockActive {
    if (-not (Test-Path -LiteralPath $maintenanceLockPath)) { return $false }
    try {
        $raw = Get-Content -LiteralPath $maintenanceLockPath -Raw -Encoding utf8
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        $ts = [datetime]$obj.timestamp
        $ageMin = ((Get-Date) - $ts).TotalMinutes
        if ($ageMin -le $MAINTENANCE_LOCK_MAX_AGE_MIN) {
            Write-WatchdogLog -Message ("Maintenance lock active (pid={0}, reason='{1}', age={2:N1}m) — skipping this entire pass." -f $obj.pid, $obj.reason, $ageMin) -Level 'WARN'
            return $true
        } else {
            Write-WatchdogLog -Message ("Maintenance lock is STALE (age={0:N1}m > {1}m, pid={2}, reason='{3}') — IGNORING it and resuming normal supervision. The script that wrote it likely crashed without cleaning up; investigate {4}." -f $ageMin, $MAINTENANCE_LOCK_MAX_AGE_MIN, $obj.pid, $obj.reason, $maintenanceLockPath) -Level 'ERROR'
            return $false
        }
    } catch {
        Write-WatchdogLog -Message ("Maintenance lock file unreadable ({0}) — ignoring it and resuming normal supervision." -f $_.Exception.Message) -Level 'WARN'
        return $false
    }
}

# ---------------------------------------------------------------------------
# Health + restart actions
# ---------------------------------------------------------------------------

function Test-ComponentHealthy {
    param([Parameter(Mandatory)][string]$Url)
    try {
        $null = Invoke-WebRequest -Uri $Url -TimeoutSec $HEALTH_TIMEOUT_SEC -UseBasicParsing -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-BridgeProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*claude-bridge*' }
}
function Get-BackendProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*jarvis*serve*' -and $_.CommandLine -notlike '*claude-bridge*' }
}
function Get-DaemonProcesses {
    # The HUD window and the caption bubble run from the same environment
    # as the daemon, so matching on the executable path alone would let a
    # daemon restart close his interface behind him.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.CommandLine -like '*jarvis.daemon*' -or $_.ExecutablePath -like '*VoiceJarvisSrc*') -and
            $_.CommandLine -notmatch 'jarvis-window\.py|jarvis-subtitles\.py'
        }
}

function Stop-ProcessSet {
    param([scriptblock]$Finder)
    $procs = & $Finder
    if (-not $procs) { return }
    foreach ($p in @($procs)) {
        $targetId = $p.Id
        if (-not $targetId) { $targetId = $p.ProcessId }
        try { Stop-Process -Id $targetId -Force -Confirm:$false -ErrorAction Stop } catch { }
    }
    Start-Sleep -Milliseconds 500
}

function Start-BridgeProcess {
    # .venv, not `uv run python`, and the same stderr redirect the other two
    # launchers use: a detached pythonw dies on its first stderr write, and
    # two launchers disagreeing about the interpreter is what let a second
    # bridge race the first for port 8100.
    $bridgePython = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $bridgePython)) {
        Write-WatchdogLog -Level 'WARN' -Message "No .venv at $bridgePython - cannot start the bridge."
        return
    }
    Start-Process -FilePath $bridgePython -WindowStyle Hidden `
        -ArgumentList "`"$repoRoot\claude-bridge.py`"" -WorkingDirectory $repo `
        -RedirectStandardError (Join-Path $logsDir 'bridge.err.log')
}
function Start-BackendProcess {
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-Command", `
        "Set-Location '$repo'; uv run jarvis serve --port 8000 *>&1 | Out-File -FilePath '$logsDir\backend.log' -Append -Encoding utf8"
}
function Start-DaemonProcess {
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-Command", `
        "Set-Location '$voice'; `$env:PYTHONPATH = '$voice\src'; & '.\.mamba_env\python.exe' -m jarvis.daemon *>&1 | Out-File -FilePath '$logsDir\daemon.log' -Append -Encoding utf8"
}

# The services behind the HUD buttons. They are not part of the voice loop, so
# they are only ever started, never stopped: a button that opens a dead port is
# the failure, and a restart of something he might be using is not the fix.
$hudServices = @(
    @{ Name = 'open-notebook'; Port = 3007;  Script = 'start-open-notebook.ps1' },
    @{ Name = 'fooocus';       Port = 7866;  Script = 'start-fooocus.ps1' },
    @{ Name = 'brightbean';    Port = 8129;  Script = 'start-brightbean.ps1' },
    @{ Name = 'kokoro';        Port = 8880;  Script = 'start-kokoro.ps1' },
    @{ Name = 'omniroute';     Port = 20128; Script = 'start-omniroute.ps1' }
)

function Test-PortListening($port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

# How many times to try starting a HUD service before accepting it is broken.
$MAX_SERVICE_STARTS = 3
# How long to give one its port after a start attempt. Measured, not guessed:
# brightbean binds in 10 seconds, OmniRoute in 77 -- it warms a model
# catalogue before it opens the listener. Twenty-five seconds called OmniRoute
# a failure three times over while it was starting perfectly well. A pass only
# ever pays this for a service that is actually down, and the single-instance
# mutex means a slow pass cannot collide with the next scheduled one.
$SERVICE_BIND_TIMEOUT_SEC = 120

function Invoke-HudServicePass {
    param($State)

    # This used to fire Start-Process and never look again. open-notebook,
    # brightbean and omniroute had been dead since 7 September and the log
    # carried "port N is dead - starting" for each of them every five
    # minutes -- getting on for nine hundred warnings a day about three
    # things that were never once started successfully, which is the same as
    # no alerting at all. So: wait for the port, count the failures, and
    # after three say so once and stop.
    foreach ($service in $hudServices) {
        $name = $service.Name
        # The state is an ordered hashtable, so these are keys, not members.
        if (-not $State.Contains('services')) { $State['services'] = [ordered]@{} }
        if (-not $State['services'].Contains($name)) {
            $State['services'][$name] = [ordered]@{ failedStarts = 0; gaveUp = $false }
        }
        $s = $State['services'][$name]

        if (Test-PortListening $service.Port) {
            if ([int]$s.failedStarts -gt 0 -or $s.gaveUp) {
                Write-WatchdogLog -Message ("{0}: up again on port {1}" -f $name, $service.Port)
                $s.failedStarts = 0
                $s.gaveUp = $false
            }
            continue
        }

        if ($s.gaveUp) { continue }

        $script = Join-Path $repoRoot $service.Script
        if (-not (Test-Path $script)) { continue }

        Write-WatchdogLog -Message ("{0}: port {1} is dead - starting" -f $name, $service.Port) -Level 'WARN'
        # The path is quoted because Start-Process does NOT quote the elements
        # of -ArgumentList, and this repo lives under "Projects Claude". The
        # child was receiving `-File C:\Users\you\Projects`, failing to find
        # it, and exiting before it could write a single line to a log -- which
        # is why open-notebook, brightbean and omniroute were reported dead and
        # restarted every five minutes from 7 September without one of them
        # ever starting. Run by hand, all three scripts work.
        #
        # -Command survives the same treatment, which is why the backend and
        # daemon launches below never showed the fault: everything after it is
        # taken as the command whether or not it was split. -File takes exactly
        # one token.
        Start-Process powershell -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $script + '"')

        $deadline = (Get-Date).AddSeconds($SERVICE_BIND_TIMEOUT_SEC)
        $bound = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
            if (Test-PortListening $service.Port) { $bound = $true; break }
        }

        if ($bound) {
            Write-WatchdogLog -Message ("{0}: started, listening on {1}" -f $name, $service.Port)
            $s.failedStarts = 0
            continue
        }

        $s.failedStarts = [int]$s.failedStarts + 1
        if ($s.failedStarts -ge $MAX_SERVICE_STARTS) {
            $s.gaveUp = $true
            Write-WatchdogLog -Level 'ERROR' -Message (
                "{0}: did not bind port {1} after {2} start attempts. Not trying again until '{3}' is cleared. Its own log will say why." `
                -f $name, $service.Port, $s.failedStarts, (Split-Path -Leaf $statePath))
        } else {
            Write-WatchdogLog -Level 'WARN' -Message (
                "{0}: start attempt {1}/{2} did not bind port {3}" `
                -f $name, $s.failedStarts, $MAX_SERVICE_STARTS, $service.Port)
        }
    }
}

$componentSpecs = @{
    bridge  = [ordered]@{ Url = 'http://127.0.0.1:8100/health';   Stop = { Stop-ProcessSet -Finder { Get-BridgeProcesses } };  Start = { Start-BridgeProcess } }
    backend = [ordered]@{ Url = 'http://127.0.0.1:8000/health';   Stop = { Stop-ProcessSet -Finder { Get-BackendProcesses } }; Start = { Start-BackendProcess } }
    daemon  = [ordered]@{ Url = 'http://127.0.0.1:8123/face-state'; Stop = { Stop-ProcessSet -Finder { Get-DaemonProcesses } }; Start = { Start-DaemonProcess } }
}

# ---------------------------------------------------------------------------
# One check-and-act pass
# ---------------------------------------------------------------------------

function Invoke-WatchdogPass {
    if (Test-MaintenanceLockActive) {
        return
    }

    $state = Read-WatchdogState

    foreach ($name in @('bridge', 'backend', 'daemon')) {
        $spec = $componentSpecs[$name]
        $s = $state[$name]

        if ($s.backedOff) {
            Write-WatchdogLog -Message ("{0}: skipped (backed off after {1} failed restart attempts — fix manually, then delete {2} to resume watching)" -f $name, $MAX_RESTART_ATTEMPTS, (Split-Path -Leaf $statePath)) -Level 'WARN'
            continue
        }

        $healthy = Test-ComponentHealthy -Url $spec.Url
        if ($healthy) {
            if ($s.consecutiveHealthFails -gt 0) {
                Write-WatchdogLog -Message ("{0}: recovered (was failing)" -f $name)
            }
            $s.consecutiveHealthFails = 0
            $s.consecutiveRestartFails = 0
            continue
        }

        $s.consecutiveHealthFails += 1
        Write-WatchdogLog -Message ("{0}: health check failed ({1}/{2} consecutive)" -f $name, $s.consecutiveHealthFails, $FAILS_BEFORE_ACTION) -Level 'WARN'

        if ($s.consecutiveHealthFails -lt $FAILS_BEFORE_ACTION) {
            continue
        }

        # Acted threshold reached — restart it.
        Write-WatchdogLog -Message ("{0}: DOWN after {1} consecutive failures — restarting" -f $name, $s.consecutiveHealthFails) -Level 'ERROR'
        & $spec.Stop
        & $spec.Start

        $upDeadline = (Get-Date).AddSeconds(30)
        $cameUp = $false
        while ((Get-Date) -lt $upDeadline) {
            Start-Sleep -Seconds 3
            if (Test-ComponentHealthy -Url $spec.Url) { $cameUp = $true; break }
        }

        if ($cameUp) {
            Write-WatchdogLog -Message ("{0}: restart succeeded, healthy again" -f $name)
            $s.consecutiveHealthFails = 0
            $s.consecutiveRestartFails = 0
        } else {
            $s.consecutiveRestartFails += 1
            $s.consecutiveHealthFails = 0
            Write-WatchdogLog -Message ("{0}: restart attempt {1}/{2} did not bring it back up" -f $name, $s.consecutiveRestartFails, $MAX_RESTART_ATTEMPTS) -Level 'ERROR'
            if ($s.consecutiveRestartFails -ge $MAX_RESTART_ATTEMPTS) {
                $s.backedOff = $true
                Write-WatchdogLog -Message ("{0}: BACKING OFF after {1} failed restart attempts in a row. Needs manual attention. Watchdog will not touch it again until {2} is cleared." -f $name, $MAX_RESTART_ATTEMPTS, (Split-Path -Leaf $statePath)) -Level 'ERROR'
            }
        }
    }

    Invoke-HudServicePass -State $state
    Save-WatchdogState -State $state
    Invoke-LogRollPass
}

# ---------------------------------------------------------------------------
# Log rolling
# ---------------------------------------------------------------------------
# Nothing else trims these. A running service holds its log open and Windows
# will not rename a held file, so roll-logs.ps1 skips whatever is in use and
# succeeds on a later pass -- which, running here every five minutes, means
# shortly after any restart. That is the only moment it is safe anyway.
function Invoke-LogRollPass {
    $roller = Join-Path $PSScriptRoot 'roll-logs.ps1'
    if (-not (Test-Path -LiteralPath $roller)) { return }
    try {
        $out = & $roller -MaxMB 20 -Keep 2 2>&1
        foreach ($line in $out) {
            $text = [string]$line
            if ($text -and $text -notmatch '^nothing over' -and $text -notmatch '^skipped \(in use\)') {
                Write-WatchdogLog -Message ("logs: " + $text)
            }
        }
    } catch {
        Write-WatchdogLog -Message ("log roll failed: " + $_.Exception.Message) -Level 'WARN'
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Only one pass at a time. Task Scheduler fires this every five minutes and a
# pass used to take seconds, so overlap never came up; now that a dead HUD
# service costs up to SERVICE_BIND_TIMEOUT_SEC to give up on, a run with
# several of them down can outlast the interval. Two passes at once would
# both find the same port dead and both start the service, which is how you
# get duplicates of the thing you were trying to repair.
#
# A named system mutex rather than a lock file: it is released by the OS if
# the process is killed, so a crash cannot wedge the watchdog off.
$singleton = New-Object System.Threading.Mutex($false, 'Global\JarvisWatchdogPass')
$holdsLock = $false
try {
    $holdsLock = $singleton.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # The previous holder died without releasing. The lock is ours.
    $holdsLock = $true
}
if (-not $holdsLock) {
    Write-WatchdogLog -Message 'another watchdog pass is still running - skipping this one'
    exit 0
}

Write-WatchdogLog -Message ("watchdog-jarvis.ps1 starting (Once={0}, IntervalSec={1})" -f [bool]$Once, $IntervalSec)

try {
    if ($Once) {
        Invoke-WatchdogPass
        Write-WatchdogLog -Message 'watchdog-jarvis.ps1 single pass complete'
    } else {
        while ($true) {
            Invoke-WatchdogPass
            Start-Sleep -Seconds $IntervalSec
        }
    }
} finally {
    # Windows would release this on process exit anyway; doing it here means a
    # scheduled run that finishes early does not hold the next one off.
    try { $singleton.ReleaseMutex() } catch { }
    $singleton.Dispose()
}
