import { lazy, Suspense, useEffect, useState } from 'react'
import { api } from './api.js'
const Workspace = lazy(() => import('./components/Workspace.jsx'))
import AcceptInvite from './components/AcceptInvite.jsx'
import AuthFrame from './components/AuthFrame.jsx'
import Icon from './components/Icon.jsx'

function ErrorNotice({ message }) {
  if (!message) return null

  return (
    <p
      role="alert"
      className="rounded-xl border border-neutral-400 bg-neutral-100 p-3 text-sm text-neutral-800"
    >
      {message}
    </p>
  )
}

export default function App() {
  const [user, setUser] = useState(null)
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [workspaceBusy, setWorkspaceBusy] = useState(false)
  const [isInvitePage] = useState(
    () => window.location.pathname.replace(/\/+$/, '') === '/accept-invite'
  )

  useEffect(() => {
    if (isInvitePage) return
    let active = true
    const controller = new AbortController()

    async function restoreSession() {
      try {
        const account = await api('/auth/me', {
          signal: controller.signal,
        })
        if (active) setUser(account)
      } catch (err) {
        if (active && err.name !== 'AbortError' && err.status !== 401) {
          setError(err.message)
        }
      } finally {
        if (active) setLoading(false)
      }
    }

    restoreSession()

    return () => {
      active = false
      controller.abort()
    }
  }, [isInvitePage])

  async function login(event) {
    event.preventDefault()
    if (busy) return

    const form = event.currentTarget
    const fields = new FormData(form)

    setBusy(true)
    setError('')

    try {
      const account = await api('/auth/login', {
        method: 'POST',
        body: {
          email: fields.get('email').trim(),
          password: fields.get('password'),
        },
      })
      form.reset()
      setUser(account)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function logout() {
    if (busy || workspaceBusy) return
    setBusy(true)
    setError('')

    try {
      await api('/auth/logout', { method: 'POST' })
      setUser(null)
    } catch (err) {
      if (err.status === 401) setUser(null)
      else setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (isInvitePage) return <AcceptInvite />
  if (loading)
    return (
      <div role="status" className="grid min-h-dvh place-items-center bg-neutral-100">
        <div className="flex items-center gap-3 text-sm text-neutral-500">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-neutral-200 border-t-neutral-700" />
          Opening your workspace…
        </div>
      </div>
    )
  if (user)
    return (
      <Suspense
        fallback={
          <p role="status" className="grid min-h-dvh place-items-center text-sm text-neutral-500">
            Opening your workspace...
          </p>
        }
      >
        <Workspace
          key={`${user.id}:${user.role}`}
          onReauthenticate={setUser}
          user={user}
          onBusyChange={setWorkspaceBusy}
          onLogout={logout}
          signingOut={busy}
          sessionError={error}
        />
      </Suspense>
    )

  return (
    <AuthFrame>
      <h1 className="text-2xl font-semibold">Sign in</h1>
      <p className="mt-2 text-sm text-neutral-500">Use your Urumi account.</p>
      <form onSubmit={login} className="mt-7 space-y-5">
        <div className="space-y-2">
          <label htmlFor="email" className="text-xs font-semibold text-neutral-700">
            Email address
          </label>
          <input
            id="email"
            name="email"
            defaultValue={(new URLSearchParams(window.location.search).get('email') || '').slice(
              0,
              254
            )}
            type="email"
            autoComplete="username"
            maxLength={254}
            placeholder="you@example.com"
            required
            disabled={busy}
            className="ui-input"
          />
        </div>
        <div className="space-y-2">
          <label htmlFor="password" className="text-xs font-semibold text-neutral-700">
            Password
          </label>
          <div className="relative">
            <input
              id="password"
              name="password"
              type={showPassword ? 'text' : 'password'}
              autoComplete="current-password"
              required
              disabled={busy}
              className="ui-input pr-12"
            />
            <button
              type="button"
              aria-label={showPassword ? 'Hide password' : 'Show password'}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((value) => !value)}
              className="absolute inset-y-0 right-0 grid w-11 place-items-center rounded-r-lg text-neutral-500 hover:text-neutral-800"
            >
              <Icon name={showPassword ? 'hide' : 'eye'} className="h-4 w-4" />
            </button>
          </div>
        </div>
        <ErrorNotice message={error} />
        <button type="submit" disabled={busy} className="ui-primary mt-1 w-full py-3">
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="mt-6 border-t border-neutral-100 pt-5 text-xs leading-5 text-neutral-500">
        Need an account? Ask your administrator for an invitation.
      </p>
    </AuthFrame>
  )
}
