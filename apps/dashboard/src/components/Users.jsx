import { useCallback, useEffect, useRef, useState } from 'react'
import MemberPermissions from './MemberPermissions.jsx'
import MemberKeys from './MemberKeys.jsx'
import { api } from '../api.js'
import ConfirmDialog from './ConfirmDialog.jsx'
import useConfirmation from './useConfirmation.js'

const inputClass = 'ui-input'

const buttonClass = 'ui-button'

function dateText(value) {
  if (!value) return '-'
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  }).format(new Date(value))
}

function accountStatus(user) {
  if (!user.active) return 'Inactive'
  if (user.registered) return 'Registered'
  if (user.invite_expires_at && new Date(user.invite_expires_at).getTime() <= Date.now())
    return 'Invitation expired'
  return 'Invitation pending'
}

export default function Users({ active, currentUser, onBusyChange }) {
  const confirmation = useConfirmation()
  const [users, setUsers] = useState([])
  const [tab, setTab] = useState('members')
  const [email, setEmail] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [permissionBusy, setPermissionBusy] = useState(false)
  const [keyBusy, setKeyBusy] = useState(false)
  const [error, setError] = useState('')
  const [issued, setIssued] = useState(null)
  const [copyMessage, setCopyMessage] = useState('')
  const [revision, setRevision] = useState(0)
  const sequence = useRef(0)
  const inFlight = useRef(false)

  const handlePermissionBusy = useCallback(
    (value) => {
      setPermissionBusy(value)
      onBusyChange(value)
    },
    [onBusyChange]
  )

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    const requestId = ++sequence.current

    async function load() {
      setLoading(true)
      setError('')
      try {
        const result = await api('/users', { signal: controller.signal })
        if (requestId === sequence.current) setUsers(result)
      } catch (err) {
        if (err.name !== 'AbortError' && requestId === sequence.current) setError(err.message)
      } finally {
        if (requestId === sequence.current) setLoading(false)
      }
    }

    load()
    return () => {
      controller.abort()
      if (sequence.current === requestId) sequence.current += 1
    }
  }, [active, revision])

  async function invite(address) {
    if (inFlight.current || issued || permissionBusy || keyBusy) return
    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setError('')
    setCopyMessage('')

    try {
      const result = await api('/users/invite', {
        method: 'POST',
        body: { email: address.trim().toLowerCase() },
      })

      // Keep the token out of the initial page request URL.
      const link = new URL(result.invite_url)
      link.searchParams.delete('token')
      link.hash = new URLSearchParams({
        token: result.invite_token,
      }).toString()

      sequence.current += 1
      setLoading(false)
      setUsers((current) =>
        [...current.filter((item) => item.id !== result.user.id), result.user].sort(
          (a, b) => a.id - b.id
        )
      )

      setIssued({
        email: result.user.email,
        url: link.toString(),
        expiresAt: result.expires_at,
      })
      setEmail('')
    } catch (err) {
      setError(
        err.status
          ? err.message
          : 'The result is uncertain. Refresh the list before generating another invitation.'
      )
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  const handleKeyBusy = useCallback(
    (value) => {
      setKeyBusy(value)
      onBusyChange(value)
    },
    [onBusyChange]
  )

  async function changeActive(user) {
    if (inFlight.current || permissionBusy || keyBusy || issued) return
    const warning = user.active
      ? `Disable ${user.email}? All keys and sessions will be revoked. Chats, tool permissions and audit records are kept.`
      : `Enable ${user.email}? Previous keys and sessions stay revoked. The member must log in again and create a new key.`
    if (
      !(await confirmation.confirm({
        title: user.active ? 'Disable account?' : 'Enable account?',
        description: warning,
        label: user.active ? 'Disable account' : 'Enable account',
      }))
    )
      return
    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setError('')
    try {
      const result = await api(`/admin/users/${user.id}/active`, {
        method: 'PATCH',
        body: { active: !user.active },
      })
      sequence.current += 1
      setUsers((current) => current.map((item) => (item.id === user.id ? result.user : item)))
      setRevision((value) => value + 1)
    } catch (err) {
      setError(
        err.status
          ? err.message
          : 'The result is uncertain. Refresh users before changing this account again.'
      )
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(issued.url)
      setCopyMessage('Invitation link copied.')
    } catch {
      setCopyMessage('Select the link below and copy it manually.')
    }
  }

  return (
    <section className="ui-panel space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[22px] font-semibold tracking-tight">Users & invitations</h2>
          <p className="mt-1 text-sm text-neutral-500">
            {tab === 'members'
              ? 'Manage accounts and share invitation links privately.'
              : tab === 'permissions'
                ? 'Choose which tools each member can use.'
                : 'Manage external client access and review security events.'}
          </p>
        </div>
        <button
          type="button"
          disabled={busy || permissionBusy || keyBusy || loading}
          onClick={() => setRevision((value) => value + 1)}
          className={buttonClass}
        >
          Refresh
        </button>
      </div>

      {error && (
        <p role="alert" className="rounded-lg bg-neutral-100 p-3 text-sm">
          {error}
        </p>
      )}

      <div className="grid grid-cols-3 gap-1 border-b border-neutral-200 pb-3 sm:flex">
        {[
          { id: 'members', label: 'Members' },
          { id: 'permissions', label: 'Tool permissions' },
          { id: 'keys', label: 'Member API keys' },
        ].map((item) => (
          <button
            key={item.id}
            type="button"
            disabled={busy || permissionBusy || keyBusy || Boolean(issued)}
            onClick={() => setTab(item.id)}
            aria-pressed={tab === item.id}
            className={`min-w-0 rounded-lg px-2 py-2.5 text-xs font-medium sm:px-4 disabled:opacity-50 ${tab === item.id ? 'bg-neutral-900 text-white' : 'text-neutral-500 hover:bg-neutral-100'}`}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div hidden={tab !== 'members'} className="space-y-6">
        {!loading && (
          <div className="grid grid-cols-3 gap-2 sm:gap-3">
            {[
              {
                label: 'Team members',
                value: users.filter((item) => item.role === 'member').length,
              },
              { label: 'Enabled accounts', value: users.filter((item) => item.active).length },
              {
                label: 'Pending invitations',
                value: users.filter((item) => item.active && !item.registered).length,
              },
            ].map((item) => (
              <div
                key={item.label}
                className="rounded-xl border border-neutral-200 bg-neutral-50/50 px-3 py-3 sm:px-5 sm:py-4"
              >
                <p className="text-[10px] font-semibold uppercase tracking-wider text-neutral-400">
                  {item.label}
                </p>
                <p className="mt-2 text-2xl font-semibold tracking-tight">{item.value}</p>
              </div>
            ))}
          </div>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault()
            invite(email)
          }}
          className="flex flex-wrap items-end gap-3"
        >
          <label className="grid min-w-60 flex-1 gap-2 text-sm font-semibold">
            Member email
            <input
              type="email"
              required
              maxLength={254}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={busy || permissionBusy || keyBusy || Boolean(issued)}
              placeholder="member@example.com"
              className={inputClass}
            />
          </label>
          <button
            type="submit"
            disabled={busy || permissionBusy || keyBusy || Boolean(issued)}
            className={`${buttonClass} bg-neutral-900 text-white hover:bg-neutral-800`}
          >
            {busy ? 'Generating…' : 'Generate invitation'}
          </button>
        </form>

        {issued && (
          <div className="space-y-3 rounded-xl border border-neutral-300 bg-neutral-50 p-4">
            <p className="break-all font-semibold">Invitation for {issued.email}</p>
            <p className="text-sm text-neutral-600">
              Expires · IST: {dateText(issued.expiresAt)}. This link can be used once. Anyone
              holding it can accept the invitation.
            </p>
            <label className="grid gap-2 text-sm">
              Private invitation link
              <textarea
                readOnly
                rows={3}
                value={issued.url}
                onFocus={(event) => event.target.select()}
                className={inputClass}
              />
            </label>
            <div className="flex flex-wrap gap-2">
              <button type="button" onClick={copyLink} className={buttonClass}>
                Copy link
              </button>
              <button
                type="button"
                onClick={() => {
                  setIssued(null)
                  setCopyMessage('')
                }}
                className={buttonClass}
              >
                Dismiss link
              </button>
            </div>
            {copyMessage && (
              <p role="status" className="text-sm">
                {copyMessage}
              </p>
            )}
            <p className="text-xs text-neutral-500">
              Copy the link before dismissing it. It is not stored in browser storage. No email is
              sent automatically.
            </p>
          </div>
        )}

        {loading ? (
          <p role="status" className="text-sm text-neutral-500">
            Loading users…
          </p>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-neutral-200">
            <table className="ui-table">
              <thead className="border-b border-neutral-200 text-neutral-500">
                <tr>
                  <th className="p-3">Email</th>
                  <th className="p-3">Role</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Invitation expires · IST</th>
                  <th className="p-3">Action</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id} className="border-b border-neutral-100">
                    <td className="break-all p-3">{user.email}</td>
                    <td className="p-3 capitalize">{user.role}</td>
                    <td className="p-3">
                      <span
                        className={`inline-flex items-center gap-1.5 rounded border px-2.5 py-0.5 text-[10px] font-medium ${user.active ? 'border-neutral-200 bg-neutral-50 text-neutral-700' : 'border-neutral-300 bg-neutral-200 text-neutral-600'}`}
                      >
                        <span className="h-1 w-1 rounded-full bg-current" />
                        {accountStatus(user)}
                      </span>
                    </td>
                    <td className="p-3">{dateText(user.invite_expires_at)}</td>
                    <td className="space-y-2 p-3">
                      {user.id !== currentUser.id && (
                        <button
                          type="button"
                          disabled={busy || permissionBusy || keyBusy || Boolean(issued)}
                          onClick={() => changeActive(user)}
                          className={buttonClass}
                        >
                          {user.active ? 'Disable account' : 'Enable account'}
                        </button>
                      )}
                      {user.role === 'member' && user.active && !user.registered && (
                        <button
                          type="button"
                          disabled={busy || permissionBusy || keyBusy || Boolean(issued)}
                          onClick={() => invite(user.email)}
                          className={buttonClass}
                        >
                          Replace invitation
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div hidden={tab !== 'keys'}>
        <MemberKeys
          users={users}
          active={active && tab === 'keys'}
          locked={busy || permissionBusy || Boolean(issued)}
          onBusyChange={handleKeyBusy}
        />
      </div>

      <div hidden={tab !== 'permissions'}>
        <MemberPermissions
          users={users}
          active={active && tab === 'permissions'}
          locked={busy || keyBusy}
          onBusyChange={handlePermissionBusy}
        />
      </div>

      <p hidden={tab !== 'members'} className="text-xs text-neutral-500">
        Replacing an invitation invalidates its previous link. Registered accounts cannot be invited
        again.
      </p>
      <ConfirmDialog request={confirmation.request} onFinish={confirmation.finish} />
    </section>
  )
}
