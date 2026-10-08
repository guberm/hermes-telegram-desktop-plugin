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

// buildMessageTmeLink calls buildTmeLink, so extract both together (with their
// `export` keywords stripped) so the helper runs with its dependency in scope.
const buildTmeLinkPair = source
  .slice(source.indexOf('export function buildTmeLink('), source.indexOf('\nexport function consumeDialogsRequestPath('))
  .replace('export function buildTmeLink', 'function buildTmeLink')
  .replace('export function buildMessageTmeLink', 'function buildMessageTmeLink')
const buildMessageTmeLinkImpl = new Function(`${buildTmeLinkPair}\nreturn buildMessageTmeLink`)()

// mediaLabel depends on the MEDIA_LABELS const that precedes it; slice both.
const mediaLabelImpl = new Function(
  `${source.slice(source.indexOf('const MEDIA_LABELS ='), source.indexOf('\nexport function contextText('))
    .replace('export function mediaLabel', 'function mediaLabel')}\nreturn mediaLabel`,
)()

const defaults = 'const DEFAULT_SETTINGS = Object.freeze({ autoRefresh: false, unmutedOnly: false, unreadOnly: false, confirmActions: true, tab: \'all\' })\nconst settingsKey = (profile, me) => `telegram-settings:${profile}:${me}`'

function makeTelegramPane(dialogs, messages = []) {
  const slots = []
  const restCalls = []
  let slot = 0
  const jsx = (type, props = {}, key) => ({ type, props, key })
  const useState = initial => {
    const index = slot++
    if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
    return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value }]
  }
  const useRef = initial => {
    const index = slot++
    if (!(index in slots)) slots[index] = { current: initial }
    return slots[index]
  }
  const useEffect = () => { slot++ }
  const useQuery = ({ queryKey }) => queryKey.includes('dialogs')
    ? { data: { dialogs, folders: [{ id: 'all', title: 'All' }] }, isPending: false, isError: false, isFetching: false }
    : { data: { topics: [], messages }, isPending: false, isError: false, isFetching: false }
  const useMutation = ({ mutationFn }) => ({ isPending: false, reset() {}, mutateAsync: mutationFn })
  const testScope = 's'.repeat(32)
  const rest = async (path, options = {}) => {
    restCalls.push({ path, options })
    if (path === '/actions/prepare') return { scope: testScope, confirmationToken: 'token', preview: {} }
    if (path === '/actions/commit') return { status: 'verified', peer: '@alice' }
    return {}
  }
  const badgeSource = source.slice(source.indexOf('export function unreadBadge('), source.indexOf('\nexport function filterDialogs('))
  const badge = new Function('jsx', `${badgeSource.replace('export function unreadBadge', 'function unreadBadge')}\nreturn unreadBadge`)(jsx)
  const componentStart = source.indexOf('function TelegramPane(')
  const componentEnd = source.indexOf('\nfunction Connected(', componentStart)
  const paneSource = source.slice(componentStart, componentEnd)
  // Only trusted repo source is compiled; dialog fixtures never enter function text.
  const Pane = new Function(
    'jsx', 'jsxs', 'useState', 'useRef', 'useEffect', 'useQueryClient', 'useQuery', 'useMutation',
    'loadSettings', 'settingsKey', 'filterDialogs', 'unreadBadge', 'renderMediaPreview', 'senderName', 'shortDate',
    'action', 'note', 'stack', 'row', 'text', 'muted', 'buildTmeLink', 'buildMessageTmeLink', 'mediaLabel', 'buildMarkReadAction', 'consumeDialogsRequestPath',
    'Button', 'Dialog', 'DialogContent', 'DialogHeader', 'DialogTitle', 'DialogDescription', 'DialogFooter',
    'Field', 'SafeHtml', 'Confirmation',
    `${paneSource}\nlet mountId = 0\nreturn TelegramPane`,
  )(
    jsx, jsx, useState, useRef, useEffect, () => ({ invalidateQueries() {} }), useQuery, useMutation,
    loadFunction('loadSettings', 'unreadBadge', defaults), (profile, me) => `telegram-settings:${profile}:${me}`,
    loadFunction('filterDialogs', 'Confirmation'), badge,
    loadFunction('renderMediaPreview', 'filterDialogs', 'const jsx = (type, props) => ({ type, props })'),
    loadFunction('senderName', 'shortDate'), loadFunction('shortDate', 'contextText'),
    (label, onClick, disabled = false, extra = {}) => jsx('button', { type: 'button', onClick, disabled, ...extra, children: label }),
    (message, isError = false) => jsx('div', { role: isError ? 'alert' : 'note', children: message }),
    {}, {}, {}, {}, () => null,
    buildMessageTmeLinkImpl, mediaLabelImpl,
    loadFunction('buildMarkReadAction', 'TEXT_LIMIT'), () => '/dialogs?limit=40&refresh=0',
    'button', 'Dialog', 'DialogContent', 'DialogHeader', 'DialogTitle', 'DialogDescription', 'DialogFooter',
    'Field', 'SafeHtml', 'Confirmation',
  )
  const render = () => {
    slot = 0
    const opened = []
    const pane = Pane({
      ctx: { storage: { get: () => null, set() {} }, os: { openExternal: url => { opened.push(url) } }, rest },
      identity: { scope: testScope, me: 'Me' }, profile: 'default', queryPrefix: ['test'],
      statusUnavailable: false, retryButton: null,
    })
    pane.opened = opened
    return pane
  }
  render.restCalls = () => restCalls
  return render
}

