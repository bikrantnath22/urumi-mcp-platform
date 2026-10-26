import assert from 'node:assert/strict'
import { StoreDeletionRequest } from '../src/storeDeletion.js'
import { monitorStores } from '../src/storeMonitor.js'

const sid = '02e539a9-ea63-4ffd-9133-1b527af88704'
const rid = '11111111-1111-4111-8111-111111111111'
const storage = () => {
  const map = new Map()
  return { getItem: key => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value), removeItem: key => map.delete(key) }
}
const reply = status => ({ store: { id: sid, status },
  operation: { action: 'delete', store_id: sid } })
const saved = storage()
let calls = []
let fail = true
const request = async (path, options) => {
  calls.push({ path, ...options })
  if (fail) throw new Error('Connection lost after acceptance')
  return reply('Deleting')
}
let client = new StoreDeletionRequest(2, sid, saved, request, () => rid)
await assert.rejects(client.submit(1))
assert.equal(client.pending.request_id, rid)
client = new StoreDeletionRequest(2, sid, saved, request)
fail = false
await client.submit(2)
assert.deepEqual(calls[0].body, calls[1].body)
assert.equal(calls[1].method, 'DELETE')
assert.equal(calls[1].path, '/stores/' + sid)
assert.equal(client.pending, null)
assert.equal(new StoreDeletionRequest(3, sid, saved, request).pending, null)

let resolve
client = new StoreDeletionRequest(2, sid, storage(), () => new Promise(r => { resolve = r }), () => rid)
const first = client.submit(1)
await assert.rejects(client.submit(1))
resolve(reply('Deleted'))
await first
for (const failure of [403, 409, 503]) {
  client = new StoreDeletionRequest(2, sid, storage(), async () => {
    throw Object.assign(new Error('failed'), { status: failure })
  }, () => rid)
  await assert.rejects(client.submit(1))
  assert.equal(Boolean(client.pending), failure === 503)
}
client = new StoreDeletionRequest(2, sid, storage(), async () => ({
  ...reply('Deleting'), store: { id: rid, status: 'Deleting' },
}), () => rid)
await assert.rejects(client.submit(1))
assert.equal(client.pending.request_id, rid)
client = new StoreDeletionRequest(2, sid, {
  getItem: () => null, setItem: () => { throw new Error('Storage unavailable') },
}, () => { throw new Error('Must not send') }, () => rid)
await assert.rejects(client.submit(1), /Storage unavailable/)
console.log('Deletion preserves the same request after lost replies; duplicate sends, other-account storage and wrong acknowledgements are rejected.')

let tick = 0
const timers = new Map()
const delay = (fn, ms) => { const id = ++tick; timers.set(id, { fn, ms }); return id }
const cancel = id => timers.delete(id)
const listeners = new Map()
const visibility = { hidden: false,
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: name => listeners.delete(name) }
const flush = async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve() }
const next = ms => {
  const found = [...timers].find(([, value]) => value.ms === ms)
  assert.ok(found, 'Expected timer ' + ms)
  timers.delete(found[0]); found[1].fn()
}
let state = 'Provisioning', reads = 0, data = [], errors = [], signals = []
let reader = async signal => {
  reads += 1; signals.push(signal)
  return { items: [{ id: sid, status: state }], next_before_id: null }
}
const monitor = monitorStores(signal => reader(signal), rows => data.push(rows),
  error => errors.push(error), { visibility, delay, cancel })
await flush()
assert.equal(reads, 1)
next(5000); await flush()
assert.equal(reads, 2)
state = 'Ready'
next(5000); await flush()
assert.equal(timers.size, 0)
visibility.hidden = true
listeners.get('visibilitychange')()
await monitor.refresh()
assert.equal(reads, 3)
visibility.hidden = false
listeners.get('visibilitychange')(); await flush()
assert.equal(reads, 4)
reader = async () => { throw new Error('Temporary status error') }
await monitor.refresh()
assert.equal(errors.at(-1), 'Temporary status error')
assert.equal(data.at(-1)[0].status, 'Ready')
assert.ok([...timers.values()].some(value => value.ms === 5000))
reader = signal => { signals.push(signal); return new Promise(() => {}) }
monitor.refresh()
assert.equal(signals.at(-1).aborted, false)
monitor.stop()
assert.equal(signals.at(-1).aborted, true)
assert.equal(timers.size, 0)
assert.equal(listeners.size, 0)
console.log('Polling follows active jobs, stops at Ready, pauses in hidden tabs and releases reads on unmount; errors preserve visible stores.')

let done
data = []
const late = monitorStores(() => new Promise(r => { done = r }), rows => data.push(rows),
  error => errors.push(error), { visibility, delay, cancel })
late.stop()
done({ items: [{ id: sid, status: 'Ready' }] })
await flush()
assert.equal(data.length, 0)
console.log('Late replies after cleanup cannot restore stale store cards. No live HTTP or browser storage was used.')
