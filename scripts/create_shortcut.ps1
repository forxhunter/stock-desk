# Creates a "Stock Desk" shortcut on the desktop that starts the app without a console window.
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { Write-Error "Run setup.bat first."; exit 1 }
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "Stock Desk.lnk"))
$lnk.TargetPath = $pythonw
$lnk.Arguments = "-m stockdesk"
$lnk.WorkingDirectory = $root
$lnk.Description = "Stock Desk - read-only stock analysis widget"
$ico = Join-Path $root "assets\stockdesk.ico"
if (Test-Path $ico) { $lnk.IconLocation = $ico }
$lnk.Save()
Write-Output "Created: $(Join-Path $desktop 'Stock Desk.lnk')"
