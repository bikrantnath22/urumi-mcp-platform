import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from './StoreProgress.jsx'
import { StoreCreationRequest } from '../storeCreation.js'

export default function CreateStore({ user, disabled, onCreated, onBusyChange }) {
  const [client] = useState(() => {
    try {
      return { value: new StoreCreationRequest(user.id, window.sessionStorage, api) }
    } catch (error) {
      return { error: error.message }
    }
  })
  const [pending, setPending] = useState(client.value?.pending || null)
  const [fields, setFields] = useState(() => ({
    name: client.value?.pending?.name || '',
    slug: client.value?.pending?.slug || '',
  }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(client.error || '')
  const [notice, setNotice] = useState('')
  const running = useRef(false)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      onBusyChange(false)
    }
  }, [onBusyChange])

  async function submit(event) {
    event.preventDefault()
    if (running.current || disabled || !client.value) return
    running.current = true
    setBusy(true)
    setError('')
    setNotice('')
    onBusyChange(true)
    let accepted
    try {
      accepted = await client.value.submit(fields)
      if (mounted.current) {
        setNotice(
          'Request accepted. Your store is being created; progress updates automatically below.'
        )
        if (!client.value.pending) setFields({ name: '', slug: '' })
      }
    } catch (failure) {
      if (mounted.current)
        setError(
          failure.name === 'AbortError'
            ? 'The request timed out. Check Stores or retry the saved request.'
            : failure.message || 'The request could not be confirmed.'
        )
    } finally {
      running.current = false
      if (mounted.current) {
        setPending(client.value.pending)
        if (client.value.pending) {
          setFields({ name: client.value.pending.name, slug: client.value.pending.slug })
        }
        setBusy(false)
        onBusyChange(false)
      }
    }
    if (accepted && mounted.current) onCreated(accepted)
  }

  function change(name, value) {
    setFields((previous) => ({ ...previous, [name]: value }))
    setError('')
    setNotice('')
  }

  return (
    <section
      className="rounded-xl border border-neutral-200 bg-neutral-50 p-5 sm:p-6"
      aria-labelledby="create-store-title"
    >
      <h2 id="create-store-title" className="text-base font-semibold">
        Create a store
      </h2>
      <p className="mt-2 text-sm text-neutral-500">New stores belong to your signed-in account.</p>
      {pending && (
        <p role="status" className="ui-banner mt-4">
          A previous request may have reached the server. Retry checks that same request. Its name
          and address stay fixed until the result is confirmed.
        </p>
      )}
      {error && (
        <p role="alert" className="ui-banner mt-4">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="ui-banner mt-4">
          {notice}
        </p>
      )}
      <form onSubmit={submit} className="mt-5">
        <fieldset disabled={busy || disabled || !client.value} className="space-y-5">
          <div className="grid gap-5 sm:grid-cols-2">
            <label className="grid gap-2 text-xs font-semibold text-neutral-700">
              Store name
              <input
                className="ui-input"
                name="name"
                required
                maxLength={100}
                placeholder="Assam Tea"
                autoComplete="off"
                readOnly={Boolean(pending)}
                value={fields.name}
                onChange={(event) => change('name', event.target.value)}
              />
            </label>
            <label className="grid gap-2 text-xs font-semibold text-neutral-700">
              Website address
              <input
                className="ui-input"
                name="slug"
                required
                maxLength={32}
                pattern="[a-z0-9](?:[a-z0-9\-]{0,30}[a-z0-9])?"
                placeholder="assamtea or assam-tea"
                autoComplete="off"
                readOnly={Boolean(pending)}
                aria-describedby="store-address-help"
                value={fields.slug}
                onChange={(event) => change('slug', event.target.value.toLowerCase())}
              />
              <span id="store-address-help" className="font-normal leading-5 text-neutral-500">
                Use 1–32 letters, numbers or internal hyphens. No spaces.
              </span>
            </label>
          </div>
          <button type="submit" className="ui-primary">
            {busy && <Spinner />}
            {busy ? 'Requesting store…' : pending ? 'Retry request' : 'Create store'}
          </button>
        </fieldset>
      </form>
    </section>
  )
}