test('settings default to manual refresh', () => {
  const load = loadFunction('loadSettings', 'unreadBadge', defaults)
  assert.deepEqual(load({ get: () => null }, 'k'), { autoRefresh: false, unmutedOnly: false, unreadOnly: false, confirmActions: true, tab: 'all' })
  // autoRefresh is a period in ms (30000/60000) or false; legacy true maps to 60s.
  assert.deepEqual(load({ get: () => ({ autoRefresh: true, unmutedOnly: true, unreadOnly: true, tab: '42', junk: 1 }) }, 'k'),
    { autoRefresh: 60000, unmutedOnly: true, unreadOnly: true, confirmActions: true, tab: '42' })
  assert.deepEqual(load({ get: () => ({ autoRefresh: 30000 }) }, 'k'),
    { autoRefresh: 30000, unmutedOnly: false, unreadOnly: false, confirmActions: true, tab: 'all' })
  assert.deepEqual(load({ get: () => ({ autoRefresh: 'yes', tab: 'nope' }) }, 'k'),
    { autoRefresh: false, unmutedOnly: false, unreadOnly: false, confirmActions: true, tab: 'all' })
  // Explicit opt-out is preserved; anything else defaults confirmation on.
  assert.deepEqual(load({ get: () => ({ confirmActions: false }) }, 'k'),
    { autoRefresh: false, unmutedOnly: false, unreadOnly: false, confirmActions: false, tab: 'all' })
  assert.deepEqual(load({ get: () => ({ confirmActions: 'no' }) }, 'k'),
    { autoRefresh: false, unmutedOnly: false, unreadOnly: false, confirmActions: true, tab: 'all' })
  assert.deepEqual(load({ get: () => { throw new Error('storage locked') } }, 'k'),
    { autoRefresh: false, unmutedOnly: false, unreadOnly: false, confirmActions: true, tab: 'all' })
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
  // Exact count: large unread values are shown in full, never collapsed to 99+.
  assert.equal(mutedPill.props.children, '7865')
  const small = badge(3, true)
  assert.equal(small.props.children, '3')
  assert.equal(badge(1, false).props.children, '1')
  assert.equal(badge(100, false).props.children, '100')
})

