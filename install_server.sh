#!/usr/bin/env bash
# Запускается НА СЕРВЕРЕ (Ubuntu/Debian): ставит Docker и запускает бота
set -e
cd "$(dirname "$0")"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
[ -f .env ] || { echo "Нет файла .env с BOT_TOKEN"; exit 1; }
mkdir -p data
if grep -q '^BOT_API_URL=' .env; then
  sudo docker compose --profile bigfiles up -d --build
else
  sudo docker compose up -d --build
fi
sudo docker compose logs --tail=20 bot
echo "Готово. Бот работает и сам перезапустится после перезагрузки сервера."
