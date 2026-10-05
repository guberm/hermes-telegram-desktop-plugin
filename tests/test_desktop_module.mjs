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

function loadFunction(name, nextName, prefix = '') {
  const start = source.indexOf(`export function ${name}(`)
  assert.notEqual(start, -1, `${name} is present`)
  const nextExport = source.indexOf('\nexport ', start + 1)
  const end = nextExport > start ? nextExport : source.length
  const fn = source.slice(start, end).replace(`export function ${name}`, `function ${name}`)
  return Function(`${prefix}\n${fn}\nreturn ${name}`)()
}

const defaults = 'const DEFAULT_SETTINGS = Object.freeze({ autoRefresh: false })'

test('settings default to manual refresh', () => {
  const load = loadFunction('loadSettings', 'Confirmation', defaults)
  assert.deepEqual(load({ get: () => null }, 'k'), { autoRefresh: false })
  assert.deepEqual(load({ get: () => ({ autoRefresh: true, junk: 1 }) }, 'k'), { autoRefresh: true })
  assert.deepEqual(load({ get: () => ({ autoRefresh: 'yes' }) }, 'k'), { autoRefresh: false })
  assert.deepEqual(load({ get: () => { throw new Error('storage locked') } }, 'k'), { autoRefresh: false })
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
