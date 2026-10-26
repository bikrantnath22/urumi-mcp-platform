import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import AuthFrame from './AuthFrame.jsx'

const inputClass = 'ui-input'

const buttonClass = 'ui-primary w-full'

function invitationToken() {
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const query = new URLSearchParams(window.location.search)
  return fragment.get('token') || query.get('token') || ''
}

export default function AcceptInvite() {
  const [token, setToken] = useState(invitationToken)
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [account, setAccount] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)

  useEffect(() => {
    // The token remains in component memory until setup finishes.
    window.history.replaceState(window.history.state, '', window.location.pathname)
  }, [])

  async function accept(event) {
    event.preventDefault()
    if (inFlight.current) return
    setError('')

    const length = Array.from(password).length
    if (length < 6 || length > 16 || !password.trim()) {
      setError('Choose a password containing 6–16 characters.')
      return
    }
    if (password !== confirmation) {
      setError('Passwords do not match.')
      return
    }

    inFlight.current = true
    setBusy(true)
    try {
      const result = await api('/auth/accept-invite', {
        method: 'POST',
        body: { token, password },
      })
      setAccount(result)
      setToken('')
      setPassword('')
      setConfirmation('')
    } catch (err) {
      setError(
        err.status
          ? err.message
          : 'The result is uncertain. Try signing in with the password you chose before requesting another invitation.'
      )
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  async function goToLogin() {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError('')

    try {
      // Clear any existing browser session before signing in as this member.
      try {
        await api('/auth/logout', { method: 'POST' })
      } catch (err) {
        if (err.status !== 401) throw err
      }
      window.location.replace(`/?email=${encodeURIComponent(account.email)}`)
    } catch (err) {
      setError(err.message)
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <AuthFrame>
      <section className="space-y-5">
        <p className="text-xs font-bold tracking-widest text-neutral-500">URUMI WORKSPACE</p>

        {account ? (
          <>
            <h1 className="text-2xl font-bold">Your account is ready</h1>
            <p className="break-all text-sm text-neutral-600">{account.email}</p>
            <p className="text-sm text-neutral-600">Sign in with the password you just created.</p>
            <button type="button" disabled={busy} onClick={goToLogin} className={buttonClass}>
              {busy ? 'Opening sign in…' : 'Go to sign in'}
            </button>
          </>
        ) : !token ? (
          <>
            <h1 className="text-2xl font-bold">Invitation link required</h1>
            <p className="text-sm leading-6 text-neutral-600">
              Open the complete invitation link shared by your administrator. If you refreshed this
              page, open that original link again.
            </p>
          </>
        ) : (
          <>
            <h1 className="text-2xl font-bold">Set your member password</h1>
            <p className="text-sm text-neutral-600">
              Choose a password containing 6–16 characters.
            </p>
            <form onSubmit={accept} className="grid gap-4">
              <label className="grid gap-2 text-sm font-semibold">
                Password
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={busy}
                  className={inputClass}
                />
              </label>
              <label className="grid gap-2 text-sm font-semibold">
                Confirm password
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  disabled={busy}
                  className={inputClass}
                />
              </label>
              <button type="submit" disabled={busy} className={buttonClass}>
                {busy ? 'Setting up…' : 'Create my account'}
              </button>
            </form>
          </>
        )}

        {error && (
          <p role="alert" className="rounded-lg bg-neutral-100 p-3 text-sm">
            {error}
          </p>
        )}
        <a href="/" className="inline-block text-sm underline">
          Back to workspace
        </a>
      </section>
    </AuthFrame>
  )
}
