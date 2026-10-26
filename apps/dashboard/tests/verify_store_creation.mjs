import assert from 'node:assert/strict'
import { StoreCreationRequest, creationValues, newStoreRequestId } from '../src/storeCreation.js'

const REQUEST = '11111111-1111-4111-8111-111111111111'
const STORE = '22222222-2222-4222-8222-222222222222'
const input = { name: 'Assam Tea', slug: 'assam-tea' }

function storage() {
  const rows = new Map()
  return {
    rows,
    getItem: key => rows.has(key) ? rows.get(key) : null,
    setItem: (key, value) => rows.set(key, value),
    removeItem: key => rows.delete(key),
  }
}

function reply(body, owner = 7) {
  return {
    store: { id: STORE, owner_id: owner, name: body.name, slug: body.slug, status: 'Provisioning' },
    operation: { store_id: STORE, action: 'provision' },
  }
}

assert.deepEqual(creationValues({ name: ' Assam Tea ', slug: 'ASSAM-TEA' }), input)
for (const slug of ['admin', 'www', '-tea', 'tea-', 'a/b', 'a'.repeat(33)]) {
  assert.throws(() => creationValues({ name: 'Tea', slug }))
}
assert.throws(() => creationValues({ name: 'Tea\nShop', slug: 'tea' }))
assert.equal(newStoreRequestId({ getRandomValues: bytes => bytes.fill(0) }),
  '00000000-0000-4000-8000-000000000000')
assert.equal(newStoreRequestId({ randomUUID: () => REQUEST }), REQUEST)
assert.throws(() => newStoreRequestId({}))
console.log('PASS: input validation, native UUIDs and secure random fallback.')

{
  const saved = storage()
  const accepted = new Map()
  let calls = 0
  const api = async (path, options) => {
    assert.equal(path, '/stores')
    assert.equal(options.method, 'POST')
    assert.deepEqual(Object.keys(options.body).sort(), ['name', 'request_id', 'slug'])
    const body = { ...options.body }
    if (!accepted.has(body.request_id)) accepted.set(body.request_id, reply(body))
    calls += 1
    if (calls === 1) throw new TypeError('Reply lost after acceptance')
    return accepted.get(body.request_id)
  }
  const first = new StoreCreationRequest(7, saved, api, () => REQUEST)
  await assert.rejects(first.submit({ ...input, owner_id: 99 }), /Reply lost/)
  assert.equal(first.pending.request_id, REQUEST)
  assert(Object.isFrozen(first.pending))
  const restored = new StoreCreationRequest(7, saved, api, () => {
    throw new Error('A retry generated a new identifier')
  })
  await assert.rejects(restored.submit({ ...input, slug: 'another-tea' }), /saved request/)
  assert.equal(calls, 1)
  const result = await restored.submit(input)
  assert.equal(result.store.id, STORE)
  assert.equal(accepted.size, 1)
  assert.equal(calls, 2)
  assert.equal(restored.pending, null)
  assert.equal(saved.rows.size, 0)
}
console.log('PASS: an accepted request with a lost reply survives reload and creates once on retry.')

for (const status of [400, 401, 403, 409, 422, 429]) {
  const saved = storage()
  const client = new StoreCreationRequest(7, saved, async () => {
    throw Object.assign(new Error('Rejected'), { status })
  }, () => REQUEST)
  await assert.rejects(client.submit(input), /Rejected/)
  assert.equal(client.pending, null)
  assert.equal(saved.rows.size, 0)
}
for (const status of [undefined, 408, 500, 502, 503, 504]) {
  const saved = storage()
  const client = new StoreCreationRequest(7, saved, async () => {
    throw Object.assign(new Error('Uncertain'), { status })
  }, () => REQUEST)
  await assert.rejects(client.submit(input), /Uncertain/)
  assert.equal(client.pending.request_id, REQUEST)
  assert.equal(saved.rows.size, 1)
}
console.log('PASS: rejected requests remain editable; timeouts and connection/server failures retain recovery.')

