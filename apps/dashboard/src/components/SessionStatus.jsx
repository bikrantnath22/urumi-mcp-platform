import { useEffect, useId, useRef, useState } from 'react'
import { api } from '../api.js'

export default function SessionStatus({ user, session, onSignedIn, onLogout }) {
  const dialog = useRef(null)
  const title = useId()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const minutes = Math.max(1, Math.ceil(session.remaining / 60))
  useEffect(() => {
    if (session.expired && !dialog.current.open) dialog.current.showModal()
    if (!session.expired && dialog.current.open) {
      dialog.current.querySelector('form')?.reset()
      dialog.current.close()
    }
  }, [session.expired])
  async function login(event) {
    event.preventDefault()
    if (busy) return
    const form = event.currentTarget
    const password = new FormData(form).get('password')
    setBusy(true)
    setError('')
    try {
      const account = await api('/auth/login', {
        method: 'POST',
        body: { email: user.email, password },
      })
      if (account.id !== user.id)
        throw new Error('Sign in using the same account to resume this workspace.')
      form.reset()
      if (!(await session.signedIn()))
        throw new Error(
          'Signed in, but the session could not be checked. Check your connection and try again.'
        )
      onSignedIn(account)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      {session.warning && (
        <div
          role="status"
          className="ui-banner mb-5 flex flex-wrap items-center justify-between gap-3"
        >
          <p>
            {session.absoluteWarning
              ? 'Your 8-hour session ends soon. Sign in again to keep working.'
              : `Your session expires in about ${minutes} minute${minutes === 1 ? '' : 's'} due to inactivity.`}
          </p>
          <button
            type="button"
            className="ui-button"
            onClick={session.absoluteWarning ? session.requestSignIn : session.continueSession}
          >
            {session.absoluteWarning ? 'Sign in again' : 'Continue working'}
          </button>
        </div>
      )}
      {session.error && !session.expired && (
        <div
          role="alert"
          className="ui-banner mb-5 flex flex-wrap items-center justify-between gap-3"
        >
          <p>{session.error}</p>
          <button type="button" className="ui-button" onClick={session.checkSession}>
            Retry session check
          </button>
        </div>
      )}
      <dialog
        ref={dialog}
        aria-labelledby={title}
        onCancel={(event) => event.preventDefault()}
        className="m-auto w-[calc(100%_-_2rem)] max-w-md rounded-2xl border border-neutral-200 bg-white p-6 text-neutral-900 shadow-2xl backdrop:bg-neutral-950/50 sm:p-7"
      >
        <h2 id={title} className="text-xl font-semibold tracking-tight">
          Sign in to continue
        </h2>
        <p className="mt-3 text-sm leading-6 text-neutral-600">
          Please sign in again to continue. Your unsent chat message stays open while you sign in.
        </p>
        <p className="mt-4 break-all rounded-lg bg-neutral-100 p-3 text-sm">{user.email}</p>
        {session.error && (
          <p role="alert" className="ui-banner mt-4">
            {session.error}
          </p>
        )}
        <form onSubmit={login} className="mt-5 space-y-4">
          <label className="block text-sm font-medium">
            Password
            <input
              autoFocus
              name="password"
              type="password"
              autoComplete="current-password"
              required
              disabled={busy}
              className="ui-input mt-2"
            />
          </label>
          {error && (
            <p role="alert" className="ui-banner">
              {error}
            </p>
          )}
          <button type="submit" disabled={busy} className="ui-primary w-full">
            {busy ? 'Signing in…' : 'Sign in and resume'}
          </button>
        </form>
        <button type="button" disabled={busy} className="ui-button mt-3 w-full" onClick={onLogout}>
          Leave this workspace
        </button>
      </dialog>
    </>
  )
}
