import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { StoreDeletionRequest } from '../storeDeletion.js'
import ConfirmDialog from './ConfirmDialog.jsx'
import useConfirmation from './useConfirmation.js'
import { Spinner } from './StoreProgress.jsx'

export default function StoreActions({ store, user, locked, onChanged, onBusyChange }) {
  const confirmation = useConfirmation()
  const [client] = useState(() => {
    try {
      return { value: new StoreDeletionRequest(user.id, store.id, window.sessionStorage, api) }
    } catch (error) {
      return { error: error.message }
    }
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(client.error || '')
  const [pending, setPending] = useState(Boolean(client.value?.pending))
  const [notice, setNotice] = useState('')
  const running = useRef(false)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const admin = user.role === 'admin'
  const canDelete = admin || store.owner_id === user.id
  const ready = store.status === 'Ready'
  const deletable = ['Ready', 'Failed'].includes(store.status)
  const disabled = locked || busy
  async function change(action) {
    if (running.current || disabled) return
    if (onBusyChange(true) === false) return
    running.current = true
    setBusy(true)
    const removing = action === 'delete'
    const enabled = store.access_enabled === false
    try {
      const accepted = await confirmation.confirm({
        title: removing
          ? 'Delete ' + store.name + '?'
          : (enabled ? 'Enable ' : 'Disable ') + store.name + '?',
        description: removing
          ? 'Permanently remove this store, products, orders, images and tool connections. This cannot be undone.'
          : enabled
            ? 'Reopen the public shop and restore tool access using the existing member grants.'
            : 'Show a maintenance page and block this store’s tools. Products, orders and member grants are kept.',
        label: removing ? 'Delete store' : enabled ? 'Enable store' : 'Disable store',
      })
      if (!accepted || !mounted.current) return
      setError('')
      setNotice('')
      if (removing) {
        await client.value.submit(store.generation)
        if (mounted.current) setNotice('Deletion accepted. Cleanup progress updates automatically.')
      } else {
        const controller = new AbortController()
        const timer = setTimeout(() => controller.abort(), 20_000)
        try {
          await api('/stores/' + store.id + '/access', {
            method: 'PUT',
            signal: controller.signal,
            body: { generation: store.generation, revision: store.access_revision, enabled },
          })
        } finally {
          clearTimeout(timer)
        }
        if (mounted.current)
          setNotice(
            enabled ? 'Store enabled.' : 'Store disabled. Saved data and member grants are kept.'
          )
      }
    } catch (failure) {
      if (mounted.current)
        setError(
          failure.name === 'AbortError'
            ? 'The result is uncertain. Refresh Stores before retrying.'
            : failure.message
        )
    } finally {
      running.current = false
      onBusyChange(false)
      if (mounted.current) {
        setPending(Boolean(client.value?.pending))
        setBusy(false)
        onChanged()
      }
    }
  }
  if (!canDelete && !admin) return null
  return (
    <div className="mt-5 space-y-3">
      <div className="flex flex-wrap gap-2">
        {canDelete && (deletable || pending) && (
          <button
            type="button"
            className="ui-button text-red-700"
            disabled={disabled || !client.value}
            onClick={() => change('delete')}
          >
            {busy && <Spinner />}
            {pending ? 'Retry saved deletion' : 'Delete store'}
          </button>
        )}
        {admin && ready && (
          <button
            type="button"
            className="ui-button"
            disabled={disabled || !store.access_guarded}
            onClick={() => change('access')}
          >
            {store.access_enabled === false ? 'Enable store' : 'Disable store'}
          </button>
        )}
      </div>
      {admin && ready && !store.access_guarded && (
        <p className="text-xs text-neutral-500">
          Maintenance controls become available after the store gate is installed.
        </p>
      )}
      {store.status === 'Provisioning' && (
        <p className="text-xs text-neutral-500">
          Deletion is available when the current creation attempt finishes.
        </p>
      )}
      {error && (
        <p role="alert" className="ui-banner">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="text-sm text-neutral-600">
          {notice}
        </p>
      )}
      <ConfirmDialog request={confirmation.request} onFinish={confirmation.finish} />
    </div>
  )
}
