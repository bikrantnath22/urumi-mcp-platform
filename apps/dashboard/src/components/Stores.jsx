import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Icon from './Icon.jsx'
import CreateStore from './CreateStore.jsx'
import { monitorStores } from '../storeMonitor.js'
import StoreProgress from './StoreProgress.jsx'
import StoreActions from './StoreActions.jsx'
import WordPressSetup from './WordPressSetup.jsx'

function displayDate(value) {
  if (!value) return '-'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '-'
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  }).format(date)
}

function storeLink(value) {
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null
    return url.href
  } catch {
    return null
  }
}

function StoreDetails({ id, updatedAt }) {
  const [attempt, setAttempt] = useState(0)
  const [details, setDetails] = useState({
    loading: true,
    data: null,
    error: '',
  })

  useEffect(() => {
    const controller = new AbortController()
    api(`/stores/${id}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          setDetails({ loading: false, data, error: '' })
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setDetails({ loading: false, data: null, error: error.message })
        }
      })
    return () => controller.abort()
  }, [id, attempt, updatedAt])

  if (details.loading) {
    return (
      <p role="status" className="text-sm text-neutral-500">
        Loading store details…
      </p>
    )
  }

  if (details.error) {
    return (
      <div className="space-y-3">
        <p role="alert" className="ui-banner">
          {details.error}
        </p>
        <button
          type="button"
          className="ui-button"
          onClick={() => {
            setDetails({ loading: true, data: null, error: '' })
            setAttempt((value) => value + 1)
          }}
        >
          Try again
        </button>
      </div>
    )
  }

  const { store, operation } = details.data
  const action = operation?.action === 'provision' ? 'Creation' : 'Deletion'

  return (
    <div className="space-y-4 text-sm">
      <div>
        <h3 className="font-semibold text-neutral-800">Latest store information</h3>
        <p className="mt-1 text-xs text-neutral-500">
          Status:{' '}
          {store.status === 'Ready' && store.access_enabled === false ? 'Disabled' : store.status} ·
          Updated {displayDate(store.updated_at)} IST
        </p>
      </div>

      {store.status_reason && <p className="ui-banner break-words">{store.status_reason}</p>}

      {operation ? (
        <dl className="grid gap-4 sm:grid-cols-2">
          <div>
            <dt className="text-xs text-neutral-500">Latest operation</dt>
            <dd className="mt-1 font-medium">
              {action} · {operation.status}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-neutral-500">Requested</dt>
            <dd className="mt-1">{displayDate(operation.created_at)} IST</dd>
          </div>
          <div>
            <dt className="text-xs text-neutral-500">Started</dt>
            <dd className="mt-1">
              {displayDate(operation.started_at)}
              {operation.started_at && ' IST'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-neutral-500">
              {operation.finished_at ? 'Finished' : 'Deadline'}
            </dt>
            <dd className="mt-1">
              {displayDate(operation.finished_at || operation.deadline_at)} IST
            </dd>
          </div>
        </dl>
      ) : (
        <p className="text-neutral-500">No operations have been recorded for this store.</p>
      )}
    </div>
  )
}

export default function Stores({ user, onBusyChange }) {
  const [page, setPage] = useState({ items: [], loading: true, error: '' })
  const [selectedId, setSelectedId] = useState(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createBusy, setCreateBusy] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const actionLock = useRef(false)
  const monitor = useRef(null)
  const handleCreateBusy = useCallback((value) => setCreateBusy(value), [])
  const handleActionBusy = useCallback((value) => {
    if (value && actionLock.current) return false
    actionLock.current = value
    setActionBusy(value)
    return true
  }, [])
  useEffect(() => {
    onBusyChange?.(createBusy || actionBusy)
  }, [createBusy, actionBusy, onBusyChange])
  useEffect(() => () => onBusyChange?.(false), [onBusyChange])
  useEffect(() => {
    monitor.current = monitorStores(
      (signal) => api('/stores?limit=100', { signal }),
      (items) => setPage({ items, loading: false, error: '' }),
      (error) => setPage((previous) => ({ ...previous, loading: false, error }))
    )
    return () => monitor.current?.stop()
  }, [user.id])
  const refresh = useCallback(() => monitor.current?.refresh(), [])
  const waiting = page.loading
  const mutationBusy = createBusy || actionBusy

  return (
    <section className="ui-panel space-y-6" aria-labelledby="stores-title">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 id="stores-title" className="text-[22px] font-semibold tracking-tight">
            Stores
          </h1>
          <p className="mt-2 text-sm leading-6 text-neutral-500">
            {user.role === 'admin'
              ? 'View stores across the platform.'
              : 'View stores belonging to your account.'}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={refresh}
            disabled={waiting || mutationBusy}
            className="ui-button"
          >
            <Icon name="refresh" />
            Refresh
          </button>
          <button
            type="button"
            disabled={waiting || mutationBusy}
            className="ui-primary"
            onClick={() => setCreateOpen((value) => !value)}
          >
            <Icon name={createOpen ? 'close' : 'plus'} />
            {createOpen ? 'Close form' : 'Create store'}
          </button>
        </div>
      </header>

      <p className="text-xs text-neutral-500">
        Store creation and deletion update automatically. All times are shown in IST.
      </p>

      {createOpen && (
        <CreateStore
          user={user}
          disabled={waiting || actionBusy}
          onCreated={refresh}
          onBusyChange={handleCreateBusy}
        />
      )}

      {page.error && (
        <p role="alert" className="ui-banner">
          {page.error}
        </p>
      )}
      {page.loading && (
        <p role="status" className="text-sm text-neutral-500">
          Loading stores…
        </p>
      )}

      {!page.loading && !page.error && page.items.length === 0 && (
        <div className="rounded-xl border border-dashed border-neutral-300 bg-neutral-50 px-6 py-12 text-center">
          <Icon name="box" className="mx-auto h-8 w-8 text-neutral-400" />
          <h2 className="mt-4 font-semibold">No stores yet</h2>
          <p className="mt-2 text-sm text-neutral-500">
            Stores created through this workspace will appear here.
          </p>
        </div>
      )}

      <div className="space-y-4" aria-busy={waiting}>
        {!page.loading &&
          page.items.map((store) => {
            const href = storeLink(store.url)
            const expanded = selectedId === store.id

            return (
              <article
                key={store.id}
                className="rounded-xl border border-neutral-200 bg-white p-5 sm:p-6"
              >
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <h2 className="break-words text-base font-semibold">{store.name}</h2>
                    <p className="mt-1 break-all text-xs text-neutral-500">{store.slug}</p>
                  </div>
                  <span
                    className={`rounded border px-3 py-1 text-xs font-medium ${
                      store.status === 'Ready'
                        ? 'border-neutral-900 bg-neutral-900 text-white'
                        : 'border-neutral-200 bg-neutral-100 text-neutral-700'
                    }`}
                  >
                    {store.status === 'Ready' && store.access_enabled === false
                      ? 'Disabled'
                      : store.status}
                  </span>
                </div>

                <div className="mt-5 space-y-2 text-sm">
                  <p className="break-all">
                    {store.status === 'Ready' && store.access_enabled !== false && href ? (
                      <a
                        className="font-medium underline underline-offset-4"
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {store.url}
                      </a>
                    ) : (
                      <span className="text-neutral-500">{store.url}</span>
                    )}
                  </p>
                  <p className="text-xs text-neutral-500">
                    Created {displayDate(store.created_at)} IST
                    {user.role === 'admin' && ` · Owner ID: ${store.owner_id}`}
                  </p>
                </div>

                <StoreProgress status={store.status} />
                {store.status === 'Ready' && store.access_enabled === false && (
                  <p role="status" className="ui-banner mt-4">
                    This store is disabled by an administrator. Products, orders and member grants
                    are kept.
                  </p>
                )}
                <StoreActions
                  key={store.id}
                  store={store}
                  user={user}
                  locked={mutationBusy}
                  onChanged={refresh}
                  onBusyChange={handleActionBusy}
                />

                <WordPressSetup
                  key={store.id + ':wp:' + store.access_revision + ':' + store.generation}
                  store={store}
                  user={user}
                  locked={mutationBusy}
                  onBusyChange={handleActionBusy}
                />

                <button
                  type="button"
                  className="ui-button mt-5"
                  aria-expanded={expanded}
                  aria-controls={`store-details-${store.id}`}
                  onClick={() => setSelectedId(expanded ? null : store.id)}
                >
                  <Icon name="eye" />
                  {expanded ? 'Hide details' : 'View details'}
                </button>

                {expanded && (
                  <div
                    id={`store-details-${store.id}`}
                    className="mt-5 border-t border-neutral-200 pt-5"
                  >
                    <StoreDetails key={store.id} id={store.id} updatedAt={store.updated_at} />
                  </div>
                )}
              </article>
            )
          })}
      </div>

      {!page.loading && page.items.length > 0 && (
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-neutral-100 pt-4">
          <p className="text-xs text-neutral-500">
            Showing {page.items.length} {page.items.length === 1 ? 'store' : 'stores'}
          </p>
        </footer>
      )}
    </section>
  )
}
