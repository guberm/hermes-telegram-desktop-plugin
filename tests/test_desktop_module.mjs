import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import test from 'node:test'

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

const defaults = 'const DEFAULT_SETTINGS = Object.freeze({ autoRefresh: false, unmutedOnly: false, tab: \'all\' })\nconst settingsKey = (profile, me) => `telegram-settings:${profile}:${me}`'

test('settings default to manual refresh', () => {
  const load = loadFunction('loadSettings', 'unreadBadge', defaults)
  assert.deepEqual(load({ get: () => null }, 'k'), { autoRefresh: false, unmutedOnly: false, tab: 'all' })
  assert.deepEqual(load({ get: () => ({ autoRefresh: true, unmutedOnly: true, tab: 'unread', junk: 1 }) }, 'k'),
    { autoRefresh: true, unmutedOnly: true, tab: 'unread' })
  assert.deepEqual(load({ get: () => ({ autoRefresh: 'yes', tab: 'nope' }) }, 'k'),
    { autoRefresh: false, unmutedOnly: false, tab: 'all' })
  assert.deepEqual(load({ get: () => { throw new Error('storage locked') } }, 'k'),
    { autoRefresh: false, unmutedOnly: false, tab: 'all' })
})

test('unread badge is high-contrast and mutes aware', () => {
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
  assert.match(hotStyle.background, /var\(--ui-accent\)$/)
  assert.equal(hotStyle.color.startsWith('var('), true, hotStyle.color)
  assert.equal(hotStyle.border, undefined)
  const mutedPill = badge(7865, true)
  assert.match(mutedPill.props.style.border, /1px solid/)
  assert.equal(mutedPill.props.children, '99+') // 7865 -> 99+
  const small = badge(3, true)
  assert.equal(small.props.children, '3')
})

test('dialog tabs and unmuted-only filter like the native client', () => {
  const filter = loadFunction('filterDialogs', 'Confirmation')
  const rows = [
    { key: '@a', unread: 0, muted: false },
    { key: '@b', unread: 4, muted: false },
    { key: '@c', unread: 9, muted: true },
    { key: '@d', unread: 2, muted: true },
  ]
  assert.equal(filter(rows, 'all', false).length, 4)
  assert.deepEqual(filter(rows, 'unread', false).map(d => d.key), ['@b', '@c', '@d'])
  assert.deepEqual(filter(rows, 'all', true).map(d => d.key), ['@a', '@b'])
  assert.deepEqual(filter(rows, 'unread', true).map(d => d.key), ['@b'])
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
