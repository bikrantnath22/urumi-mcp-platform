import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const emptyFilters = {
  source: '',
  outcome: '',
  server_slug: '',
  tool: '',
  user_id: '',
}

const fieldClass = 'ui-input'

const buttonClass = 'ui-button'

const sourceLabels = {
  chat: 'Chat',
  mcp: 'MCP client',
  dashboard: 'Dashboard',
}

function auditPath(filters, role, beforeId) {
  const query = new URLSearchParams({ limit: '20' })

  for (const [name, value] of Object.entries(filters)) {
    if (name === 'user_id' && role !== 'admin') continue
    if (value.trim()) query.set(name, value.trim())
  }

  if (beforeId) query.set('before_id', String(beforeId))
  return `/audit?${query.toString()}`
}

function displayDate(value) {
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  }).format(new Date(value))
}

export default function Audit({ user, active }) {
  const [draft, setDraft] = useState({ ...emptyFilters })
  const [filters, setFilters] = useState({ ...emptyFilters })
  const [refreshCount, setRefreshCount] = useState(0)
  const [items, setItems] = useState([])
  const [nextBeforeId, setNextBeforeId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')

  const requestVersion = useRef(0)
  const olderController = useRef(null)

  useEffect(() => {
    if (!active) return

    const version = ++requestVersion.current
    const controller = new AbortController()

    async function load() {
      setLoading(true)
      setLoadingMore(false)
      setItems([])
      setNextBeforeId(null)
      setError('')
      try {
        const data = await api(auditPath(filters, user.role), {
          signal: controller.signal,
        })

        if (version === requestVersion.current) {
          setItems(data.items)
          setNextBeforeId(data.next_before_id)
        }
      } catch (err) {
        if (version === requestVersion.current && err.name !== 'AbortError') {
          setError(err.message)
        }
      } finally {
        if (version === requestVersion.current) setLoading(false)
      }
    }

    load()

    return () => {
      requestVersion.current += 1
      controller.abort()
      olderController.current?.abort()
    }
  }, [active, filters, refreshCount, user.role])

  async function loadOlder() {
    if (loading || loadingMore || !nextBeforeId || olderController.current) return

    const version = requestVersion.current
    const controller = new AbortController()
    olderController.current = controller

    setLoadingMore(true)
    setError('')

    try {
      const data = await api(auditPath(filters, user.role, nextBeforeId), {
        signal: controller.signal,
      })

      if (version === requestVersion.current) {
        setItems((previous) => {
          const seen = new Set(previous.map((item) => item.id))
          return [...previous, ...data.items.filter((item) => !seen.has(item.id))]
        })
        setNextBeforeId(data.next_before_id)
      }
    } catch (err) {
      if (version === requestVersion.current && err.name !== 'AbortError') {
        setError(err.message)
      }
    } finally {
      if (olderController.current === controller) {
        olderController.current = null
      }
      if (version === requestVersion.current) setLoadingMore(false)
    }
  }

  function changeFilter(event) {
    const { name, value } = event.target
    setDraft((previous) => ({ ...previous, [name]: value }))
  }

  function applyFilters(event) {
    event.preventDefault()
    if (loading || loadingMore) return
    setFilters({ ...draft })
  }

  function clearFilters() {
    setDraft({ ...emptyFilters })
    setFilters({ ...emptyFilters })
  }

  const waiting = loading || loadingMore

  return (
    <section className="ui-panel">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">Audit history</h1>
          <p className="mt-2 text-sm text-neutral-500">
            {user.role === 'admin'
              ? 'Review tool calls across the workspace.'
              : 'Review your tool calls.'}
          </p>
        </div>

        <button
          type="button"
          disabled={waiting}
          onClick={() => setRefreshCount((value) => value + 1)}
          className={buttonClass}
        >
          Refresh
        </button>
      </header>

      <form onSubmit={applyFilters} className="mt-6 space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="grid gap-2 text-xs font-semibold text-neutral-600">
            Source
            <select
              name="source"
              value={draft.source}
              onChange={changeFilter}
              className={fieldClass}
            >
              <option value="">All sources</option>
              <option value="chat">Chat</option>
              <option value="mcp">MCP client</option>
              <option value="dashboard">Dashboard</option>
            </select>
          </label>

          <label className="grid gap-2 text-xs font-semibold text-neutral-600">
            Outcome
            <select
              name="outcome"
              value={draft.outcome}
              onChange={changeFilter}
              className={fieldClass}
            >
              <option value="">All outcomes</option>
              <option value="success">Success</option>
              <option value="error">Error</option>
              <option value="denied">Denied</option>
            </select>
          </label>

          <label className="grid gap-2 text-xs font-semibold text-neutral-600">
            Server
            <input
              name="server_slug"
              value={draft.server_slug}
              onChange={changeFilter}
              maxLength={32}
              placeholder="For example, notes"
              className={fieldClass}
            />
          </label>

          <label className="grid gap-2 text-xs font-semibold text-neutral-600">
            Tool name
            <input
              name="tool"
              value={draft.tool}
              onChange={changeFilter}
              maxLength={256}
              placeholder="For example, notes__get_note"
              className={fieldClass}
            />
          </label>

          {user.role === 'admin' && (
            <label className="grid gap-2 text-xs font-semibold text-neutral-600">
              User ID
              <input
                name="user_id"
                type="number"
                min={1}
                max={2147483647}
                step={1}
                value={draft.user_id}
                onChange={changeFilter}
                placeholder="All users"
                className={fieldClass}
              />
            </label>
          )}
        </div>

        <div className="flex gap-3">
          <button type="submit" disabled={waiting} className="ui-primary">
            Apply filters
          </button>

          <button type="button" onClick={clearFilters} disabled={waiting} className={buttonClass}>
            Clear
          </button>
        </div>
      </form>

      {error && (
        <p
          role="alert"
          className="mt-5 rounded-xl border border-neutral-400 bg-neutral-100 p-4 text-sm"
        >
          {error}
        </p>
      )}

      {loading ? (
        <p role="status" className="mt-8 text-sm text-neutral-500">
          Loading audit records…
        </p>
      ) : (
        <>
          <div className="mt-7 overflow-x-auto rounded-xl border border-neutral-200">
            <table className="ui-table min-w-[960px]">
              <thead className="border-b border-neutral-200 text-xs text-neutral-500">
                <tr>
                  <th scope="col" className="px-3 py-3">
                    Time · IST
                  </th>
                  <th scope="col" className="px-3 py-3">
                    User
                  </th>
                  <th scope="col" className="px-3 py-3">
                    Server
                  </th>
                  <th scope="col" className="px-3 py-3">
                    Tool
                  </th>
                  <th scope="col" className="px-3 py-3">
                    Source
                  </th>
                  <th scope="col" className="px-3 py-3">
                    Outcome
                  </th>
                  <th scope="col" className="px-3 py-3">
                    Duration
                  </th>
                  <th scope="col" className="px-3 py-3">
                    Error
                  </th>
                </tr>
              </thead>

              <tbody className="divide-y divide-neutral-100">
                {items.map((item) => (
                  <tr key={item.id}>
                    <td className="px-3 py-4 text-xs">{displayDate(item.created_at)}</td>
                    <td className="px-3 py-4 text-xs">
                      <p>
                        {item.user_email || (item.user_id ? `User ${item.user_id}` : 'Anonymous')}
                      </p>
                      {item.user_id != null && (
                        <p className="mt-1 text-neutral-500">ID {item.user_id}</p>
                      )}
                    </td>
                    <td className="px-3 py-4 text-xs">{item.server_slug || '-'}</td>
                    <td className="max-w-60 break-all px-3 py-4 font-mono text-xs">{item.tool}</td>
                    <td className="px-3 py-4 text-xs">
                      {sourceLabels[item.source] || item.source}
                    </td>
                    <td className="px-3 py-4">
                      <span className="rounded bg-neutral-100 px-2 py-1 text-xs capitalize">
                        {item.outcome}
                      </span>
                    </td>
                    <td className="px-3 py-4 text-xs">{item.duration_ms} ms</td>
                    <td className="px-3 py-4 text-xs">
                      {item.error ? (
                        <details className="max-w-64">
                          <summary className="cursor-pointer underline underline-offset-2">
                            View error
                          </summary>
                          <p className="mt-2 break-words leading-5">{item.error}</p>
                        </details>
                      ) : (
                        '-'
                      )}
                    </td>
                  </tr>
                ))}

                {!items.length && !error && (
                  <tr>
                    <td colSpan={8} className="px-3 py-8 text-center text-neutral-500">
                      No audit records match these filters.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <footer className="mt-5 flex items-center justify-between gap-4">
            <p className="text-xs text-neutral-500">{items.length} records loaded</p>
            <button
              type="button"
              onClick={loadOlder}
              disabled={waiting || !nextBeforeId}
              className={buttonClass}
            >
              {loadingMore ? 'Loading…' : 'Load older'}
            </button>
          </footer>
        </>
      )}
    </section>
  )
}
