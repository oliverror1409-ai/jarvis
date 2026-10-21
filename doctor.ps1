# Jarvis doctor - the faults that hide.
#
# Everything that went wrong today was silent. The login had been dead for
# days and the CLI hung on it rather than erroring; the NIM fallback pointed
# at models NVIDIA had retired; home was set to a city four hundred
# kilometres away; the allowance probe timed out on a slow success. None of
# it announced itself - he found each one by noticing a bad answer.
#
# So this checks for exactly those classes of fault, and anything else that
# is quietly wrong, and says so in one screen.
#
#     .\doctor.ps1            # check
#     .\doctor.ps1 -Quiet     # only problems
#
# ASCII only, deliberately: PowerShell 5.1 reads .ps1 as ANSI, and a stray
# em-dash turns the whole file into a parse error.

param([switch]$Quiet)

$problems = @()

function Say([string]$state, [string]$text) {
    $mark = switch ($state) { 'ok' { '  OK  ' } 'warn' { ' WARN ' } default { ' FAIL ' } }
    $colour = switch ($state) { 'ok' { 'Green' } 'warn' { 'Yellow' } default { 'Red' } }
    if ($state -eq 'ok' -and $Quiet) { return }
    Write-Host "[$mark] " -ForegroundColor $colour -NoNewline
    Write-Host $text
}

Write-Host ""
Write-Host "=== JARVIS DOCTOR ===" -ForegroundColor Cyan

# ---- services --------------------------------------------------------------
$services = @(
    @{ name = 'Bridge';  url = 'http://127.0.0.1:8100/health' },
    @{ name = 'Daemon';  url = 'http://127.0.0.1:8123/health' },
    @{ name = 'Backend'; url = 'http://127.0.0.1:8000/health' }
)
foreach ($s in $services) {
    try {
        Invoke-RestMethod $s.url -TimeoutSec 6 | Out-Null
        Say ok "$($s.name) answering"
    } catch {
        Say fail "$($s.name) is not answering ($($s.url))"
        $problems += ("{0} down - restart-jarvis.ps1 -Component {1}" -f $s.name, $s.name.ToLower())
    }
}

# ---- the login that hangs rather than fails --------------------------------
$creds = Join-Path $env:USERPROFILE '.claude\.credentials.json'
if (Test-Path $creds) {
    try {
        $oauth = (Get-Content $creds -Raw | ConvertFrom-Json).claudeAiOauth
        $expires = [double]($oauth.expiresAt)
        $nowSec = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        if (-not $oauth.accessToken -or $expires -le 0) {
            Say fail "Claude login is DEAD - Jarvis is running on NIM fallback"
            $problems += "Run: claude login   (restores full model quality)"
        } elseif (($expires / 1000) -lt $nowSec) {
            Say fail "Claude login has expired"
            $problems += "Run: claude login"
        } else {
            $when = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$expires).LocalDateTime
            Say ok "Claude login valid until $when"
        }
    } catch {
        Say warn "credentials file unreadable"
    }
} else {
    Say fail "no Claude credentials file"
    $problems += "Run: claude login"
}

