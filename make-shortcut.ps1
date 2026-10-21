# Put a Jarvis shortcut on the desktop.
#
#   powershell -ExecutionPolicy Bypass -File make-shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File make-shortcut.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File make-shortcut.ps1 -Port 8200
#
# This hands the work to start.py on purpose. An earlier version wrote the
# shortcut itself and pointed it at pyw.exe -- the py launcher -- which picks
# whichever Python it considers newest. That is routinely NOT the one `pip
# install` used: on the machine this was found on, `python` was a 3.11 holding
# fastapi and uvicorn while `py -3` was a 3.14 holding neither. The shortcut
# launched the wrong interpreter, the bridge could not import its packages and
# died at once, and with no console there was nothing to see. It looked for all
# the world like "Jarvis only works from the command prompt".
#
# start.py knows which interpreter is running it, so a shortcut it writes
# cannot point at the wrong one. It also refuses to write one at all until the
# packages are actually present.
#
# ASCII only on purpose: a .ps1 with smart quotes in it fails to parse on a
# machine whose console codepage is not UTF-8.

[CmdletBinding()]
param(
    [switch]$Remove,
    [int]$Port = 8100
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$starter = Join-Path $here "start.py"
if (-not (Test-Path $starter)) {
    Write-Error "start.py is not next to this script. Run it from inside the Jarvis folder."
}

$cmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $cmd) {
    Write-Error "Python is not on PATH. Install it from python.org and tick 'Add Python to PATH'."
}

# Whatever `python` means in this shell is what the person used for pip, so it
# is the right interpreter to hand this to.
$flag = if ($Remove) { "--remove-shortcut" } else { "--shortcut" }
& $cmd.Source $starter $flag --port $Port
exit $LASTEXITCODE
