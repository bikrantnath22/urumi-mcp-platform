import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { canSetupWordPress, prepareWordPressSetup } from '../wordpressSetup.js'
import { Spinner } from './StoreProgress.jsx'

export default function WordPressSetup({ store, user, locked, onBusyChange }) {
  const [busy, setBusy] = useState(false)
  const [setup, setSetup] = useState(null)
  const [error, setError] = useState('')
  const running = useRef(false)
  const controller = useRef(null)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      controller.current?.abort()
    }
  }, [])
  useEffect(() => {
    if (!setup) return
    const timer = setTimeout(
      () => setSetup(null),
      Math.max(0, Date.parse(setup.expires_at) - Date.now())
    )
    return () => clearTimeout(timer)
  }, [setup])
  if (!canSetupWordPress(store, user)) return null

  async function start() {
    if (running.current || locked || onBusyChange(true) === false) return
    running.current = true
    setBusy(true)
    setError('')
    setSetup(null)
    const abort = new AbortController()
    controller.current = abort
    const timer = setTimeout(() => abort.abort(), 20_000)
    try {
      const result = await prepareWordPressSetup(store, user, api, abort.signal)
      if (mounted.current && !abort.signal.aborted) setSetup(result)
    } catch (failure) {
      if (mounted.current)
        setError(
          abort.signal.aborted
            ? 'Setup could not be confirmed. Start setup again.'
            : failure.message
        )
    } finally {
      clearTimeout(timer)
      running.current = false
      if (mounted.current) setBusy(false)
      onBusyChange(false)
    }
  }
  return (
    <section
      className="mt-5 rounded-lg border border-neutral-200 bg-neutral-50 p-4"
      aria-label="WordPress administrator login"
    >
      <h3 className="text-sm font-semibold">WordPress login</h3>
      <p className="mt-2 text-xs leading-5 text-neutral-500">
        Choose your password on this store’s WordPress setup page. Your dashboard password stays
        separate. New stores use username admin; existing stores show their current username during
        setup.
      </p>
      {error && (
        <p role="alert" className="ui-banner mt-3">
          {error}
        </p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className="ui-button" disabled={locked || busy} onClick={start}>
          {busy && <Spinner />}
          {busy ? 'Preparing setup…' : 'Set WordPress password'}
        </button>
        <a
          className="ui-button"
          href={store.url + '/wp-admin/'}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open WordPress admin
        </a>
      </div>
      {setup && (
        <form
          className="mt-3"
          method="post"
          action={setup.url}
          target="_blank"
          rel="noopener noreferrer"
          onSubmit={() => {
            // Let the browser copy the POST body before removing the in-memory token.
            setTimeout(() => setSetup(null), 0)
          }}
        >
          <input type="hidden" name="urumi_setup_token" value={setup.token} />
          <button type="submit" className="ui-primary">
            Continue to WordPress
          </button>
          <p className="mt-2 text-xs text-neutral-500">
            Opens this store in a new tab. Setup expires in five minutes; starting again replaces
            the previous setup.
          </p>
        </form>
      )}
    </section>
  )
}
