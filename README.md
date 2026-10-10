# Hermes Desktop Telegram plugin

One unified package: a Telegram client for Hermes Desktop (UI), a
profile-scoped dashboard backend, and an agent declaration. The package is
profile-safe: it contains no OAuth secrets, api_id/api_hash, or session
files — the backend resolves the active profile's authorized Telethon
session on every request.

## Layout

- `plugins/telegram/plugin.yaml` + `__init__.py` — agent declaration (inert).
- `plugins/telegram/desktop/plugin.js` — Hermes Desktop runtime plugin
  (materialized into `~/.hermes/desktop-plugins/telegram-client/`, sidebar route `/telegram-client`).
- `plugins/telegram/dashboard/manifest.json` + `plugin_api.py` — backend,
  mounted on `/api/plugins/telegram-client/`.
- `tests/` — offline tests for the backend, package contracts, and the ESM module.

## Install

1. Copy `plugins/telegram/` as a whole to `~/.hermes/plugins/telegram-client/` (or
   profile-locally to `~/.hermes/profiles/<name>/plugins/telegram-client/`).
2. Add `telegram-client` to `plugins.enabled` of the active profile (the trust gate
   for the dashboard backend, separate from the Desktop UI toggle).
3. Desktop materializes `desktop/plugin.js` into `~/.hermes/desktop-plugins/telegram-client/`
   on start by itself — no manual copy needed.
4. Restart/reload plugins: ⌘K → **Reload desktop plugins**;
   a **Telegram** row appears in the sidebar, route `/telegram-client`.

With an unauthorized dedicated session, the page itself shows a login panel:
phone login (code, then the 2FA password if configured) or QR. The backend
builds the QR matrix; the Desktop pane only paints it.

`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are read through the profile's Hermes
secrets (`agent.secret_scope.get_secret`, so the dashboard route's profile
scope and a single-profile `.env` both work). No other plugin's or tool's
config file is consulted. The login flow and all plugin API calls use only the dedicated session
`~/.hermes/telegram-plugin-auth/telegram-plugin-auth.session`. Phone-code/QR
challenge values are held in process memory only, the auth state file and the
Telethon session artifacts (`.session`, plus SQLite's `-wal`/`-shm` sidecars)
are restricted to `0600`; a successful session remains in a separate
Telethon file. A missing/unauthorized dedicated session fails closed with an
error — no fallback to the RSS reader.

## Safety

- Reading dialogs and history is read-only. All history arrives sanitized
  from the backend (Python HTMLParser: tag allowlist, escaped text, https
  links only) and is rendered through React elements.
- Every mutation — **prepare → exact preview (JSON) → explicit Confirm →
  commit → readback verification of the result**. A ticket lives 5 minutes,
  is bound to scope and account, and requires `confirmed: true` (strict bool).
- Delete — deletes the selected message for everyone (`revoke=True`); confirmation is mandatory.
- Read state (mark-as-read) — only on explicit Confirm; the readback verifies
  the cursor advanced through the selected message, while newer messages may
  remain unread.
- Sending: exact preview (peer + text), after commit — readback of the text
  and the reply link.

## Checks

```
HERMES_SOURCE=~/.hermes/hermes-agent python -m unittest discover -s tests -v
node --test tests/*.mjs
HERMES_SOURCE=~/.hermes/hermes-agent python -m py_compile plugins/telegram/dashboard/plugin_api.py plugins/telegram/__init__.py
```

HTTP routes require a live runtime; the offline tests make no network calls
and do not touch the live Telegram session.
