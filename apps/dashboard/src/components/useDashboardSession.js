import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Pure calculation is exported from a separate module for boundary checks.
import { sessionRemaining } from './sessionTiming.js'

export default function useDashboardSession(user) {
  const [status, setStatus] = useState({
    warning: false,
    absoluteWarning: false,
    expired: false,
    remaining: 0,
    error: '',
  })
  const deadline = useRef(null)
  const lastActivity = useRef(-Infinity)
  const lastRenewal = useRef(-Infinity)
  const inFlight = useRef(null)
  const generation = useRef(0)
  const expired = useRef(false)

  const expire = useCallback(() => {
    expired.current = true
    setStatus({ warning: false, absoluteWarning: false, expired: true, remaining: 0, error: '' })
  }, [])

  const apply = useCallback(
    (data) => {
      if (data.user_id !== user.id) {
        expired.current = true
        setStatus({
          warning: false,
          absoluteWarning: false,
          expired: true,
          remaining: 0,
          error:
            'The account changed in another tab. Sign in to this account to resume its workspace.',
        })
        return false
      }
      deadline.current = { ...data, receivedAt: performance.now() }
      lastRenewal.current = performance.now()
      expired.current = false
      const remaining = sessionRemaining(deadline.current, performance.now())
      setStatus({
        warning: remaining <= 120,
        absoluteWarning: sessionRemaining(deadline.current, performance.now(), true) <= 120,
        expired: false,
        remaining,
        error: '',
      })
      return true
    },
    [user.id]
  )

  const refresh = useCallback(
    async (renew = false) => {
      if (inFlight.current || (renew && expired.current)) return
      const version = generation.current
      const request = Symbol('session request')
      inFlight.current = request
      try {
        const data = await api(
          renew ? '/auth/renew' : '/auth/session',
          renew ? { method: 'POST', body: {} } : {}
        )
        if (version === generation.current) return apply(data)
      } catch (error) {
        if (version !== generation.current) return
        if (error.status === 401) expire()
        else
          setStatus((current) => ({
            ...current,
            error: 'Could not check your session. Check your connection.',
          }))
      } finally {
        if (inFlight.current === request) inFlight.current = null
      }
      return false
    },
    [apply, expire]
  )

  useEffect(() => {
    generation.current += 1
    expired.current = false
    deadline.current = null
    lastActivity.current = -Infinity
    lastRenewal.current = -Infinity
    inFlight.current = null
    const version = generation.current
    Promise.resolve().then(() => {
      if (version === generation.current) refresh()
    })
    function activity(event) {
      if (!event.isTrusted || document.visibilityState !== 'visible') return
      lastActivity.current = performance.now()
      const nearIdleExpiry =
        deadline.current &&
        sessionRemaining(deadline.current, lastActivity.current) <= 120 &&
        sessionRemaining(deadline.current, lastActivity.current, true) > 120
      if (
        !expired.current &&
        deadline.current &&
        (lastActivity.current - lastRenewal.current >= 60_000 || nearIdleExpiry)
      )
        refresh(true)
    }
    function tick() {
      if (!deadline.current || expired.current) return
      const remaining = sessionRemaining(deadline.current, performance.now())
      if (remaining <= 0) {
        expire()
        return
      }
      setStatus((current) => ({
        ...current,
        warning: remaining <= 120,
        absoluteWarning: sessionRemaining(deadline.current, performance.now(), true) <= 120,
        remaining,
      }))
      if (
        document.visibilityState === 'visible' &&
        lastActivity.current > lastRenewal.current &&
        performance.now() - lastRenewal.current >= 60_000
      )
        refresh(true)
    }
    const events = ['pointerdown', 'keydown', 'input', 'wheel']
    function checkSharedSession() {
      if (document.visibilityState !== 'visible') return
      generation.current += 1
      inFlight.current = null
      refresh()
    }
    let channel
    try {
      channel = new BroadcastChannel('urumi-session')
      channel.addEventListener('message', checkSharedSession)
    } catch {
      /* The visibility check also works without BroadcastChannel. */
    }
    for (const event of events) window.addEventListener(event, activity, { passive: true })
    document.addEventListener('visibilitychange', checkSharedSession)
    window.addEventListener('urumi-session-expired', expire)
    const timer = window.setInterval(tick, 15_000)
    return () => {
      generation.current += 1
      for (const event of events) window.removeEventListener(event, activity)
      window.removeEventListener('urumi-session-expired', expire)
      document.removeEventListener('visibilitychange', checkSharedSession)
      channel?.close()
      window.clearInterval(timer)
    }
  }, [user.id, refresh, expire])

  async function signedIn() {
    generation.current += 1
    expired.current = false
    deadline.current = null
    inFlight.current = null
    return await refresh()
  }
  return {
    ...status,
    continueSession: () => refresh(true),
    checkSession: () => refresh(),
    requestSignIn: expire,
    signedIn,
  }
}