{
  const saved = storage()
  let release, calls = 0
  const client = new StoreCreationRequest(7, saved, async (path, options) => {
    calls += 1
    return new Promise(resolve => { release = () => resolve(reply(options.body)) })
  }, () => REQUEST)
  const first = client.submit(input)
  await assert.rejects(client.submit(input), /already being submitted/)
  assert.equal(calls, 1)
  release()
  await first
  assert.equal(client.busy, false)
}
{
  const saved = storage()
  const first = new StoreCreationRequest(7, saved, async () => { throw new Error('Offline') }, () => REQUEST)
  await assert.rejects(first.submit(input), /Offline/)
  const other = new StoreCreationRequest(8, saved, async (path, options) => reply(options.body, 8), () => REQUEST)
  assert.equal(other.pending, null)
  await other.submit(input)
  assert(saved.rows.has(first.key))
  assert(!saved.rows.has(other.key))
}
console.log('PASS: rapid submissions send once and different accounts keep separate recovery data.')

{
  const normalTimer = globalThis.setTimeout
  globalThis.setTimeout = callback => normalTimer(callback, 1)
  try {
    const saved = storage()
    const client = new StoreCreationRequest(7, saved, (path, options) =>
      new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () =>
          reject(new DOMException('Reply timed out', 'AbortError')), { once: true })
      }), () => REQUEST)
    await assert.rejects(client.submit(input), { name: 'AbortError' })
    assert.equal(client.pending.request_id, REQUEST)
    assert.equal(client.busy, false)
  } finally {
    globalThis.setTimeout = normalTimer
  }
}
console.log('PASS: a stalled request is aborted and retains the same recovery identifier.')

for (const value of [
  '{', 'null', JSON.stringify({ ...input, request_id: 'bad' }),
  JSON.stringify({ ...input, request_id: REQUEST, owner_id: 8 }),
]) {
  const saved = storage()
  saved.setItem('urm.store-create.v1.7', value)
  assert.throws(() => new StoreCreationRequest(7, saved, () => {}), /could not be restored/)
}
{
  let calls = 0
  const blocked = storage()
  blocked.setItem = () => { throw new Error('Storage unavailable') }
  const client = new StoreCreationRequest(7, blocked, () => { calls += 1 }, () => REQUEST)
  await assert.rejects(client.submit(input), /Storage unavailable/)
  assert.equal(calls, 0)
  assert.equal(client.pending, null)
}
for (const mutate of [
  data => { data.store.owner_id = 8 },
  data => { data.store.slug = 'another-store' },
  data => { data.store.id = 'invalid' },
  data => { data.operation.store_id = REQUEST },
  data => { data.operation.action = 'delete' },
]) {
  const saved = storage()
  const client = new StoreCreationRequest(7, saved, async (path, options) => {
    const data = reply(options.body)
    mutate(data)
    return data
  }, () => REQUEST)
  await assert.rejects(client.submit(input), /could not be confirmed/)
  assert.equal(client.pending.request_id, REQUEST)
}
console.log('PASS: unavailable/corrupt storage and wrong-account or malformed acknowledgements fail safely.')

{
  const saved = storage()
  let failRemove = true, calls = 0
  saved.removeItem = key => {
    if (failRemove) throw new Error('Storage unavailable')
    saved.rows.delete(key)
  }
  const client = new StoreCreationRequest(7, saved, async (path, options) => {
    calls += 1
    const data = reply(options.body)
    options.body.slug = 'mutated-caller-data'
    return data
  }, () => REQUEST)
  await client.submit(input)
  assert.equal(client.pending.slug, input.slug)
  failRemove = false
  await client.submit(input)
  assert.equal(calls, 2)
  assert.equal(client.pending, null)
}
console.log('PASS: failed browser cleanup retains the original request and caller mutations cannot change it.')
console.log('Store creation client verification passed. All HTTP calls and browser storage were fixtures.')