test('folder tabs remain horizontally scrollable in a constrained narrow header', () => {
  const start = source.indexOf("const tabBar = jsx('div', { role: 'tablist'")
  const end = source.indexOf("feedback &&", start)
  assert.ok(start > 0 && end > start, 'folder tab bar is rendered before the dialog content')
  const header = source.slice(start, end)
  assert.match(header, /minWidth: 0/)
  assert.match(header, /overflowX: 'auto'/)
  assert.match(header, /scrollbarWidth: 'thin'/)
  assert.match(header, /'aria-label': 'Telegram folders'/)
  assert.match(header, /'aria-orientation': 'horizontal'/)
  assert.match(header, /tabIndex: 0/)
  assert.match(header, /onKeyDown: event =>/)
  assert.match(header, /flex: '0 0 auto'/)
  assert.match(header, /\.\.\.stack, width: '100%', minWidth: 0/)
})

test('folder scrollport translates vertical wheel and drags only its gaps', () => {
  const render = makeTelegramPane([])
  const nodes = tree => {
    if (Array.isArray(tree)) return tree.flatMap(nodes)
    return tree && typeof tree === 'object'
      ? [tree, ...nodes(tree.props?.children)]
      : []
  }
  const tablist = nodes(render()).find(node => node.props?.role === 'tablist')
  const scroll = {
    scrollLeft: 10, scrollWidth: 400, clientWidth: 100,
    setPointerCapture(id) { this.captured = id },
    releasePointerCapture(id) { this.released = id },
  }
  let prevented = false
  tablist.props.onWheel({ currentTarget: scroll, deltaX: 0, deltaY: 30, preventDefault() { prevented = true } })
  assert.equal(scroll.scrollLeft, 40)
  assert.equal(prevented, true)
  prevented = false
  tablist.props.onWheel({ currentTarget: scroll, deltaX: 12, deltaY: 0, preventDefault() { prevented = true } })
  assert.equal(scroll.scrollLeft, 40, 'native horizontal trackpad input remains untouched')
  assert.equal(prevented, false)
  tablist.props.onPointerDown({ button: 0, pointerId: 4, clientX: 100, target: { closest: () => null }, currentTarget: scroll })
  tablist.props.onPointerMove({ pointerId: 4, clientX: 80, currentTarget: scroll })
  assert.equal(scroll.scrollLeft, 60)
  tablist.props.onPointerUp({ pointerId: 4, currentTarget: scroll })
  assert.equal(scroll.released, 4)
  tablist.props.onPointerDown({ button: 0, pointerId: 5, clientX: 100, target: { closest: () => ({}) }, currentTarget: scroll })
  tablist.props.onPointerMove({ pointerId: 5, clientX: 40, currentTarget: scroll })
  assert.equal(scroll.scrollLeft, 60, 'dragging over a tab leaves its click target undisturbed')
  assert.equal(tablist.props.onKeyDown instanceof Function, true)
})

