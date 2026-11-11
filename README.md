# Личный медиа-бот для Telegram

Работает **только для тебя** (владелец = первый, кто написал `/start`, или `OWNER_ID` в `.env`).

## Что умеет
- 🔗 Ссылка YouTube / Instagram / TikTok / (и ~1000 других сайтов) → кнопки:
  - 🎬 видео в выбранном качестве (144p…2160p, с примерным размером)
  - 🎵 MP3 · 🎤 голосовое · ⭕ кружок
- 🎞 Своё видео → ⭕ кружок (до 60 сек, квадрат 640×640), 🎤 голосовое, 🎵 MP3
- 🎵 MP3 / любой звук → 🎤 голосовое
- Время в подписи (`1:25`) или в ссылке (`?t=85`) — кружок начнётся с этого места
- Слишком большое видео (>50 МБ) автоматически пережимается
- Повторный запрос того же — мгновенно (берётся из кэша Telegram)
- `/update` — обновить yt-dlp, если сайт перестал скачиваться
- Прислать файл `cookies.txt` — для закрытых/«подтвердите, что вы не бот» видео

## Бесплатный сервер (рекомендация)
**Oracle Cloud Always Free** — навсегда бесплатная VM (ARM, до 4 ядер / 24 ГБ ОЗУ).
Нужна карта только для проверки, деньги не списываются.

1. Регистрация: https://www.oracle.com/cloud/free/
2. Compute → Instances → Create: образ **Ubuntu 24.04**, форма **VM.Standard.A1.Flex** (Always Free),
   скачай SSH-ключ (`.key`).
3. На своём ПК в этой папке:
   ```powershell
   .\deploy.ps1 -Server ubuntu@IP_СЕРВЕРА -Key C:\путь\к\ключу.key
   ```
   Скрипт скопирует бота, поставит Docker и запустит. Бот перезапускается сам после сбоев/перезагрузки.

Альтернатива: Google Cloud e2-micro (Always Free, слабее).

Логи на сервере: `cd ~/tg-media-bot && sudo docker compose logs -f bot`

## Запуск на своём ПК (для проверки)
```powershell
.\setup_token.ps1
.\.venv\Scripts\python bot.py
```

## Файлы больше 50 МБ (до 2 ГБ) — по желанию
1. На https://my.telegram.org → API development tools получи `api_id` и `api_hash`.
2. Раскомментируй в `.env` строки `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `BOT_API_URL`.
3. Снова `deploy.ps1` — запустится локальный Bot API сервер.

## Если YouTube на сервере пишет «Sign in to confirm you're not a bot»
IP облаков часто блокируются YouTube. Установи в браузер расширение
«Get cookies.txt LOCALLY», экспортируй cookies с youtube.com (и instagram.com) в файл
`cookies.txt` и просто пришли его боту.

## Память и напоминания
- Все сообщения сохраняются в Supabase (таблица `tg_messages`), посмотреть: `/history`.
- «запомни что …» — сохраняет факт, список: `/memory`.
- «напомни 10 декабря сдать экзамен» — напомнит **7, 8 и 9 декабря** в `REMIND_HOUR` (по умолчанию 9:00 по `TZ`). Список: `/reminders`.
  Понимает также «завтра …», «послезавтра …», «через 3 дня …», «10.12 …».
- Свободный текст: если задан `ANTHROPIC_API_KEY`, бот отвечает как ИИ и учитывает то, что запомнил. Модель: `AI_MODEL` (по умолчанию `claude-haiku-5-5`).

### Переменные окружения
| Переменная | Что это |
|---|---|
| `SUPABASE_URL` | `https://xmrxkuqareicdslioxzo.supabase.co` |
| `SUPABASE_KEY` | **secret / service_role** ключ из Supabase → Project Settings → API. Не публичный ключ! |
| `ANTHROPIC_API_KEY` | (необязательно) ключ с console.anthropic.com |
| `TZ` | часовой пояс, например `Asia/Tashkent` |
| `REMIND_HOUR` | час напоминаний, по умолчанию 9 |
