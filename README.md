# Hermes Desktop Telegram plugin

One unified package: a Telegram client for Hermes Desktop (UI), a
profile-scoped dashboard backend, and an agent declaration. The package is
profile-safe: it contains no OAuth secrets, api_id/api_hash, or session
files — the backend resolves the active profile's authorized Telethon
session on every request.

## Layout

- `plugins/telegram/plugin.yaml` + `__init__.py` — agent declaration (inert).
- `plugins/telegram/desktop/plugin.js` — Hermes Desktop runtime plugin
  (materialized into `~/.hermes/desktop-plugins/telegram/`, sidebar `/telegram`).
- `plugins/telegram/dashboard/manifest.json` + `plugin_api.py` — backend,
  mounted on `/api/plugins/telegram/`.
- `tests/` — offline tests for the backend, package contracts, and the ESM module.

## Install

1. Copy `plugins/telegram/` as a whole to `~/.hermes/plugins/telegram/` (or
   profile-locally to `~/.hermes/profiles/<name>/plugins/telegram/`).
2. Add `telegram` to `plugins.enabled` of the active profile (the trust gate
   for the dashboard backend, separate from the Desktop UI toggle).
3. **Runtime dependency**: the Hermes desktop/dashboard backend executes
   `plugin_api.py` in its own env (e.g.
   `~/.hermes/installs/<id>/environments/<env>/venv`). Telethon must be
   installed into exactly that env, otherwise the routes mount but every
   request returns 502 "backend unavailable":
   ```
   uv pip install --python ~/.hermes/installs/<id>/environments/<env>/venv/bin/python telethon
   ```
4. Desktop materializes `desktop/plugin.js` into `~/.hermes/desktop-plugins/telegram/`
   on start by itself — no manual copy needed.
5. Restart/reload plugins: ⌘K → **Reload desktop plugins**;
   a **Telegram** row appears in the sidebar, route `/telegram`.

With an unauthorized dedicated session, the page itself shows a login panel:
phone login (code, then the 2FA password if configured) or QR. The QR encoder
is inlined in `desktop/plugin.js` because the Desktop runtime only allows
imports from `@hermes/plugin-sdk` and `react`. Embedded encoder —
`qrcode@1.5.4` (MIT); license text: `LICENSES/qrcode-MIT.txt`.

`api_id` and `api_hash` are read from the active profile's
`~/.hermes/rss_reader/config.json`; `session_path` from that file is not
used. The login flow and all plugin API calls use only the dedicated session
`~/.hermes/telegram-plugin-auth/telegram-plugin-auth.session`. Phone-code/QR
challenge values are held in process memory only, and service state is
written with `0600` permissions; a successful session remains in a separate
Telethon file. A missing/unauthorized dedicated session fails closed with an
error — no fallback to the RSS reader.

## Safety

- Reading dialogs and history is read-only. All history arrives sanitized
  from the backend (Python HTMLParser: tag allowlist, escaped text, https
  links only) and is rendered through React elements.
- Every mutation — **prepare → exact preview (JSON) → explicit Confirm →
  commit → readback verification of the result**. A ticket lives 5 minutes,
  is bound to scope and account, and requires `confirmed: true` (strict bool).
- Delete — ordinary deletion of the selected message (no bulk/irreversible
  operations); confirmation is mandatory.
- Read state (mark-as-read) — only on explicit Confirm; the readback checks
  unread == 0.
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
