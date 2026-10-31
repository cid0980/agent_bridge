<#
  agent-bridge — installer for Windows (PowerShell)
  Usage:  cd into the cloned repo, then:   powershell -ExecutionPolicy Bypass -File install.ps1
#>
$ErrorActionPreference = "Stop"

$InstallDir = "$env:USERPROFILE\local-mcp-bridge"
Write-Host "▶ agent-bridge v3 installer (Windows)"

# 1. files
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item "$PSScriptRoot\*.py", "$PSScriptRoot\requirements.txt" $InstallDir -Force
Set-Location $InstallDir

# 2. python venv
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "Python not found. Install from https://python.org (check 'Add python to PATH')" -ForegroundColor Yellow
  exit 1
}
python -m venv .venv
$Py = "$InstallDir\.venv\Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r requirements.txt
Write-Host "✓ dependencies installed"

# 3. token
$TokenFile = "$InstallDir\.bridge_token"
if (Test-Path $TokenFile) { $Token = Get-Content $TokenFile }
else {
  $Token = -join ((48..57 + 97..102) | Get-Random -Count 32 | ForEach-Object {[char]$_})
  $Token | Out-File -NoNewline -Encoding ascii $TokenFile
}
Write-Host "bridge token: $Token  (stored in .bridge_token)"

# 4. cloudflared (optional but recommended)
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  try { winget install --id Cloudflare.cloudflared -e -h } catch {
    Write-Host "install cloudflared manually: https://github.com/cloudflare/cloudflared" -ForegroundColor Yellow
  }
}

# 5. run at login = startup-folder shortcut (simple + visible)
$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\agent-bridge.lnk")
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-WindowStyle Hidden -Command `"`$env:BRIDGE_TOKEN='$Token'; & '$Py' '$InstallDir\bridge_v3.py'`""
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.Save()
Write-Host "✓ bridge will start at login"

# 6. go
Write-Host ""
Write-Host "✅ install complete."
Write-Host "  start now    : powershell -WindowStyle Hidden -Command `"`$env:BRIDGE_TOKEN='$Token'; & '$Py' '$InstallDir\bridge_v3.py'`""
Write-Host "  open tunnel  : cloudflared tunnel --url http://127.0.0.1:8765"
Write-Host "  then paste the https://...trycloudflare.com URL + token to your agent."
