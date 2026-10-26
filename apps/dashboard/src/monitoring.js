const counts = ['calls', 'success', 'errors', 'denied', 'uncertain']
function number(value, nullable = false) {
  if (nullable && value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0)
    throw new Error('Invalid monitoring data.')
  return value
}
function count(value) {
  if (!Number.isSafeInteger(value)) throw new Error('Invalid monitoring data.')
  return number(value)
}
function date(value, nullable = false) {
  if (nullable && value === null) return null
  if (typeof value !== 'string' || value.length > 64 || !Number.isFinite(Date.parse(value)))
    throw new Error('Invalid monitoring data.')
  return value
}
function text(value, limit) {
  if (typeof value !== 'string' || value.length > limit) throw new Error('Invalid monitoring data.')
  return value
}
function metrics(value) {
  const result = Object.fromEntries(counts.map((key) => [key, count(value[key])]))
  if (result.calls !== result.success + result.errors + result.denied + result.uncertain)
    throw new Error('Invalid monitoring counts.')
  for (const key of ['average_ms', 'p95_ms', 'error_rate_percent'])
    result[key] = number(value[key], true)
  if (result.error_rate_percent > 100) throw new Error('Invalid monitoring rate.')
  return result
}
export function validateMonitoring(value) {
  if (
    !value ||
    ![60, 360, 1440].includes(value.window_minutes) ||
    value.cache_seconds !== 20 ||
    typeof value.truncated !== 'boolean' ||
    !Array.isArray(value.servers) ||
    value.servers.length > 200 ||
    !Array.isArray(value.series) ||
    value.series.length !== 12 ||
    !Array.isArray(value.alerts) ||
    value.alerts.length > 10
  )
    throw new Error('Invalid monitoring data.')
  return {
    generated_at: date(value.generated_at),
    window_minutes: value.window_minutes,
    truncated: value.truncated,
    tools: metrics(value.tools),
    http_process: {
      since: date(value.http_process.since),
      ...Object.fromEntries(
        ['requests', 'errors', 'rejected', 'cancelled'].map((key) => [
          key,
          count(value.http_process[key]),
        ])
      ),
      average_ms: number(value.http_process.average_ms, true),
    },
    jobs: {
      ...Object.fromEntries(
        [
          'queued',
          'running',
          'overdue',
          'expired_leases',
          'waiting',
          'failed_creations',
          'created',
        ].map((key) => [key, count(value.jobs[key])])
      ),
      average_creation_seconds: number(value.jobs.average_creation_seconds, true),
    },
    stores: Object.fromEntries(
      ['Provisioning', 'Ready', 'Failed', 'Deleting', 'Deleted'].map((key) => [
        key,
        count(value.stores[key] ?? 0),
      ])
    ),
    servers: value.servers.map((item) => {
      if (!['Connected', 'Disabled', 'Unreachable'].includes(item.status))
        throw new Error('Invalid server status.')
      return {
        id: count(item.id),
        slug: text(item.slug, 32),
        name: text(item.name, 100),
        status: item.status,
        status_note: text(item.status_note, 240),
        last_checked_at: date(item.last_checked_at, true),
        metrics: metrics(item.metrics),
      }
    }),
    series: value.series.map((point) => ({
      start: date(point.start),
      end: date(point.end),
      ...metrics(point),
    })),
    alerts: value.alerts.map((item) => {
      if (!['warning', 'critical'].includes(item.severity))
        throw new Error('Invalid alert severity.')
      return {
        code: text(item.code, 64),
        count: count(item.count),
        severity: item.severity,
        message: text(item.message, 240),
      }
    }),
  }
}

export function startMonitoring(
  request,
  onData,
  onError,
  { windowMinutes = 60, interval = 30000, timeout = 10000 } = {}
) {
  let stopped = false,
    timer,
    controller
  async function tick() {
    if (stopped) return
    controller = new AbortController()
    const deadline = setTimeout(() => controller.abort(), timeout)
    try {
      if (typeof document === 'undefined' || document.visibilityState !== 'hidden') {
        const result = await request('/admin/monitoring?window_minutes=' + windowMinutes, {
          signal: controller.signal,
        })
        if (!stopped && !controller.signal.aborted) onData(validateMonitoring(result))
      }
    } catch {
      if (!stopped) onError('Monitoring could not refresh. Displayed results may be out of date.')
    } finally {
      clearTimeout(deadline)
      if (!stopped) timer = setTimeout(tick, interval)
    }
  }
  tick()
  return () => {
    stopped = true
    clearTimeout(timer)
    controller?.abort()
  }
}
