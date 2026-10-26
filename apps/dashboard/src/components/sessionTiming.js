export function sessionRemaining(data, now, absoluteOnly = false) {
  const serverNow = Date.parse(data.server_now)
  const idle = Date.parse(data.idle_expires_at)
  const absolute = Date.parse(data.absolute_expires_at)
  if (![serverNow, idle, absolute, now, data.receivedAt].every(Number.isFinite)) return 0
  return Math.max(
    0,
    Math.floor(
      ((absoluteOnly ? absolute : Math.min(idle, absolute)) -
        serverNow -
        Math.max(0, now - data.receivedAt)) /
        1000
    )
  )
}