# ---- round-trip latency, which is what he actually feels -------------------
try {
    $body = @{ model = 'claude-sonnet-5'
               messages = @(@{ role = 'user'; content = 'Reply with exactly: ok' }) } | ConvertTo-Json -Depth 5
    $t0 = Get-Date
    Invoke-RestMethod 'http://127.0.0.1:8100/v1/chat/completions' -Method Post `
        -ContentType 'application/json' -Body $body -TimeoutSec 90 | Out-Null
    $secs = [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    if ($secs -gt 20) {
        Say fail "a one-word answer took $secs s"
        $problems += "Brain is slow ($secs s) - check the NIM lanes and the login"
    } elseif ($secs -gt 8) {
        Say warn "a one-word answer took $secs s"
    } else {
        Say ok "brain answers in $secs s"
    }
} catch {
    Say fail "the brain did not answer at all"
    $problems += "No answer from /v1/chat/completions - see logs\bridge.log"
}

# ---- NIM lanes: models get retired without warning -------------------------
$nimFile = Join-Path $env:USERPROFILE '.config\jarvis\nim-keys.json'
if (Test-Path $nimFile) {
    $nim = Get-Content $nimFile -Raw | ConvertFrom-Json
    $lanes = @()
    foreach ($mode in 'chat', 'fast', 'bg') {
        $v = $nim.routing.$mode
        if ($v -is [array]) { $lanes += $v } elseif ($v) { $lanes += ($v -split '\s+') }
    }
    $lanes = $lanes | Where-Object { $_ } | Select-Object -Unique
    $key = if ($nim.keys[0].key) { $nim.keys[0].key } else { $nim.keys[0] }
    # Twice before condemning, and 'gone' told apart from 'busy'. A model
    # NVIDIA has retired (404/410) needs the routing rewired; one that is
    # merely overloaded (503/429) needs nothing but the next lane, which
    # the bridge already does. Reporting the second as the first is how a
    # doctor teaches people to ignore it.
    $retired = @()
    $busy = @()
    foreach ($model in $lanes) {
        $answered = $false
        $lastCode = 0
        foreach ($attempt in 1, 2) {
            try {
                $probe = @{ model = $model; max_tokens = 4
                            messages = @(@{ role = 'user'; content = 'ok' }) } | ConvertTo-Json -Depth 5
                Invoke-RestMethod 'https://integrate.api.nvidia.com/v1/chat/completions' -Method Post `
                    -Headers @{ Authorization = "Bearer $key" } -ContentType 'application/json' `
                    -Body $probe -TimeoutSec 20 | Out-Null
                $answered = $true
                break
            } catch {
                if ($_.Exception.Response) { $lastCode = $_.Exception.Response.StatusCode.value__ }
            }
        }
        if (-not $answered) {
            if ($lastCode -eq 404 -or $lastCode -eq 410) { $retired += $model } else { $busy += $model }
        }
    }
    if ($retired.Count -eq 0 -and $busy.Count -eq 0) {
        Say ok "all $($lanes.Count) NIM lanes answer"
    } else {
        if ($busy.Count -gt 0) {
            Say warn "NIM lanes busy right now (will retry, next lane covers it): $($busy -join ', ')"
        }
        if ($retired.Count -gt 0) {
            Say fail "NIM models RETIRED by NVIDIA: $($retired -join ', ')"
            $problems += "Retired NIM models - re-probe the catalog and rewire routing"
        }
    }
} else {
    Say warn "no NIM keys - there is no fallback if Claude is down"
}

# ---- home, because a wrong one makes every local answer wrong --------------
$cfgPath = Join-Path $env:USERPROFILE '.config\jarvis\config.json'
if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    if ($cfg.location_city) {
        Say ok "home is $($cfg.location_city) ($($cfg.location_latitude), $($cfg.location_longitude))"
    } else {
        Say warn "no home set - local answers will guess from the VPN exit"
        $problems += "Set location_city / location_latitude / location_longitude in config.json"
    }
}

# ---- allowance meter -------------------------------------------------------
try {
    $u = Invoke-RestMethod 'http://127.0.0.1:8100/usage' -TimeoutSec 60
    if ($u.ok) {
        $stale = ''
        if ($u.stale) { $stale = ' (stale)' }
        Say ok "allowances: session $($u.session_utilization)%, week $($u.week_utilization)%$stale"
    } else {
        Say warn "allowance meter: $($u.error)"
    }
} catch {
    Say warn "allowance meter did not answer"
}

# ---- disk, because renders fill it -----------------------------------------
$drive = Get-PSDrive C
$freeGB = [Math]::Round($drive.Free / 1GB)
if ($freeGB -lt 40) {
    Say fail "only $freeGB GB free on C: - renders will start failing"
    $problems += "Free disk space on C:"
} elseif ($freeGB -lt 120) {
    Say warn "$freeGB GB free on C:"
} else {
    Say ok "$freeGB GB free on C:"
}

# ---- camera, so AR's silence is explained ----------------------------------
$cams = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.PNPClass -eq 'Camera' }
if ($cams) {
    Say ok "camera present: $(($cams | Select-Object -First 1).Name)"
} else {
    Say warn "no camera - AR hand tracking cannot run (the rest of AR works)"
}

# ---- the tool surface, because a broken tool says nothing ------------------
$toolCheck = Join-Path $PSScriptRoot 'tool-check.py'
$mamba = Join-Path $PSScriptRoot 'VoiceJarvisSrc\.mamba_env\python.exe'
if ((Test-Path $toolCheck) -and (Test-Path $mamba)) {
    # Read the counts the checker prints, rather than counting the phrase:
    # the summary line "0 will not construct" contains the phrase itself,
    # so matching on text reported a fault when there was none.
    $out = & $mamba $toolCheck 2>&1 | Out-String
    $m = [regex]::Match($out, '(\d+) will not construct')
    $broken = if ($m.Success) { [int]$m.Groups[1].Value } else { 0 }
    $m2 = [regex]::Match($out, '(\d+) with schema faults')
    $schema = if ($m2.Success) { [int]$m2.Groups[1].Value } else { 0 }
    $m3 = [regex]::Match($out, '(\d+) tools inspected')
    $total = if ($m3.Success) { $m3.Groups[1].Value } else { '?' }
    if ($broken -eq 0 -and $schema -eq 0) {
        Say ok "$total tools: none broken, no schema faults"
    } else {
        Say fail "$broken tools will not construct, $schema with schema faults"
        $problems += "Run: python tool-check.py --run   (see which tools are broken)"
    }
}

# ---- what to do ------------------------------------------------------------
Write-Host ""
if ($problems.Count -eq 0) {
    Write-Host "Nothing wrong that this can see." -ForegroundColor Green
} else {
    Write-Host "TO FIX:" -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" }
}
Write-Host ""