test('manual refresh is forced exactly once without changing automatic refresh behavior', () => {
  const consume = loadFunction('consumeDialogsRequestPath', 'buildMarkReadAction')
  const force = { current: true }
  assert.equal(consume(force), '/dialogs?limit=40&refresh=1')
  assert.equal(force.current, false)
  assert.equal(consume(force), '/dialogs?limit=40&refresh=0')
  assert.ok(/const forcedNextDialogsRefresh = useRef\(false\)/.test(source), 'manual refresh uses a one-shot ref')
  assert.ok(/refreshAll\(true\)/.test(source), 'manual refresh forces the backend cache bypass')
  assert.ok(/action\('Retry', \(\) => \{ void refreshAll\(true\) \}/.test(source), 'dialog retry also forces a fresh snapshot')
})

test('mark-read actions preserve the selected post and forum topic ID', () => {
  const create = loadFunction('buildMarkReadAction', 'TEXT_LIMIT')
  assert.deepEqual(create('@group', 321), { action: 'mark-read', peer: '@group', maxId: 321, topicId: 0 })
  assert.deepEqual(create('id:-100123', 777, 55), { action: 'mark-read', peer: 'id:-100123', maxId: 777, topicId: 55 })
  assert.equal(create('@group', 321, 1), null, 'General forum topic read must fail closed')
  assert.equal(create('@group', 0), null)
  assert.equal(create('', 321), null)
  assert.doesNotMatch(source, /\/dialogs\/mark-read/)
  assert.ok(/function requestMarkRead\(maxId, selectedTopicId = 0\)[\s\S]*?buildMarkReadAction\(dialogKey, maxId, selectedTopicId\)[\s\S]*?prepareAndConfirm\(body\)/.test(source), 'mark-read uses the prepare/confirm flow')
  assert.ok(/Mark whole forum read/.test(source), 'whole-forum action is explicit')
  assert.ok(/action\('Mark read up to here', \(\) => \{ void requestMarkRead\(message\.id, topicId\) \}/.test(source), 'message action preserves its selected ID')
  assert.ok(/requestMarkRead\(activeDialog\.topMessageId, 0\)/.test(source), 'chat action uses the visible chat ceiling')
  assert.ok(/'Mark topic as read'/.test(source), 'in-topic action targets the current topic')
  assert.ok(/requestMarkRead\(activeTopic\.topMessage, topicId\)/.test(source), 'topic action uses the topic ceiling')
})

test('per-post save action forwards the message to Saved Messages via prepare/confirm', () => {
  assert.ok(/action\('Save', \(\) => \{ void prepareAndConfirm\(\{ action: 'save', peer: dialogKey, messageId: message\.id \}\) \}, waiting/.test(source), 'Save button is wired to the prepare/confirm flow')
  assert.ok(/title: 'Save to Saved Messages'/.test(source), 'Save button carries its Saved Messages title')
})

test('forum topics are fetched by stable ID and selected before their messages are loaded', () => {
  assert.ok(/read\('\/topics\?' \+ new URLSearchParams\(\{ peer: dialogKey, limit: '50' \}\)\)/.test(source), 'topics use the explicit peer')
  assert.ok(/enabled: !!dialogKey && !!activeDialog\?\.isForum/.test(source), 'topics load only for forum dialogs')
  assert.ok(/topicId > 0/.test(source), 'topic ID scopes message loading')
  assert.ok(/setTopicId\(topic\.id\)/.test(source), 'selection preserves the stable topic ID')
  assert.ok(/topic\.title/.test(source), 'selected topic title is visible')
  assert.ok(/action\('Reply', \(\) => setCompose\(\{ peer: dialogKey, messageId: message\.id, message: '' \}\), waiting\)/.test(source), 'forum messages retain their message-targeted reply action')
  assert.equal(/!activeDialog\?\.isForum && action\('Reply'/.test(source), false, 'the topic selection must not suppress message replies')
})

test('safe Telegram image previews render as bounded lazy images', () => {
  const render = loadFunction('renderMediaPreview', 'filterDialogs', 'const jsx = (type, props) => ({ type, props })')
  const src = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB'
  const image = render({ id: 7, mediaPreview: src })
  assert.equal(image.type, 'img')
  assert.equal(image.props.src, src)
  assert.equal(image.props.loading, 'lazy')
  assert.match(image.props.alt, /7/)
  assert.equal(render({ mediaPreview: 'data:image/svg+xml;base64,PHN2Zz4=' }), null)
  // data URI bound is 240_000 chars INCLUDING the 'data:image/png;base64,' prefix (22 chars)
  assert.equal(render({ mediaPreview: `data:image/png;base64,${'A'.repeat(239979)}` }), null)
  assert.equal(render({ mediaPreview: `data:image/png;base64,${'A'.repeat(239978)}` })?.type, 'img')
})

test('unmuted switch changes and restores the rendered dialog rows in the same folder', () => {
  const render = makeTelegramPane([
    { key: '@alice', name: 'Alice', muted: false, unread: 0, folderIds: ['all'] },
    { key: '@quiet', name: 'Quiet group', muted: true, unread: 0, folderIds: ['all'] },
  ])
  const nodes = tree => {
    const result = []
    const visit = node => {
      if (Array.isArray(node)) return node.forEach(visit)
      if (!node || typeof node !== 'object') return
      result.push(node)
      visit(node.props?.children)
    }
    visit(tree)
    return result
  }
  const dialogNames = tree => nodes(tree)
    .filter(node => node.type === 'button' && Array.isArray(node.props.children) && !node.props['aria-label'])
    .map(node => node.props.children[0]?.props?.children)
  const toggle = tree => nodes(tree).find(node => node.type === 'button' && node.props.role === 'switch' && String(node.props.children).includes('Unmuted only'))

  let tree = render()
  assert.deepEqual(dialogNames(tree), ['Alice', '🔇 Quiet group'])
  toggle(tree).props.onClick()
  tree = render()
  assert.equal(toggle(tree).props['aria-checked'], true)
  assert.deepEqual(dialogNames(tree), ['Alice'])
  toggle(tree).props.onClick()
  tree = render()
  assert.equal(toggle(tree).props['aria-checked'], false)
  assert.deepEqual(dialogNames(tree), ['Alice', '🔇 Quiet group'])
  assert.ok(/queryKey: \[\.\.\.queryPrefix, scope, 'dialogs', folder, unreadOnly, settings\.unmutedOnly\]/.test(source))
  assert.ok(/params\.unmutedOnly = String\(settings\.unmutedOnly\)/.test(source))
  assert.ok(/placeholderData: previous => previous/.test(source))
})

test('mark-read UI prepares exact targets and commits only after confirmation', async () => {
  const render = makeTelegramPane([
    { key: '@alice', name: 'Alice', muted: false, unread: 2, topMessageId: 42, folderIds: ['all'] },
  ], [{ id: 15, date: '', mine: false, sender: 'Bob', htmlPreview: 'target', media: '', replyTo: null }])
  const treeNodes = tree => Array.isArray(tree) ? tree.flatMap(treeNodes)
    : tree && typeof tree === 'object' ? [tree, ...treeNodes(tree.props?.children)] : []
  let tree = render()
  treeNodes(tree).find(node => node.type === 'button' && Array.isArray(node.props.children)
    && node.props.children[0]?.props?.children === 'Alice').props.onClick()
  tree = render()
  treeNodes(tree).find(node => node.type === 'button' && node.props.children === 'Mark read up to here').props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(render.restCalls().map(call => call.path), ['/actions/prepare'])
  assert.deepEqual(render.restCalls()[0].options.body, {
    scope: 's'.repeat(32), action: 'mark-read', peer: '@alice', maxId: 15, topicId: 0,
  })
  treeNodes(render()).find(node => node.type === 'Confirmation').props.onCancel()
  tree = render()
  treeNodes(tree).find(node => node.type === 'button' && node.props.children === 'Mark chat as read').props.onClick()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(render.restCalls().map(call => call.path), ['/actions/prepare', '/actions/prepare'])
  assert.equal(render.restCalls()[1].options.body.maxId, 42)
  tree = render()
  const confirmation = treeNodes(tree).find(node => node.type === 'Confirmation')
  assert.ok(confirmation?.props.ticket)
  await confirmation.props.onConfirm()
  assert.deepEqual(render.restCalls().map(call => call.path), ['/actions/prepare', '/actions/prepare', '/actions/commit'])
})

test('confirm-actions toggle: on shows the dialog, off commits immediately', async () => {
  const render = makeTelegramPane([
    { key: '@alice', name: 'Alice', muted: false, unread: 2, topMessageId: 42, folderIds: ['all'] },
  ], [{ id: 15, date: '', mine: false, sender: 'Bob', htmlPreview: 'target', media: '', replyTo: null }])
  const treeNodes = tree => Array.isArray(tree) ? tree.flatMap(treeNodes)
    : tree && typeof tree === 'object' ? [tree, ...treeNodes(tree.props?.children)] : []
  const findToggle = tree => treeNodes(tree).find(node => node.type === 'button'
    && node.props.role === 'switch' && String(node.props.children).startsWith('Confirm actions'))
  const clickMarkRead = async tree => {
    treeNodes(tree).find(node => node.type === 'button' && Array.isArray(node.props.children)
      && node.props.children[0]?.props?.children === 'Alice').props.onClick()
    tree = render()
    treeNodes(tree).find(node => node.type === 'button' && node.props.children === 'Mark read up to here').props.onClick()
    await new Promise(resolve => setImmediate(resolve))
    return render()
  }

  let tree = render()
  // Default: confirmation on, switch rendered checked.
  assert.equal(findToggle(tree).props['aria-checked'], true)
  tree = await clickMarkRead(tree)
  assert.deepEqual(render.restCalls().map(call => call.path), ['/actions/prepare'])
  assert.ok(treeNodes(tree).find(node => node.type === 'Confirmation')?.props.ticket, 'dialog shown when confirmation is on')

  // Toggle off: the same action prepares and commits in one step, no dialog.
  findToggle(tree).props.onClick()
  tree = render()
  assert.equal(findToggle(tree).props['aria-checked'], false)
  render.restCalls().length = 0
  tree = await clickMarkRead(tree)
  assert.deepEqual(render.restCalls().map(call => call.path), ['/actions/prepare', '/actions/commit'])
  assert.equal(treeNodes(tree).find(node => node.type === 'Confirmation')?.props.ticket, null,
    'no dialog ticket when confirmation is off')
  assert.match(source, /if \(!settings\.confirmActions && prepared\) return await commit\(prepared\)/)

  // Toggle back on: the dialog returns.
  findToggle(tree).props.onClick()
  tree = render()
  assert.equal(findToggle(tree).props['aria-checked'], true)
})

test('dialog tabs filter by backend-returned stable folder IDs', () => {
  const filter = loadFunction('filterDialogs', 'Confirmation')
  const rows = [
    { key: '@a', unread: 0, muted: false, folderIds: ['all', '7'] },
    { key: '@b', unread: 4, muted: false, folderIds: ['all', '8', 'unread'] },
    { key: '@c', unread: 9, muted: true, folderIds: ['all', 'unread'] },
    { key: '@d', unread: 2, muted: true, folderIds: ['archive', 'unread'] },
  ]
  const allRows = filter(rows, 'all', false).map(d => d.key)
  assert.deepEqual(allRows, ['@a', '@b', '@c'])
  assert.deepEqual(filter(rows, 'all', true).map(d => d.key), ['@a', '@b'])
  assert.deepEqual(filter(rows, 'all', false).map(d => d.key), allRows, 'turning the filter off restores muted rows in the same folder')
  assert.ok(/const visibleDialogs = filterDialogs\(dialogsList, selectedTab, settings\.unmutedOnly\)/.test(source), 'the toggle state feeds the rendered dialog list')
  assert.deepEqual(filter(rows, 'unread', false).map(d => d.key), ['@b', '@c', '@d'])
  assert.deepEqual(filter(rows, '7', false).map(d => d.key), ['@a'])
  assert.deepEqual(filter(rows, '8', false).map(d => d.key), ['@b'])
  assert.deepEqual(filter(rows, 'archive', false).map(d => d.key), ['@d'])
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

test('buildMessageTmeLink points at a specific message', () => {
  assert.equal(buildMessageTmeLinkImpl('@username', 501, ''), 'https://t.me/username/501')
  // Public channel: the dialog's webUsername (no @) is the deep link target.
  assert.equal(buildMessageTmeLinkImpl('id:5', 9, 'channel'), 'https://t.me/channel/9')
  // Private supergroup (no username): t.me/c/<positive_id>/<message_id>.
  assert.equal(buildMessageTmeLinkImpl('id:-1001234567890', 77, ''), 'https://t.me/c/1234567890/77')
  // Bare numeric peer id: plain t.me/<peer_id>/<message_id> form.
  assert.equal(buildMessageTmeLinkImpl('id:123', 4, ''), 'https://t.me/123/4')
  // No usable message id falls back to the chat link.
  assert.equal(buildMessageTmeLinkImpl('@username', 0, ''), 'https://t.me/username')
  assert.equal(buildMessageTmeLinkImpl('', 10, ''), null)
})

test('mediaLabel maps raw Telethon media classes to friendly names', () => {
  assert.equal(mediaLabelImpl('MessageMediaPhoto'), 'photo')
  assert.equal(mediaLabelImpl('MessageMediaDocument'), 'file')
  assert.equal(mediaLabelImpl('MessageMediaVideo'), 'video')
  assert.equal(mediaLabelImpl('MessageMediaVoice'), 'voice')
  // Unknown classes pass through rather than being hidden, so a real
  // Telethon media type is never silently turned into a wrong label.
  assert.equal(mediaLabelImpl('MessageMediaUnsupported'), 'MessageMediaUnsupported')
  assert.equal(mediaLabelImpl(''), '')
  assert.equal(mediaLabelImpl(undefined), '')
})

test('per-post Open in browser button deep-links the exact message', async () => {
  const render = makeTelegramPane([
    { key: '@alice', name: 'Alice', muted: false, unread: 0, topMessageId: 42, folderIds: ['all'] },
  ], [{ id: 15, date: '2026-10-07T10:00:00Z', mine: false, sender: 'Bob', htmlPreview: 'hello', media: '', replyTo: null }])
  const treeNodes = tree => Array.isArray(tree) ? tree.flatMap(treeNodes)
    : tree && typeof tree === 'object' ? [tree, ...treeNodes(tree.props?.children)] : []
  let tree = render()
  treeNodes(tree).find(node => node.type === 'button' && Array.isArray(node.props.children)
    && node.props.children[0]?.props?.children === 'Alice').props.onClick()
  tree = render()
  const open = treeNodes(tree).find(node => node.type === 'button' && node.props.children === 'Open post in browser')
  assert.ok(open, 'per-post Open post in browser button is rendered')
  await open.props.onClick()
  assert.deepEqual(tree.opened, ['https://t.me/alice/15'])
  assert.match(source, /os\.openExternal/)
})

test('message-history action bar is sticky while the history scrolls', () => {
  const render = makeTelegramPane([
    { key: '@alice', name: 'Alice', muted: false, unread: 2, topMessageId: 42, folderIds: ['all'] },
  ], [{ id: 15, date: '2026-10-07T10:00:00Z', mine: false, sender: 'Bob', htmlPreview: 'hello', media: '', replyTo: null }])
  const treeNodes = tree => Array.isArray(tree) ? tree.flatMap(treeNodes)
    : tree && typeof tree === 'object' ? [tree, ...treeNodes(tree.props?.children)] : []
  let tree = render()
  treeNodes(tree).find(node => node.type === 'button' && Array.isArray(node.props.children)
    && node.props.children[0]?.props?.children === 'Alice').props.onClick()
  tree = render()
  // The bar holding 'Open in browser' / 'Mark chat as read' / 'Reply here'
  // is the sticky header div itself.
  const bar = treeNodes(tree).find(node => node.type === 'div' && node.props?.style?.position === 'sticky'
    && treeNodes(node.props.children).some(child => child?.props?.children === 'Reply here'))
  assert.ok(bar, 'action bar is rendered')
  assert.equal(bar.props.style.position, 'sticky', 'action bar sticks to the top of the scroll container')
  assert.equal(bar.props.style.zIndex, 2, 'action bar renders above the messages')
  assert.match(String(bar.props.style.background), /var\(--ui-bg-editor/)
  assert.match(source, /position: 'sticky', top: '-0\.5rem', zIndex: 2/)
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
