import assert from 'node:assert/strict'
import { canSetupWordPress, prepareWordPressSetup } from '../src/wordpressSetup.js'

const owner = { id: 7, role: 'member' }
const base = { owner_id: owner.id, id: '02e539a9-ea63-4ffd-9133-1b527af88704', status: 'Ready',
  access_enabled: true, access_guarded: true, generation: 1,
  url: 'http://assamtea.localtest.me' }
const expectedUrl = base.url + '/wp-login.php?action=urumi_setup'
const response = { store_id: base.id, token: 'A'.repeat(43), url: expectedUrl,
  expires_at: new Date(Date.now() + 300000).toISOString() }
let calls = 0
const request = async (url, options) => {
  calls++
  assert.equal(url, '/stores/' + base.id + '/wordpress-setup')
  assert.equal(options.method, 'POST')
  assert.deepEqual(options.body, { generation: 1 })
  assert.ok(!JSON.stringify(options.body).includes('password'))
  return structuredClone(response)
}
const result = await prepareWordPressSetup(base, owner, request)
assert.deepEqual(result, { token: response.token, url: expectedUrl, expires_at: response.expires_at })
assert.ok(Object.isFrozen(result))
assert.ok(!result.url.includes(result.token))
for (const changed of [
  { status: 'Provisioning' }, { access_enabled: false }, { access_guarded: false },
  { url: 'javascript:alert(1)' }, { url: 'http://alice:secret@assamtea.localtest.me' },
  { url: base.url + '/other' }, { url: base.url + '?other=1' }, { generation: 1.5 },
]) {
  const before = calls
  await assert.rejects(prepareWordPressSetup({ ...base, ...changed }, owner, request))
  assert.equal(calls, before)
}
for (const changed of [
  { url: 'http://another.localtest.me/wp-login.php?action=urumi_setup' },
  { store_id: 'another-store' }, { token: '' }, { token: 'A'.repeat(44) },
  { expires_at: 'invalid' }, { expires_at: new Date(0).toISOString() },
]) {
  await assert.rejects(prepareWordPressSetup(base, owner, async () => ({ ...response, ...changed })))
}
const mutable = { ...base }
await prepareWordPressSetup(mutable, owner, async () => {
  mutable.id = 'another-store'; mutable.url = 'https://another.invalid'
  return structuredClone(response)
})
await assert.rejects(prepareWordPressSetup(base, owner, async () => { throw new Error('Unavailable') }), /Unavailable/)
const abort = new AbortController()
await prepareWordPressSetup(base, owner, async (_, options) => {
  assert.equal(options.signal, abort.signal)
  return structuredClone(response)
}, abort.signal)
for (const user of [undefined, { id: 8, role: 'member' }, { id: 8, role: 'admin' },
  { id: '7', role: 'admin' }, { role: 'admin' }]) {
  const before = calls
  assert.equal(canSetupWordPress(base, user), false)
  await assert.rejects(prepareWordPressSetup(base, user, request), /Only the store owner/)
  assert.equal(calls, before)
}
for (const role of ['member', 'admin']) {
  const user = { ...owner, role }
  assert.equal(canSetupWordPress(base, user), true)
  await prepareWordPressSetup(base, user, request)
}
assert.equal(canSetupWordPress({ ...base, owner_id: 8 }, owner), false)
console.log('Only store owners can start setup; platform admin status cannot grant access to another store.')
console.log('WordPress setup targets one Ready store; malformed replies, disabled stores and unsafe destinations fail.')
console.log('Capabilities stay out of URLs and passwords never enter setup requests. All HTTP calls were fixtures.')
