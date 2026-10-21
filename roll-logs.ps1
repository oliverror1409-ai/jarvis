<#
.SYNOPSIS
    Roll oversized Jarvis logs so they stop growing without bound.

.PARAMETER MaxMB
    Roll any log at or above this size (default 20).

.PARAMETER Keep
    How many rolled generations to keep per log (default 2).

.DESCRIPTION
    Nothing trimmed these. daemon.log had reached 16.5 MB and backend.log
    25.6 MB, and the only reason the pair was not larger is that the machine
    is rebuilt now and then. There is no rotation anywhere in the stack: both
    are written by PowerShell redirection from the launch scripts, so Python's
    RotatingFileHandler never sees them.

    A running service holds its log open, and Windows will not let a held
    file be renamed. That is fine and is the design: this skips a locked file
    and says so, then succeeds on the next pass after a restart releases the
    handle. Called every five minutes from the watchdog, rotation therefore
    lands shortly after any restart, which is exactly when it is safe.

    Rolled files are name.log.1, name.log.2, ... oldest discarded.
#>
[CmdletBinding()]
param(
    [int]$MaxMB = 20,
    [int]$Keep = 2
)

$ErrorActionPreference = 'Continue'
$logsDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path -LiteralPath $logsDir)) { return }

$limit = $MaxMB * 1MB

function Test-Locked {
    param([string]$Path)
    try {
        $s = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $s.Close()
        $s.Dispose()
        return $false
    } catch {
        return $true
    }
}

$rolled = 0
$skipped = 0

Get-ChildItem -LiteralPath $logsDir -Filter '*.log' -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Length -ge $limit } |
  ForEach-Object {
    $log = $_
    if (Test-Locked -Path $log.FullName) {
      Write-Output ("skipped (in use): {0}  {1:N1} MB" -f $log.Name, ($log.Length/1MB))
      $skipped++
      return
    }

    # Discard the oldest generation, then shuffle the rest down one.
    $oldest = "{0}.{1}" -f $log.FullName, $Keep
    if (Test-Path -LiteralPath $oldest) {
      Remove-Item -LiteralPath $oldest -Force -Confirm:$false -ErrorAction SilentlyContinue
    }
    for ($i = $Keep - 1; $i -ge 1; $i--) {
      $from = "{0}.{1}" -f $log.FullName, $i
      $to = "{0}.{1}" -f $log.FullName, ($i + 1)
      if (Test-Path -LiteralPath $from) {
        Move-Item -LiteralPath $from -Destination $to -Force -ErrorAction SilentlyContinue
      }
    }

    try {
      Move-Item -LiteralPath $log.FullName -Destination ("{0}.1" -f $log.FullName) -Force -ErrorAction Stop
      Write-Output ("rolled: {0}  {1:N1} MB" -f $log.Name, ($log.Length/1MB))
      $rolled++
    } catch {
      Write-Output ("could not roll {0}: {1}" -f $log.Name, $_.Exception.Message)
      $skipped++
    }
  }

if ($rolled -eq 0 -and $skipped -eq 0) {
  Write-Output ("nothing over {0} MB" -f $MaxMB)
}
