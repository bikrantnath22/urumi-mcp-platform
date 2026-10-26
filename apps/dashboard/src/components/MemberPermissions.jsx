import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import PermissionGroups from './PermissionGroups.jsx'

const buttonClass = 'ui-button'

export default function MemberPermissions({ users, active, locked, onBusyChange }) {
  const [memberId, setMemberId] = useState('')
  const [catalog, setCatalog] = useState([])
  const [grants, setGrants] = useState([])
  const [loadedFor, setLoadedFor] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const sequence = useRef(0)
  const inFlight = useRef(false)

  useEffect(() => {
    if (!active || !memberId) return
    const controller = new AbortController()
    const requestId = ++sequence.current

    async function load() {
      setLoading(true)
      setError('')
      try {
        const [tools, permissions] = await Promise.all([
          api('/tools', { signal: controller.signal }),
          api(`/users/${memberId}/permissions`, { signal: controller.signal }),
        ])
        if (requestId !== sequence.current) return
        setCatalog(tools.tools)
        setGrants(permissions.tools)
        setLoadedFor(memberId)
      } catch (err) {
        if (err.name !== 'AbortError' && requestId === sequence.current) {
          setLoadedFor('')
          setError(err.message)
        }
      } finally {
        if (requestId === sequence.current) setLoading(false)
      }
    }

    load()
    return () => {
      controller.abort()
      if (sequence.current === requestId) sequence.current += 1
    }
  }, [active, memberId, revision])

  async function changePermission(name, currentlyGranted) {
    if (inFlight.current || locked || loading || loadedFor !== memberId) return
    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setError('')
    try {
      await api(`/users/${memberId}/permissions`, {
        method: currentlyGranted ? 'DELETE' : 'POST',
        body: { name },
      })
      setGrants((current) =>
        currentlyGranted ? current.filter((item) => item.name !== name) : [...current, { name }]
      )
    } catch (err) {
      setError(
        err.status
          ? err.message
          : 'The result is uncertain. Refresh permissions to check the saved state.'
      )
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  const members = users.filter((user) => user.role === 'member' && user.active)
  const ready = memberId && loadedFor === memberId && !loading

  return (
    <section className="space-y-4 border-t border-neutral-200 pt-6">
      <div>
        <h3 className="text-base font-semibold tracking-tight">Member tool permissions</h3>
        <p className="mt-1 text-sm text-neutral-500">
          Open a store or service to grant or revoke its tools. These permissions apply to chat,
          dashboard calls, and their API keys.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="grid min-w-60 flex-1 gap-2 text-sm font-semibold">
          Member
          <select
            value={memberId}
            disabled={busy || locked}
            onChange={(event) => setMemberId(event.target.value)}
            className="rounded-lg border border-neutral-300 bg-white px-3 py-2"
          >
            <option value="">Select a member</option>
            {members.map((user) => (
              <option key={user.id} value={String(user.id)}>
                {user.email}
                {user.registered ? '' : ' (invitation pending)'}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={!memberId || busy || locked || loading}
          onClick={() => setRevision((value) => value + 1)}
          className={buttonClass}
        >
          Refresh permissions
        </button>
      </div>

      {error && (
        <p role="alert" className="rounded-lg bg-neutral-100 p-3 text-sm">
          {error}
        </p>
      )}
      {loading && (
        <p role="status" className="text-sm text-neutral-500">
          Loading tools and permissions…
        </p>
      )}
      {ready && (
        <>
          <p className="text-sm text-neutral-600">{grants.length} tools granted to this member.</p>
          <PermissionGroups
            key={memberId}
            catalog={catalog}
            grants={grants}
            disabled={busy || locked}
            onChange={changePermission}
          />
        </>
      )}
    </section>
  )
}
