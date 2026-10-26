import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import ConfirmDialog from './ConfirmDialog.jsx'
import useConfirmation from './useConfirmation.js'

const buttonClass = 'ui-button'

const endpoint =
  import.meta.env.VITE_MCP_URL ||
  (import.meta.env.DEV ? 'http://127.0.0.1:8003/mcp' : new URL('/mcp', window.location.origin).href)

function displayDate(value) {
  if (!value) return 'Never'

  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  }).format(new Date(value))
}

export default function ApiKeys({ onBusyChange }) {
  const confirmation = useConfirmation()
  const [keys, setKeys] = useState([])
  const [name, setName] = useState('')
  const [issued, setIssued] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const inFlight = useRef(false)
  const keyField = useRef(null)

  useEffect(() => {
    let active = true
    const controller = new AbortController()

    async function load() {
      try {
        const data = await api('/keys', {
          signal: controller.signal,
        })
        if (active) setKeys(data)
      } catch (err) {
        if (active && err.name !== 'AbortError') {
          setError(err.message)
        }
      } finally {
        if (active) setLoading(false)
      }
    }

    load()

    return () => {
      active = false
      controller.abort()
    }
  }, [])

  async function perform(action) {
    if (inFlight.current) return

    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setError('')
    setNotice('')

    try {
      await action()
    } catch (err) {
      setError(
        err.status
          ? err.message
          : `${err.message} Refresh the key list before repeating this request.`
      )
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  async function createKey(event) {
    event.preventDefault()
    if (!name.trim() || issued || loading) return

    await perform(async () => {
      const created = await api('/keys', {
        method: 'POST',
        body: { name: name.trim() },
      })

      const { key, ...metadata } = created

      setIssued({ id: created.id, key })
      setKeys((previous) => [metadata, ...previous.filter((item) => item.id !== metadata.id)])
      setName('')
      setNotice('API key created.')
    })
  }

  async function refresh() {
    await perform(async () => {
      setKeys(await api('/keys'))
      setNotice('Key list refreshed.')
    })
  }

  async function revokeKey(item) {
    if (inFlight.current || loading) return
    if (
      !(await confirmation.confirm({
        title: 'Revoke API key?',
        description: `Clients using “${item.name}” will lose access. You can create a new key later, but this key cannot be restored.`,
        label: 'Revoke key',
      }))
    )
      return
    await perform(async () => {
      await api(`/keys/${item.id}`, { method: 'DELETE' })

      setKeys((previous) =>
        previous.map((key) =>
          key.id === item.id ? { ...key, revoked_at: new Date().toISOString() } : key
        )
      )

      setIssued((previous) => (previous?.id === item.id ? null : previous))
      setNotice(`Revoked "${item.name}".`)
    })
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(issued.key)
      setNotice('Key copied.')
    } catch {
      keyField.current?.focus()
      keyField.current?.select()
      setNotice('Key selected. Press Ctrl+C to copy it manually.')
    }
  }

  return (
    <section className="ui-panel">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">My API keys</h1>
          <p className="mt-2 text-sm leading-6 text-neutral-500">
            Connect an external MCP client using a key from your account.
          </p>
        </div>

        <button type="button" onClick={refresh} disabled={busy || loading} className={buttonClass}>
          Refresh
        </button>
      </header>

      <div className="mt-6 rounded-xl border border-neutral-200 bg-neutral-50 p-4">
        <p className="text-xs font-semibold text-neutral-500">MCP endpoint</p>
        <code className="mt-2 block break-all text-sm">{endpoint}</code>
      </div>

      <form onSubmit={createKey} className="mt-6 space-y-3">
        <label htmlFor="key-name" className="block text-sm font-semibold">
          Key name
        </label>

        <div className="flex flex-col gap-3 sm:flex-row">
          <input
            id="key-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={100}
            placeholder="For example, Desktop MCP client"
            required
            disabled={busy || loading || Boolean(issued)}
            className="ui-input min-w-0 flex-1"
          />

          <button
            type="submit"
            disabled={busy || loading || Boolean(issued) || !name.trim()}
            className="ui-primary"
          >
            {busy ? 'Working…' : 'Create key'}
          </button>
        </div>
      </form>

      {issued && (
        <section className="mt-6 rounded-xl border border-neutral-400 bg-neutral-50 p-4">
          <h2 className="text-sm font-bold">Your new API key</h2>
          <p className="mt-2 text-sm leading-6 text-neutral-600">
            Copy it now. You cannot retrieve it after dismissing this panel or refreshing the page.
          </p>

          <textarea
            ref={keyField}
            aria-label="New API key"
            value={issued.key}
            readOnly
            spellCheck={false}
            rows={2}
            onClick={(event) => event.currentTarget.select()}
            className="mt-3 w-full rounded-lg border border-neutral-300 bg-white p-3 font-mono text-xs outline-none focus:ring-2 focus:ring-neutral-300"
          />

          <div className="mt-3 flex flex-wrap gap-3">
            <button type="button" onClick={copyKey} disabled={busy} className={buttonClass}>
              Copy key
            </button>

            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setIssued(null)
                setNotice('Key dismissed.')
              }}
              className={buttonClass}
            >
              Dismiss key
            </button>
          </div>
        </section>
      )}

      {error && (
        <p
          role="alert"
          className="mt-5 rounded-xl border border-neutral-400 bg-neutral-100 p-4 text-sm"
        >
          {error}
        </p>
      )}

      {notice && (
        <p role="status" className="mt-5 text-sm text-neutral-600">
          {notice}
        </p>
      )}

      {loading ? (
        <p role="status" className="mt-8 text-sm text-neutral-500">
          Loading keys…
        </p>
      ) : (
        <div className="mt-7 overflow-x-auto rounded-xl border border-neutral-200">
          <table className="ui-table">
            <thead className="border-b border-neutral-200 text-xs text-neutral-500">
              <tr>
                <th scope="col" className="px-3 py-3">
                  Name
                </th>
                <th scope="col" className="px-3 py-3">
                  Prefix
                </th>
                <th scope="col" className="px-3 py-3">
                  Created · IST
                </th>
                <th scope="col" className="px-3 py-3">
                  Last used · IST
                </th>
                <th scope="col" className="px-3 py-3">
                  Status
                </th>
                <th scope="col" className="px-3 py-3">
                  Action
                </th>
              </tr>
            </thead>

            <tbody className="divide-y divide-neutral-100">
              {keys.map((item) => (
                <tr key={item.id}>
                  <td className="max-w-52 break-words px-3 py-4 font-medium">{item.name}</td>
                  <td className="px-3 py-4 font-mono text-xs">{item.prefix}…</td>
                  <td className="px-3 py-4 text-xs">{displayDate(item.created_at)}</td>
                  <td className="px-3 py-4 text-xs">{displayDate(item.last_used_at)}</td>
                  <td className="px-3 py-4">
                    <span className="rounded bg-neutral-100 px-2 py-1 text-xs">
                      {item.revoked_at ? 'Revoked' : 'Active'}
                    </span>
                  </td>
                  <td className="px-3 py-4">
                    {!item.revoked_at && (
                      <button
                        type="button"
                        onClick={() => revokeKey(item)}
                        disabled={busy}
                        className={buttonClass}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}

              {!keys.length && (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-neutral-500">
                    You have no API keys yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      <ConfirmDialog request={confirmation.request} onFinish={confirmation.finish} />
    </section>
  )
}
