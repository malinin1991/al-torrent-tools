# Telegram-боты AL Torrent Tools

Оба профиля реализованы в [`backend/app/telegram_bot/`](../backend/app/telegram_bot/) и используют общий backend-образ и PostgreSQL. В этом каталоге отдельного приложения нет.

- `primary` — работа с релизами и подписками, уведомления об изменениях.
- `hevc` — контроль HEVC, запросы доступа и уведомления одобренным получателям.

## Настройка

В веб-интерфейсе `/settings` включи нужный профиль, укажи его токен и при необходимости адрес Bot API. Настройки сохраняются в БД:

- основной профиль: `telegram_enabled`, `telegram_bot_token`, `telegram_bot_api_base_url`;
- HEVC: `telegram_hevc_enabled`, `telegram_hevc_bot_token`, `telegram_hevc_bot_api_base_url`.

Пустой Bot API URL означает `https://api.telegram.org`. Передавай корневой адрес API, без токена: приложение само добавляет `/bot` и токен. Управление запросами доступа HEVC доступно на `/telegram-access`.

Пока профиль выключен или токен не задан, процесс ожидает настройку и повторяет чтение конфигурации каждые 30 с. После изменения токена или Bot API URL уже работающего polling-процесса перезапусти соответствующий контейнер.

## Запуск

Compose содержит сервисы `telegram-bot` и `telegram-hevc-bot`; оба стартуют через общий entrypoint с миграциями под advisory lock.

Локально, после установки backend-зависимостей, настройки БД и применения миграций, из `backend/` с активированной venv запускай каждый нужный профиль в отдельном терминале:

```bash
python -m app.telegram_bot --profile primary
python -m app.telegram_bot --profile hevc
```

`--profile` имеет приоритет над переменной окружения `ALTT_TELEGRAM_PROFILE`; по умолчанию используется `primary`. Не запускай одновременно несколько polling-процессов с одним токеном.

## Проверка работы

Процесс обрабатывает Telegram updates через long polling и отправляет сообщения из DB outbox. По умолчанию outbox проверяется каждые 15 с, heartbeat обновляется каждые 30 с. Состояние видно на странице `/info`.

Health-файлы разделены: `/tmp/altt_telegram_healthy` и `/tmp/altt_telegram_hevc_healthy`. Compose проверяет их через `backend/scripts/healthcheck_telegram.py`. Heartbeat процесса сам по себе не подтверждает успешное подключение к Telegram.

Служебные переменные процесса: `ALTT_TELEGRAM_OUTBOX_INTERVAL_SEC`, `ALTT_TELEGRAM_HEARTBEAT_INTERVAL_SEC`, `ALTT_TELEGRAM_CONFIG_RETRY_SEC`, `ALTT_TELEGRAM_HEALTH_FILE`. Это переменные окружения процесса; не добавляй их в локальный `backend/.env`, который читает строгая модель `Settings` приложения.
