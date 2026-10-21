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

# pythonw runs with no console at all, which is the point: the shortcut
# starts Jarvis in the background and nothing is left on the taskbar. pyw.exe
# is the launcher's windowless twin and survives a Python upgrade, which a
# path to one pythonw.exe does not.
$python = $null
$argPrefix = ""
$pyw = Join-Path $env:WINDIR "pyw.exe"
if (Test-Path $pyw) {
    $python = $pyw
    $argPrefix = "-3 "
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Error "Python is not on PATH. Install it from python.org and tick 'Add Python to PATH'."
    }
    # Same interpreter, windowless build, which sits beside python.exe.
    $candidate = Join-Path (Split-Path $cmd.Source -Parent) "pythonw.exe"
    $python = if (Test-Path $candidate) { $candidate } else { $cmd.Source }
}

$icon = Join-Path $here "avatar\jarvis.ico"
if (-not (Test-Path $icon)) { $icon = $python }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $python
$sc.Arguments = $argPrefix + '"' + $starter + '" --port ' + $Port + ' --background' 
$sc.WorkingDirectory = $here
$sc.IconLocation = $icon
$sc.Description = "Start Jarvis and open it"
# 1 = normal. There is no console to hide now: pythonw has none, and the
# bridge is detached, so the shortcut starts it and goes away.
$sc.WindowStyle = 1
$sc.Save()

Write-Output "Shortcut created: $link"
Write-Output "Double-click it to start Jarvis. No console window is left behind."
Write-Output "To stop it: QUIT inside the app, or 'python start.py --stop' here."
