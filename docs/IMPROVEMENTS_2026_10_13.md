# Согласованные улучшения 1–3 и 5

Дата: 2026-10-13. Обычный CPython 3.15 и PostgreSQL 16. Реализация подготовлена локально; на Unraid не развёрнута.

## Реализовано

1. Атомарный старт pending → running, отдельный неблокирующий run-lock, условные stop/cancel/finalize и fresh reclaim с commit перед unlock. Терминальные задания не исполняются повторно; отмена приоритетнее partial/success.
2. Сохранённые счётчики и полный список проблем sync, partial, пагинация и явный ограниченный повтор server-validated целей. Неполный каталог блокирует sweep/baseline. Повтор проверяет source/ID/hash/роль, не подменяет неизвестную версию, не ставит общий checkpoint для subset и сохраняет baseline только совпавших целей.
3. Telegram claim/lease/token на одну запись, fencing, backoff/cooldown, отдельная обработка неопределённого исхода, явный ручной повтор и страница диагностики. Primary producer дедуплицирует событие, включая legacy history; обычный enqueue не обходит unknown. Receipt, primary tg_status и audit атомарны; HEVC не меняет primary status.
5. Рабочий список HEVC: exact версия, причина HEVC/SLA, доступность видимой страницы и квитанция Kensetsu. Общие ready/unavailable счётчики явно относятся к DB-снимку. Pairing один раз, FS только на странице, выбор/раскрытия/прокрутка сохраняются при SSE. Отправка использует прежнюю защиту повторов; проверяемый исходник и активный hash блокируют финальный POST.

Дизайн: [JobRunner](design/job-execution.md), [sync](design/sync-outcomes.md), [Telegram](design/telegram-delivery.md), [HEVC](design/hevc-workbench.md). Обоснование сохранения [PostgreSQL](design/data-store.md).

## Проверки

- До реализации: **1041 passed** на обычном 3.15 и временной PostgreSQL.
- Полный итоговый прогон после исправлений: **1288 passed, 0 skipped**, 14.55 с. Regular 3.15 ARM64 Docker-образ, текущий backend read-only, пустой dotenv, DATABASE_URL и ALTT_TEST_POSTGRES_URL только временного стенда.
- Реальные PostgreSQL-проверки: независимые соединения runner/reclaim/stop, concurrent Telegram consumers/producers, stale tokens, sync persistence/exact retries и существующие данные при 0025 → 0026 → 0025 → 0026. Уникальные схемы удалялись в finally.
- Оба исполняемых Node-теста реальных client scripts прошли: `tests/js/hevc_workbench.test.cjs`, `tests/js/operations.test.cjs`. Покрыты selection/version/SSE/details/scroll, cursor после afterSettle, job isolation и acknowledgement unknown.
- Browser smoke на отдельном API/БД и синтетических файлах: отображение HEVC/SLA/FS-расхождений, блокировка недоступного исходника, сохранение выбора/раскрытий после live refresh, диагностика Telegram и partial/счётчики/ошибки. Нативный confirm заблокировал дальнейшее управление тестовой вкладкой; сам confirm дополнительно проверен API/JS-тестами. Это ограничение browser automation, не успешная браузерная проверка подтверждения.
- Предупреждения: одно upstream FastAPI/httpx deprecation и четыре Alembic path_separator deprecation из upgrade/downgrade. Ошибок тестов нет; зависимости не обновлялись.

Live qB/Kensetsu/Telegram, рабочая БД и пользовательские MKV не использовались. Временный UI имел пустой dotenv, synthetic media и localhost-порт; worker и боты не запускались.

Синтаксис app/tests/worker проверен compileall на regular 3.15 с pycache внутри временного контейнера; node --check и git diff --check прошли. Временные API/PostgreSQL-контейнеры и обе тестовые сети удалены после проверки их метки владения; исходные Docker-образы сохранены.

## Независимый review

Peer/integration review выполнены отдельными агентами. Исправлены и повторно проверены: primary unknown producer bypass, неполный каталог/sweep, hashless retry, baseline от stale target, precedence счётчиков и metadata waiting. Итоговый integration review не нашёл обоснованных остаточных P1/P2; прежние изолированные repro больше не воспроизводятся.

Это **не Bugbot**. Навык review-bugbot требует специализированный subagent_type=bugbot, которого нет среди доступных инструментов этой сессии. UI-управление приложением также запрещено. Специализированный Bugbot не запускался; обычный независимый review его не подменяет.

## Обновление и границы

Миграция: `0026_job_outcomes_outbox`. Перед обновлением заменить старые API, worker и оба профиля ботов согласованно: старый код не соблюдает новые claims/dedupe. Entry point сериализует миграции существующей PostgreSQL-блокировкой.

Постоянная worker-очередь не добавлялась. Квитанция Kensetsu означает приём, а не завершение кодирования. Exactly-once внешней Telegram-доставки не обещается. Скорость нативного 5950X этой задачей не измерялась.
