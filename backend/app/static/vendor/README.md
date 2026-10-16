# Локальные зависимости UI

HTMX **1.9.12** обслуживается самим приложением, чтобы формы и live-обновления работали без доступа браузера к CDN. Версия сохранена прежней; frontend build не требуется.

- Файл: `htmx-1.9.12.min.js`.
- Источник: [официальный dist тега v1.9.12](https://github.com/bigskysoftware/htmx/blob/v1.9.12/dist/htmx.min.js).
- Лицензия: [htmx-LICENSE.txt](htmx-LICENSE.txt), копия из того же тега.
- SHA-256: `449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452`.
- SHA-384 (base64): `ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2`.

SHA-384 сверена с [официальной документацией HTMX 1.x](https://v1.htmx.org/docs/#installing). При обновлении заменять dist и лицензию вместе, обновлять версию подключения в `templates/base.html`, проверять сценарии `tests/js/*.test.cjs` и браузерные HTMX/SSE-обновления.
