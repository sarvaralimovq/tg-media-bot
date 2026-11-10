# Запускается на твоём компьютере: копирует бота на сервер и запускает.
# Пример: .\deploy.ps1 -Server ubuntu@123.45.67.89 -Key C:\Users\User\.ssh\oracle.key
param(
  [Parameter(Mandatory=$true)][string]$Server,
  [string]$Key = ""
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .env)) { Write-Host "Сначала запусти setup_token.ps1"; exit 1 }
$sshArgs = @(); if ($Key) { $sshArgs = @("-i", $Key) }
& ssh @sshArgs $Server "mkdir -p ~/tg-media-bot"
& scp @sshArgs bot.py requirements.txt Dockerfile docker-compose.yml .dockerignore install_server.sh .env "${Server}:~/tg-media-bot/"
& ssh @sshArgs $Server "cd ~/tg-media-bot && sed -i 's/\r$//' install_server.sh .env && bash install_server.sh"
