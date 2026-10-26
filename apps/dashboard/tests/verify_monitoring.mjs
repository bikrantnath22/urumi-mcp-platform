import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'
import { startMonitoring, validateMonitoring } from '../src/monitoring.js'

const metrics = { calls: 10, success: 7, errors: 2, denied: 1, uncertain: 0,
  average_ms: 25, p95_ms: 80, error_rate_percent: 22.2 }
const point = { start: '2026-10-22T06:00:00Z', end: '2026-10-22T06:05:00Z', ...metrics }
const data = { generated_at: point.end, window_minutes: 60, cache_seconds: 20, truncated: false,
  http_process: { since: point.start, requests: 15, errors: 1, rejected: 2, cancelled: 0, average_ms: 23 },
  tools: metrics, jobs: { queued: 0, running: 1, overdue: 0, expired_leases: 0, waiting: 0,
    failed_creations: 0, created: 2, average_creation_seconds: 123 }, stores: { Ready: 2 },
  servers: [{ id: 1, slug: 'woo', name: 'Woo', status: 'Connected', status_note: 'Last call succeeded.',
    last_checked_at: null, metrics, credential: 'PRIVATE' }],
  series: Array.from({ length: 12 }, () => point), alerts: [] }
assert.equal(validateMonitoring(data).tools.calls, 10)
assert.equal(validateMonitoring(data).servers[0].credential, undefined)
for (const change of [ { tools: { ...metrics, calls: -1 } }, { tools: { ...metrics, p95_ms: NaN } },
  { tools: { ...metrics, error_rate_percent: 101 } }, { series: [] }, { window_minutes: 1 },
  { generated_at: 'bad' }, { alerts: [{ code: 'a', severity: 'healthy', count: 1, message: 'bad' }] } ]) {
  assert.throws(() => validateMonitoring({ ...data, ...change }))
}
let calls = 0, active = 0, maximum = 0, received = 0
let ready
const twice = new Promise(resolve => { ready = resolve })
const stop = startMonitoring(async () => {
  calls++; active++; maximum = Math.max(maximum, active)
  await delay(8); active--; return data
}, () => { received++; if (received === 2) ready() }, () => assert.fail('Unexpected failure'), { interval: 5, timeout: 100 })
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Polling stalled')), 2000)
  twice.then(() => { clearTimeout(timer); resolve() })
})
stop()
const before = received
await delay(30)
assert.equal(maximum, 1)
assert.ok(calls >= 2 && received >= 1)
assert.equal(received, before)
let aborted = false, reported = false
const cancel = startMonitoring((_path, { signal }) => new Promise((resolve, reject) => {
  signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')) })
}), () => assert.fail('Cancelled result published'), () => { reported = true }, { timeout: 5, interval: 100 })
await delay(15)
cancel()
assert.equal(aborted, true)
assert.equal(reported, true)
let deliver
const stopLate = startMonitoring(() => new Promise(resolve => { deliver = resolve }),
  () => assert.fail('Late result escaped'), () => assert.fail('Late error escaped'))
stopLate()
deliver(data)
await delay(5)
console.log('Monitoring validates bounded metrics; malformed responses and credentials do not reach the page.')
console.log('Polling stays sequential; timeouts, navigation cleanup and late replies cannot publish stale success.')
console.log('Monitoring client verification passed. All HTTP calls were fixtures.')
