import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugins/telegram/desktop/plugin.js', import.meta.url), 'utf8')

test('Telegram Desktop source parses as an ES module', () => {
  // The Desktop loader imports a blob URL as ESM. Plain `node --check file.js`
  // may parse this extensionless package file as CommonJS and miss ESM errors.
  const result = spawnSync(process.execPath, ['--input-type=module', '--check'], {
    encoding: 'utf8',
    input: source
  })
  assert.equal(result.status, 0, result.stderr || result.stdout)
})

test('auth UI uses only approved runtime imports and reaches the auth endpoints', () => {
  // Runtime-loader rejects any package import beyond SDK/react; QR core must
  // be bundled inline rather than imported as a bare package.
  assert.doesNotMatch(source, /import\s*\(\s*['"]qrcode['"]\s*\)/)
  assert.match(source, /export function AuthPanel\(/)
  for (const endpoint of ['/auth/start-phone', '/auth/submit-code', '/auth/submit-password', '/auth/qr-start', '/auth/qr-poll', '/auth/password-recovery']) {
    assert.ok(source.includes(endpoint), `AuthPanel wires ${endpoint}`)
  }
  assert.match(source, /Sign in to Telegram/)
  assert.match(source, /invalidateQueries\(\{ queryKey: \[\.\.\.prefix, 'status'\]/)
  assert.match(source, /async function requestPasswordRecovery\(\)[\s\S]*?ctx\.rest\('\/auth\/password-recovery', \{ method: 'POST'/)
})

test('password-recovery UI posts to the start route and applies its returned auth stage', () => {
  const start = source.indexOf('async function requestPasswordRecovery()')
  const end = source.indexOf('\n  async function verifyPasswordRecovery()', start)
  const flow = source.slice(start, end)
  assert.match(flow, /ctx\.rest\('\/auth\/password-recovery', \{ method: 'POST', timeoutMs: 40000 \}\)/)
  assert.match(flow, /applyAuthState\(await ctx\.rest/)
  assert.match(flow, /finally \{ setBusy\(false\) \}/)
})

test('resend countdown ticks each second, clamps to zero, and cleans up its timer', () => {
  const countdown = loadFunction('startResendCountdown')
  let tick
  let delay
  let cancelled
  let state = 2
  const cleanup = countdown(update => { state = update(state) }, (callback, ms) => { tick = callback; delay = ms; return 42 }, id => { cancelled = id })
  assert.equal(delay, 1000)
  tick()
  assert.equal(state, 1)
  tick()
  assert.equal(state, 0)
  tick()
  assert.equal(state, 0)
  cleanup()
  assert.equal(cancelled, 42)
})

test('bundled login QR encoder returns a structurally valid matrix', () => {
  const start = source.indexOf('var QRCore=(()=>')
  const end = source.indexOf('\nfunction QrImage(', start)
  assert.ok(start > 0 && end > start, 'inline QR core and render wrapper are present')
  const code = source.slice(start, end)
    .replace('export function renderLoginQr', 'function renderLoginQr') +
    '\nglobalThis.renderLoginQr = renderLoginQr'
  const context = { TextEncoder }
  vm.runInNewContext(code, context)
  const matrix = context.renderLoginQr('tg://login?token=unit-test-token-1234567890')
  assert.ok(Array.isArray(matrix))
  assert.ok(matrix.length >= 21 && matrix.length <= 177 && matrix.length % 4 === 1)
  assert.ok(matrix.every(row => Array.isArray(row) && row.length === matrix.length && row.every(x => typeof x === 'boolean')))
  // Top-left finder pattern and its central 3x3 block are preserved.
  assert.equal(matrix[0][0], true)
  assert.equal(matrix[1][1], false)
  assert.equal(matrix[3][3], true)
})

test('every referenced module-level helper is declared', () => {
  // Regression: mountId was used by useState initializers but never declared,
  // which only surfaced as a runtime ReferenceError on page mount.
  assert.match(source, /(?:^|\n)(?:let|const|var)\s+mountId\b/)
  for (const name of ['stack', 'row', 'text', 'muted', 'DEFAULT_SETTINGS', 'ID']) {
    assert.match(source, new RegExp(`(?:^|\\n)(?:let|const|var)\\s+${name}\\b`), name)
  }
})

function loadFunction(name, nextName, prefix = '') {
  const start = source.indexOf(`export function ${name}(`)
  assert.notEqual(start, -1, `${name} is present`)
  const nextExport = source.indexOf('\nexport ', start + 1)
  const end = nextExport > start ? nextExport : source.length
  const fn = source.slice(start, end).replace(`export function ${name}`, `function ${name}`)
  return Function(`${prefix}\n${fn}\nreturn ${name}`)()
}

const defaults = 'const DEFAULT_SETTINGS = Object.freeze({ autoRefresh: false, unmutedOnly: false, unreadOnly: false, tab: \'all\' })\nconst settingsKey = (profile, me) => `telegram-settings:${profile}:${me}`'

test('settings default to manual refresh', () => {
  const load = loadFunction('loadSettings', 'unreadBadge', defaults)
  assert.deepEqual(load({ get: () => null }, 'k'), { autoRefresh: false, unmutedOnly: false, unreadOnly: false, tab: 'all' })
  assert.deepEqual(load({ get: () => ({ autoRefresh: true, unmutedOnly: true, unreadOnly: true, tab: '42', junk: 1 }) }, 'k'),
    { autoRefresh: true, unmutedOnly: true, unreadOnly: true, tab: '42' })
  assert.deepEqual(load({ get: () => ({ autoRefresh: 'yes', tab: 'nope' }) }, 'k'),
    { autoRefresh: false, unmutedOnly: false, unreadOnly: false, tab: 'all' })
  assert.deepEqual(load({ get: () => { throw new Error('storage locked') } }, 'k'),
    { autoRefresh: false, unmutedOnly: false, unreadOnly: false, tab: 'all' })
})

test('unread badge uses the verified Desktop contrast tokens and readable counts', () => {
  const start = source.indexOf('export function unreadBadge(')
  const end = source.indexOf('\nexport function filterDialogs(')
  const fn = source.slice(start, end).replace('export function unreadBadge', 'function unreadBadge')
  // Safe: the sliced body is our own shipped source (no untrusted input), the
  // only injected identifier is the jsx stub.
  const badge = new Function('jsx', `${fn}\nreturn unreadBadge`)((type, props) => ({ type, props }))
  assert.equal(badge(0, false), null)
  assert.equal(badge(-1, false), null)
  const hot = badge(5, false)
  assert.equal(hot.props['aria-label'], '5 unread', hot.props['aria-label'])
  assert.equal(hot.props.children, '5')
  const hotStyle = hot.props.style
  assert.equal(hotStyle.background, 'var(--dt-primary-solid)')
  assert.equal(hotStyle.color, 'var(--dt-primary-solid-foreground)')
  assert.equal(hotStyle.color.includes('--ui-accent-foreground'), false)
  assert.equal(hotStyle.border, undefined)
  const mutedPill = badge(7865, true)
  assert.match(mutedPill.props.style.border, /1px solid/)
  assert.equal(mutedPill.props.children, '99+') // 7865 -> 99+
  const small = badge(3, true)
  assert.equal(small.props.children, '3')
  assert.equal(badge(1, false).props.children, '1')
  assert.equal(badge(100, false).props.children, '99+')
})

test('dialog tabs filter by backend-returned stable folder IDs', () => {
  const filter = loadFunction('filterDialogs', 'Confirmation')
  const rows = [
    { key: '@a', unread: 0, muted: false, folderIds: ['all', '7'] },
    { key: '@b', unread: 4, muted: false, folderIds: ['all', '8', 'unread'] },
    { key: '@c', unread: 9, muted: true, folderIds: ['all', 'unread'] },
    { key: '@d', unread: 2, muted: true, folderIds: ['archive', 'unread'] },
  ]
  assert.equal(filter(rows, 'all', false).length, 3)
  assert.deepEqual(filter(rows, 'all', false, true).map(d => d.key), ['@b', '@c'])
  assert.deepEqual(filter(rows, 'unread', false).map(d => d.key), ['@b', '@c', '@d'])
  assert.deepEqual(filter(rows, '7', false).map(d => d.key), ['@a'])
  assert.deepEqual(filter(rows, '8', false).map(d => d.key), ['@b'])
  assert.deepEqual(filter(rows, 'archive', false).map(d => d.key), ['@d'])
  assert.deepEqual(filter(rows, 'all', true).map(d => d.key), ['@a', '@b'])
  assert.deepEqual(filter(rows, 'all', true, true).map(d => d.key), ['@b'])
  assert.equal(filter(null, 'all', false).length, 0)
  assert.equal(filter([null, false, 'x'], 'all', false).length, 0)
})

test('peer helpers produce safe links', () => {
  const link = loadFunction('buildTmeLink', 'TEXT_LIMIT')
  assert.equal(link('@username'), 'https://t.me/username')
  assert.equal(link('id:-1001234567890'), 'https://t.me/c/1234567890')
  assert.equal(link('id:123'), null)
  assert.equal(link('javascript:alert(1)'), null)
  assert.equal(link(''), null)
})

test('sender and date helpers degrade gracefully', () => {
  const nameOf = loadFunction('senderName', 'shortDate')
  const dateOf = loadFunction('shortDate', 'contextText')
  assert.equal(nameOf('Ada Lovelace <ada@example.com>'), 'Ada Lovelace')
  assert.equal(nameOf('@ada'), '@ada')
  assert.equal(nameOf(''), '(unknown sender)')
  assert.equal(dateOf(''), '')
  assert.equal(dateOf('not-a-date'), 'not-a-date')
  assert.match(dateOf('2026-10-05T12:34:56Z'), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
})

test('context copy marks data as untrusted', () => {
  const context = loadFunction('contextText', 'DEFAULT_SETTINGS')
  const text = context('me', { id: 1, text: 'hi' })
  assert.match(text, /UNTRUSTED TELEGRAM DATA/)
  assert.match(text, /"source": "telegram"/)
})
