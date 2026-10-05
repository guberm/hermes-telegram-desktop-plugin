# Hermes Desktop Telegram plugin

Один unified-пакет: Telegram-клиент для Hermes Desktop (UI), профиль-скоупед
dashboard backend и декларация агента. Пакет profile-safe: не содержит
OAuth-секретов, api_id/api_hash или файлов сессии — backend резолвит
авторизованную Telethon-сессию активного профиля на каждый запрос.

## Layout

- `plugins/telegram/plugin.yaml` + `__init__.py` — декларация агента (inert).
- `plugins/telegram/desktop/plugin.js` — Hermes Desktop runtime plugin
  (materialized в `~/.hermes/desktop-plugins/telegram/`, sidebars `/telegram`).
- `plugins/telegram/dashboard/manifest.json` + `plugin_api.py` — backend,
  монтируется на `/api/plugins/telegram/`.
- `tests/` — офлайн-тесты backend, контрактов пакета и ESM-модуля.

## Install

1. `plugins/telegram/` — целиком под `~/.hermes/plugins/telegram/` (или
   profile-локально под `~/.hermes/profiles/<name>/plugins/telegram/`).
2. Добавить `telegram` в `plugins.enabled` активного профиля (trust gate
   для dashboard backend, отдельный от переключателя в Desktop UI).
3. Desktop сам materializes `desktop/plugin.js` в `~/.hermes/desktop-plugins/telegram/`
   при старте — вручную копировать не нужно.
4. Перезапустить/перечитать плагины: ⌘K → **Reload desktop plugins**;
   в боковой панели появится строка **Telegram**, маршрут `/telegram`.

Сессия берётся из `~/.hermes/rss_reader/config.json` активного профиля
(`api_id`, `api_hash`, `session_path`), как в настроенном rss_reader/тг-боте.
Backend держит refresh в памяти, ничего не пишет на диск и не копирует секреты.

## Safety

- Чтение диалогов и истории — read-only. Вся история приходит с бэкенда
  санитизированной (Python HTMLParser: allowlist тегов, экранированный текст,
  только https-ссылки) и рендерится через React-элементы.
- Каждая мутация — **prepare → точный preview (JSON) → явный Confirm →
  commit → readback-проверка результата**. Тикет живёт 5 минут,
  привязан к scope и аккуанту, и требует `confirmed: true` (строго bool).
- Delete — обычное удаление выбранного сообщения (массовых/безвозвратных
  операций нет); подтверждение обязательно.
- Read state (mark-read) — только по явному Confirm; readback сверяет
  unread == 0.
- Отправка: точный preview (peer + текст), после commit — readback текста
  и reply-линковки.

## Checks

```
HERMES_SOURCE=~/.hermes/hermes-agent python -m unittest discover -s tests -v
node --test tests/*.mjs
HERMES_SOURCE=~/.hermes/hermes-agent python -m py_compile plugins/telegram/dashboard/plugin_api.py plugins/telegram/__init__.py
```

HTTP-роуты требуют живого рантайма; офлайн-тесты не делают сетевых вызовов
и не трогают живую сессию Telegram.
