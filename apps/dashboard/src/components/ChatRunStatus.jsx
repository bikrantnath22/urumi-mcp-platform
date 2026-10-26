import { useEffect, useState } from 'react'

function RetryNotice({ turn }) {
  const [now, setNow] = useState(Date.now)
  const recorded = Date.parse(turn.created_at || '')
  const deadline = turn.retry_at
    ? Date.parse(turn.retry_at)
    : recorded + (turn.duration_ms || 0) + (turn.retry_after_s || 0) * 1000
  useEffect(() => {
    if (!Number.isFinite(deadline) || Date.now() >= deadline) return
    const timer = setInterval(() => {
      const current = Date.now()
      setNow(current)
      if (current >= deadline) clearInterval(timer)
    }, 1000)
    return () => clearInterval(timer)
  }, [deadline])
  if (!(turn.retry_after_s > 0)) return null
  const remaining = Math.max(0, Math.ceil((deadline - now) / 1000))
  return (
    <p className="mt-2 text-xs text-neutral-600">
      {!Number.isFinite(deadline)
        ? `Suggested wait when recorded: ${turn.retry_after_s} seconds.`
        : remaining > 0
          ? `Suggested wait: ${remaining} seconds. This does not retry the request.`
          : 'The suggested waiting period has elapsed. Provider limits may still apply.'}
    </p>
  )
}

export default function ChatRunStatus({ turn }) {
  if (!turn.error) return null
  const completed = (turn.tool_calls || []).filter((call) => call.status === 'completed').length
  const totalsReady = turn.results_complete === true && completed >= 2 && Boolean(turn.reply)
  const title = totalsReady
    ? 'Totals retrieved; summary unavailable'
    : completed > 0
      ? turn.error_provider === 'groq'
        ? 'Tool results saved; reply unavailable'
        : 'Some tool calls completed'
      : 'Request could not finish'

  return (
    <div role="alert" className="ui-banner">
      <p className="font-semibold text-neutral-900">{title}</p>
      <p className="mt-1">{turn.error}</p>
      {totalsReady && (
        <p className="mt-2 text-xs">
          Both requested totals were retrieved. The saved results are shown above.
        </p>
      )}
      {!totalsReady && completed > 0 && (
        <p className="mt-2 text-xs">
          {completed} tool {completed === 1 ? 'call completed' : 'calls completed'}. Results are
          recorded below. Other requested steps may remain unfinished. Do not repeat completed
          actions.
        </p>
      )}
      {!completed && (turn.tool_calls || []).length > 0 && (
        <p className="mt-2 text-xs">Review recorded tool activity before repeating this request.</p>
      )}
      <RetryNotice turn={turn} />
    </div>
  )
}
