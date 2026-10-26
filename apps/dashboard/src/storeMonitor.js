// Poll only while work is active; background reads never submit lifecycle requests.
export function monitorStores(
  read,
  onData,
  onError,
  { visibility = globalThis.document, delay = setTimeout, cancel = clearTimeout } = {}
) {
  let stopped = false,
    serial = 0,
    controller,
    timer,
    requestTimer,
    failures = 0
  const clear = () => {
    cancel(timer)
    cancel(requestTimer)
  }
  const schedule = (ms) => {
    cancel(timer)
    if (!stopped && !visibility?.hidden) timer = delay(refresh, ms)
  }
  async function refresh() {
    clear()
    controller?.abort()
    const current = ++serial
    if (stopped || visibility?.hidden) return
    controller = new AbortController()
    const signal = controller.signal
    requestTimer = delay(() => controller?.abort(), 20_000)
    try {
      const data = await read(signal)
      if (stopped || current !== serial) return
      if (signal.aborted) throw new Error('Store status check timed out.')
      if (!Array.isArray(data?.items) || data.next_before_id) {
        throw new Error('Store information is incomplete. Refresh to try again.')
      }
      failures = 0
      onData(data.items)
      if (data.items.some((store) => ['Provisioning', 'Deleting'].includes(store.status))) {
        schedule(5000)
      }
    } catch (error) {
      if (stopped || current !== serial) return
      onError(
        signal.aborted
          ? 'Store status took too long to load. Checking again shortly.'
          : error.message || 'Store status is temporarily unavailable.'
      )
      failures += 1
      schedule(Math.min(30_000, 5000 * 2 ** Math.min(failures - 1, 3)))
    } finally {
      if (current === serial) cancel(requestTimer)
    }
  }
  function visible() {
    if (visibility?.hidden) {
      serial += 1
      clear()
      controller?.abort()
    } else refresh()
  }
  visibility?.addEventListener('visibilitychange', visible)
  refresh()
  return {
    refresh,
    stop() {
      stopped = true
      serial += 1
      clear()
      controller?.abort()
      visibility?.removeEventListener('visibilitychange', visible)
    },
  }
}
