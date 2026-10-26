import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Icon from './Icon.jsx'
import StoreSettings from './StoreSettings.jsx'

const groups = [
  {
    title: 'Login protection',
    description:
      'Successful and failed sign-in attempts count. Existing sessions continue to work.',
    fields: [
      ['login_account_limit', 'Attempts per email address', 1, 100],
      ['login_ip_limit', 'Attempts per client IP', 1, 1000],
      ['login_window_seconds', 'Login window in minutes', 1, 1440],
    ],
  },
  {
    title: 'Chat usage',
    description:
      'New messages count across all conversations. Reading saved messages and replaying a completed request do not count.',
    fields: [
      ['chat_per_minute', 'Messages per user / 60 seconds', 1, 1000],
      ['chat_per_hour', 'Messages per user / 60 minutes', 1, 10000],
      ['chat_global_per_minute', 'Messages across all users / 60 seconds', 1, 10000],
    ],
  },
  {
    title: 'Tool usage',
    description:
      'One allowance per user across chat, dashboard, image uploads and every API key. Each tool invocation counts separately.',
    fields: [['tool_per_minute', 'Tool calls per user / 60 seconds', 1, 10000]],
  },
]

export default function UsageLimits({ active, onBusyChange }) {
  const [record, setRecord] = useState(null)
  const [values, setValues] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [storeBusy, setStoreBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  function handleStoreBusy(value) {
    setStoreBusy(value)
    onBusyChange(value)
  }

  async function load(signal) {
    try {
      const data = await api('/admin/usage-limits', { signal })
      if (signal?.aborted) return
      setRecord(data)
      setValues(data)
      setNotice('')
    } catch (err) {
      if (err.name !== 'AbortError') setError(err.message)
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }
  function refresh() {
    setLoading(true)
    setError('')
    load()
  }
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    api('/admin/usage-limits', { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return
        setRecord(data)
        setValues(data)
        setError('')
        setNotice('')
      })
      .catch((err) => {
        if (err.name !== 'AbortError') setError(err.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [active])

  async function save(event) {
    event.preventDefault()
    if (saving || loading || storeBusy || !record) return
    setSaving(true)
    onBusyChange(true)
    setError('')
    setNotice('')
    const body = { revision: record.revision }
    for (const group of groups) for (const [name] of group.fields) body[name] = Number(values[name])
    try {
      const data = await api('/admin/usage-limits', { method: 'PUT', body })
      setRecord(data)
      setValues(data)
      setNotice('Limits saved. They apply to new requests immediately.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
      onBusyChange(false)
    }
  }

  return (
    <section className="mx-auto max-w-4xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Usage limits</h1>
          <p className="mt-2 text-sm leading-6 text-neutral-500">
            Manage request allowances for admin and member accounts.
          </p>
        </div>
        <button
          type="button"
          onClick={() => refresh()}
          disabled={saving || loading || storeBusy}
          className="ui-button"
        >
          <Icon name="refresh" />
          Refresh
        </button>
      </div>
      <div className="ui-banner text-sm leading-6">
        A window starts with its first accepted request. Requests can use the allowance at any point
        in that window. Raising a limit keeps existing counts; changing the login window starts a
        new login allowance. Provider limits still apply.
      </div>
      {error && (
        <p role="alert" className="ui-banner">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="ui-banner">
          {notice}
        </p>
      )}
      {loading && (
        <p role="status" className="text-sm text-neutral-500">
          Loading limits…
        </p>
      )}
      {values && (
        <form onSubmit={save} className="space-y-5">
          {groups.map((group) => (
            <fieldset
              key={group.title}
              disabled={saving || loading || storeBusy}
              className="rounded-xl border border-neutral-200 bg-white p-5 sm:p-6"
            >
              <legend className="px-2 text-sm font-semibold">{group.title}</legend>
              <p className="mb-5 text-xs leading-6 text-neutral-500">{group.description}</p>
              <div className="grid gap-5 sm:grid-cols-2">
                {group.fields.map(([name, label, min, max]) => (
                  <label key={name} className="grid gap-2 text-xs font-semibold text-neutral-700">
                    {label}
                    <input
                      className="ui-input"
                      type="number"
                      min={min}
                      max={max}
                      step="1"
                      required
                      value={name === 'login_window_seconds' ? values[name] / 60 : values[name]}
                      onChange={(event) => {
                        setValues((previous) => ({
                          ...previous,
                          [name]:
                            name === 'login_window_seconds'
                              ? Number(event.target.value) * 60
                              : event.target.value,
                        }))
                        setNotice('')
                      }}
                    />
                  </label>
                ))}
              </div>
            </fieldset>
          ))}
          <div className="flex flex-wrap items-center justify-between gap-4">
            <p className="text-xs leading-6 text-neutral-500">
              Changes are recorded in security history. No restart is needed.
            </p>
            <button className="ui-primary" type="submit" disabled={saving || loading || storeBusy}>
              {saving ? 'Saving…' : 'Save limits'}
            </button>
          </div>
        </form>
      )}
      {active && <StoreSettings disabled={saving || loading} onBusyChange={handleStoreBusy} />}
    </section>
  )
}
