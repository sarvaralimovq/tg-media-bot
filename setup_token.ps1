# Спрашивает токен бота и сохраняет в .env (токен видишь только ты)
Set-Location $PSScriptRoot
Write-Host "Вставь токен бота от @BotFather и нажми Enter:" -ForegroundColor Cyan
$sec = Read-Host -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)).Trim()
if ($token -notmatch '^\d+:[\w-]{30,}$') { Write-Host "Похоже, это не токен бота. Попробуй ещё раз." -ForegroundColor Red; exit 1 }
$owner = Read-Host "Твой Telegram ID (Enter = пропустить, владельцем станет первый написавший /start)"
"BOT_TOKEN=$token`nOWNER_ID=$owner`n" | Out-File -Encoding ascii .env
Write-Host "Сохранено в .env" -ForegroundColor Green
