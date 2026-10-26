import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import ConfirmDialog from './ConfirmDialog.jsx'
import useConfirmation from './useConfirmation.js'

const buttonClass = 'ui-button'
const inputClass = 'ui-input'

function dateText(value) {
  return value
    ? new Intl.DateTimeFormat('en-IN', {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone: 'Asia/Kolkata',
      }).format(new Date(value))
    : 'Never'
}

export default function MemberKeys({ users, active, locked, onBusyChange }) {
  const confirmation = useConfirmation()
  const [memberId, setMemberId] = useState('')
  const [name, setName] = useState('External MCP access')
  const [keys, setKeys] = useState([])
  const [events, setEvents] = useState([])
  const [moreKeys, setMoreKeys] = useState(null)
  const [moreEvents, setMoreEvents] = useState(null)
  const [loadedFor, setLoadedFor] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [issued, setIssued] = useState(null)
  const [copyMessage, setCopyMessage] = useState('')
  const [revision, setRevision] = useState(0)
  const sequence = useRef(0)
  const inFlight = useRef(false)
  const selected = users.find((user) => String(user.id) === memberId)

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    const version = ++sequence.current
    async function load() {
      setLoading(true)
      setError('')
      try {
        const [keyData, eventData] = await Promise.all([
          memberId
            ? api(`/admin/users/${memberId}/keys`, { signal: controller.signal })
            : Promise.resolve(null),
          api('/admin/security-events', { signal: controller.signal }),
        ])
        if (controller.signal.aborted || version !== sequence.current) return
        setKeys(keyData?.items || [])
        setMoreKeys(keyData?.next_before_id || null)
        setLoadedFor(memberId)
        setEvents(eventData.items)
        setMoreEvents(eventData.next_before_id)
      } catch (err) {
        if (err.name !== 'AbortError' && version === sequence.current) {
          setLoadedFor('')
          setError(err.message)
        }
      } finally {
        if (version === sequence.current) setLoading(false)
      }
    }
    load()
    return () => {
      controller.abort()
      sequence.current += 1
    }
  }, [active, memberId, revision, selected?.active, selected?.registered])

  async function perform(operation) {
    if (inFlight.current || locked || loading) return
    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setError('')
    try {
      await operation()
    } catch (err) {
      setError(
        !err.status || err.status >= 500
          ? 'The result is uncertain. Refresh keys and security events before retrying. A new key may already exist.'
          : err.message
      )
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  async function issue(event) {
    event.preventDefault()
    if (issued || !selected?.active || !selected?.registered || loadedFor !== memberId) return
    await perform(async () => {
      const key = await api(`/admin/users/${memberId}/keys`, {
        method: 'POST',
        body: { name: name.trim() },
      })
      setIssued({ ...key, email: selected.email })
      setCopyMessage('')
      setRevision((value) => value + 1)
    })
  }

  async function revoke(key) {
    if (issued || loadedFor !== memberId || inFlight.current || locked || loading) return
    if (
      !(await confirmation.confirm({
        title: 'Revoke member key?',
        description: `Revoke “${key.name}” for ${selected.email}? Clients using this key will lose access.`,
        label: 'Revoke key',
      }))
    )
      return
    await perform(async () => {
      await api(`/admin/users/${memberId}/keys/${key.id}`, { method: 'DELETE' })
      setRevision((value) => value + 1)
    })
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(issued.key)
      setCopyMessage('Key copied. Share it privately with the member.')
    } catch {
      setCopyMessage('Select the key and copy it manually.')
    }
  }

  async function loadMore(kind) {
    await perform(async () => {
      if (kind === 'keys') {
        const data = await api(`/admin/users/${memberId}/keys?before_id=${moreKeys}`)
        setKeys((current) => [
          ...new Map([...current, ...data.items].map((item) => [item.id, item])).values(),
        ])
        setMoreKeys(data.next_before_id)
      } else {
        const data = await api(`/admin/security-events?before_id=${moreEvents}`)
        setEvents((current) => [
          ...new Map([...current, ...data.items].map((item) => [item.id, item])).values(),
        ])
        setMoreEvents(data.next_before_id)
      }
    })
  }

  const disabled = busy || locked || loading
  return (
    <section className="space-y-4 border-t border-neutral-200 pt-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold tracking-tight">Member API keys</h3>
          <p className="mt-1 text-sm text-neutral-500">
            Issue and revoke a member’s external MCP credentials. Tool permissions still apply.
          </p>
        </div>
        <button
          type="button"
          disabled={disabled}
          onClick={() => setRevision((value) => value + 1)}
          className={buttonClass}
        >
          Refresh keys & events
        </button>
      </div>
      {error && (
        <p role="alert" className="rounded-lg bg-neutral-100 p-3 text-sm">
          {error}
        </p>
      )}
      <label className="grid gap-2 text-sm font-semibold">
        Member
        <select
          value={memberId}
          disabled={disabled || Boolean(issued)}
          onChange={(event) => setMemberId(event.target.value)}
          className={inputClass}
        >
          <option value="">Select a member</option>
          {users
            .filter((user) => user.role === 'member')
            .map((user) => (
              <option key={user.id} value={String(user.id)}>
                {user.email}
                {!user.active ? ' - disabled' : !user.registered ? ' - awaiting registration' : ''}
              </option>
            ))}
        </select>
      </label>
      {selected && (
        <>
          <form onSubmit={issue} className="flex flex-wrap items-end gap-3">
            <label className="grid flex-1 gap-2 text-sm">
              Key name
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={100}
                disabled={disabled || Boolean(issued)}
                className={inputClass}
              />
            </label>
            <button
              type="submit"
              disabled={
                disabled ||
                Boolean(issued) ||
                loadedFor !== memberId ||
                !selected.active ||
                !selected.registered ||
                !name.trim()
              }
              className={`${buttonClass} bg-neutral-900 text-white hover:bg-neutral-800`}
            >
              Issue member key
            </button>
          </form>
          {(!selected.active || !selected.registered) && (
            <p className="text-xs text-neutral-500">
              The member must be enabled and registered before a key can be issued.
            </p>
          )}
          {loading && (
            <p role="status" className="text-sm">
              Loading keys…
            </p>
          )}
          {loadedFor === memberId && (
            <div className="overflow-x-auto rounded-xl border border-neutral-200">
              <table className="ui-table">
                <thead>
                  <tr className="border-b text-neutral-500">
                    <th className="p-2">Name</th>
                    <th className="p-2">Prefix</th>
                    <th className="p-2">Created · IST</th>
                    <th className="p-2">Last used · IST</th>
                    <th className="p-2">Status</th>
                    <th className="p-2">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((key) => (
                    <tr key={key.id} className="border-b border-neutral-100">
                      <td className="p-2">{key.name}</td>
                      <td className="p-2 font-mono">{key.prefix}…</td>
                      <td className="p-2">{dateText(key.created_at)}</td>
                      <td className="p-2">{dateText(key.last_used_at)}</td>
                      <td className="p-2">{key.revoked_at ? 'Revoked' : 'Active'}</td>
                      <td className="p-2">
                        {!key.revoked_at && (
                          <button
                            type="button"
                            disabled={disabled || Boolean(issued)}
                            onClick={() => revoke(key)}
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
                      <td colSpan={6} className="p-3 text-neutral-500">
                        No keys for this member.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
          {moreKeys && (
            <button
              type="button"
              disabled={disabled}
              onClick={() => loadMore('keys')}
              className={buttonClass}
            >
              Load older keys
            </button>
          )}
        </>
      )}
      {issued && (
        <div className="space-y-3 rounded-xl border border-neutral-300 bg-neutral-50 p-4">
          <p className="font-semibold">New key for {issued.email}</p>
          <p className="text-sm">
            Copy and share this key privately. After dismissal, only its prefix can be viewed.
          </p>
          <textarea
            readOnly
            value={issued.key}
            rows={2}
            onFocus={(event) => event.target.select()}
            className={inputClass}
            aria-label="New member API key"
          />
          <div className="flex gap-2">
            <button type="button" onClick={copyKey} className={buttonClass}>
              Copy key
            </button>
            <button
              type="button"
              onClick={() => {
                setIssued(null)
                setCopyMessage('')
              }}
              className={buttonClass}
            >
              Dismiss key
            </button>
          </div>
          {copyMessage && (
            <p role="status" className="text-sm">
              {copyMessage}
            </p>
          )}
        </div>
      )}
      <div className="space-y-3 border-t border-neutral-200 pt-4">
        <h3 className="font-semibold">Access security events</h3>
        <p className="text-xs text-neutral-500">
          Account changes and key issuance/revocation. Tool calls remain in Audit history.
        </p>
        <div className="overflow-x-auto rounded-xl border border-neutral-200">
          <table className="ui-table text-xs">
            <thead>
              <tr>
                <th className="p-2">When · IST</th>
                <th className="p-2">Performed by</th>
                <th className="p-2">Account</th>
                <th className="p-2">Action</th>
              </tr>
            </thead>
            <tbody>
              {events.map((item) => (
                <tr key={item.id} className="border-t border-neutral-100">
                  <td className="p-2">{dateText(item.created_at)}</td>
                  <td className="break-all p-2">{item.actor_email || 'Unknown'}</td>
                  <td className="break-all p-2">{item.target_email || 'Unknown'}</td>
                  <td className="p-2">{item.action}</td>
                </tr>
              ))}
              {!events.length && (
                <tr>
                  <td colSpan={4} className="p-2">
                    No recorded access changes yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {moreEvents && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => loadMore('events')}
            className={buttonClass}
          >
            Load older events
          </button>
        )}
      </div>
      <ConfirmDialog request={confirmation.request} onFinish={confirmation.finish} />
    </section>
  )
}
