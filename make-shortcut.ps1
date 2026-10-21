# Put a Jarvis shortcut on the desktop.
#
# After this, starting Jarvis is a double-click: the bridge comes up, its
# window opens, and there is no terminal to find or command to remember.
#
#   powershell -ExecutionPolicy Bypass -File make-shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File make-shortcut.ps1 -Remove
#
# ASCII only on purpose: a .ps1 with smart quotes or dashes in it fails to
# parse on a machine whose console codepage is not UTF-8.

[CmdletBinding()]
param(
    [switch]$Remove,
    [int]$Port = 8100,
    [string]$Name = "Jarvis"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop ($Name + ".lnk")

if ($Remove) {
    if (Test-Path $link) {
        Remove-Item $link -Force
        Write-Output "Removed $link"
    } else {
        Write-Output "No shortcut at $link - nothing to remove."
    }
    exit 0
}

$starter = Join-Path $here "start.py"
if (-not (Test-Path $starter)) {
    Write-Error "start.py is not next to this script. Run it from inside the Jarvis folder."
}

# Prefer the launcher: it survives the user upgrading Python, which a path to
# a specific python.exe does not.
$python = $null
$launcher = Join-Path $env:WINDIR "py.exe"
if (Test-Path $launcher) {
    $python = $launcher
    $argPrefix = "-3 "
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Error "Python is not on PATH. Install it from python.org and tick 'Add Python to PATH'."
    }
    $python = $cmd.Source
    $argPrefix = ""
}

$icon = Join-Path $here "avatar\jarvis.ico"
if (-not (Test-Path $icon)) { $icon = $python }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $python
$sc.Arguments = $argPrefix + '"' + $starter + '" --port ' + $Port
$sc.WorkingDirectory = $here
$sc.IconLocation = $icon
$sc.Description = "Start Jarvis and open it in its own window"
# 7 = minimized. The console still exists, so closing it stops Jarvis
# properly, but it does not sit in front of the window you actually want.
$sc.WindowStyle = 7
$sc.Save()

Write-Output "Shortcut created: $link"
Write-Output "Double-click it to start Jarvis. Closing the small console window stops it."
