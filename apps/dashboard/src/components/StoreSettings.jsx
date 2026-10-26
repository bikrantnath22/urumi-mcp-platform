import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Icon from './Icon.jsx'

function formValues(record) {
  return {
    enabled: record.enabled,
    max_per_user: String(record.max_per_user),
    max_total: String(record.max_total),
    max_concurrent_creations: String(record.max_concurrent_creations ?? ''),
    timeout_minutes: String(record.timeout_seconds / 60),
  }
}

export default function StoreSettings({ disabled = false, onBusyChange }) {
  const [record, setRecord] = useState(null)
  const [values, setValues] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [conflict, setConflict] = useState(false)
  const request = useRef(null)

  const load = useCallback((controller) => {
    return api('/admin/store-settings', {
      signal: controller.signal,
    })
      .then((data) => {
        if (controller.signal.aborted) return
        setRecord(data)
        setValues(formValues(data))
        setError('')
        setNotice('')
        setConflict(false)
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(err.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    request.current = controller
    load(controller)
    return () => request.current?.abort()
  }, [load])

  function refresh() {
    if (disabled || saving || loading) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError('')
    setNotice('')
    load(controller)
  }

  function change(name, value) {
    setValues((previous) => ({ ...previous, [name]: value }))
    setNotice('')
  }

  async function save(event) {
    event.preventDefault()
    if (disabled || loading || saving || conflict || !record || !values) return

    const perUser = Number(values.max_per_user)
    const total = Number(values.max_total)
    const parallel = Number(values.max_concurrent_creations)
    const minutes = Number(values.timeout_minutes)

    if (
      ![perUser, total, parallel, minutes].every(Number.isInteger) ||
      perUser < 1 ||
      perUser > total ||
      total > 100 ||
      parallel < 1 ||
      parallel > 100 ||
      minutes < 1 ||
      minutes > 60
    ) {
      setError(
        'Use whole numbers: 1–100 for store limits and simultaneous creations, and 1–60 minutes. The per-user store limit cannot exceed the platform store limit.'
      )
      return
    }

    setSaving(true)
    onBusyChange(true)
    setError('')
    setNotice('')

    try {
      const data = await api('/admin/store-settings', {
        method: 'PUT',
        body: {
          revision: record.revision,
          enabled: values.enabled,
          max_per_user: perUser,
          max_total: total,
          max_concurrent_creations: parallel,
          timeout_seconds: minutes * 60,
        },
      })
      setRecord(data)
      setValues(formValues(data))
      setConflict(false)
      setNotice('Store settings saved. No restart is needed.')
    } catch (err) {
      setError(err.message)
      if (err.status === 409) setConflict(true)
    } finally {
      setSaving(false)
      onBusyChange(false)
    }
  }

  const blocked = disabled || loading || saving

  return (
    <section
      aria-labelledby="store-settings-title"
      className="space-y-5 border-t border-neutral-200 pt-8"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 id="store-settings-title" className="text-lg font-semibold tracking-tight">
            Store provisioning
          </h2>
          <p className="mt-2 text-sm leading-6 text-neutral-500">
            Manage store limits, simultaneous creations and provisioning time.
          </p>
          {record && (
            <span className="mt-3 inline-flex rounded border border-neutral-200 bg-neutral-100 px-3 py-1 text-xs font-medium text-neutral-700">
              {record.enabled ? 'Creation enabled' : 'Creation paused'}
            </span>
          )}
        </div>
        <button type="button" onClick={refresh} disabled={blocked} className="ui-button">
          <Icon name="refresh" />
          Refresh store settings
        </button>
      </div>

      {error && (
        <p role="alert" className="ui-banner">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="ui-banner">
          {notice}
        </p>
      )}
      {loading && (
        <p role="status" className="text-sm text-neutral-500">
          Loading store settings…
        </p>
      )}

      {values && (
        <form onSubmit={save} className="space-y-5">
          <fieldset
            disabled={blocked}
            className="rounded-xl border border-neutral-200 bg-white p-5 sm:p-6"
          >
            <legend className="px-2 text-sm font-semibold">Creation policy</legend>

            <label className="flex cursor-pointer items-start gap-3">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4 shrink-0 accent-neutral-900"
                checked={values.enabled}
                onChange={(event) => change('enabled', event.target.checked)}
              />
              <span>
                <span className="text-sm font-semibold text-neutral-800">Allow new stores</span>
                <span className="mt-1 block text-xs leading-6 text-neutral-500">
                  Pausing creation keeps existing stores available.
                </span>
              </span>
            </label>

            <div className="mt-6 grid gap-5 sm:grid-cols-2">
              <label className="grid gap-2 text-xs font-semibold text-neutral-700">
                Maximum stores per user
                <input
                  className="ui-input"
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  required
                  value={values.max_per_user}
                  onChange={(event) => change('max_per_user', event.target.value)}
                />
              </label>
              <label className="grid gap-2 text-xs font-semibold text-neutral-700">
                Maximum stores across the platform
                <input
                  className="ui-input"
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  required
                  value={values.max_total}
                  onChange={(event) => change('max_total', event.target.value)}
                />
              </label>
              <div className="space-y-2">
                <label className="grid gap-2 text-xs font-semibold text-neutral-700">
                  Maximum simultaneous store creations
                  <input
                    className="ui-input"
                    type="number"
                    min="1"
                    max="100"
                    step="1"
                    required
                    aria-describedby="store-parallel-help"
                    value={values.max_concurrent_creations}
                    onChange={(event) => change('max_concurrent_creations', event.target.value)}
                  />
                </label>
                <p id="store-parallel-help" className="text-xs leading-6 text-neutral-500">
                  Stores being created at the same time. Extra requests wait until capacity is
                  available.
                </p>
              </div>
              <label className="grid gap-2 text-xs font-semibold text-neutral-700">
                Provisioning timeout (minutes)
                <input
                  className="ui-input"
                  type="number"
                  min="1"
                  max="60"
                  step="1"
                  required
                  value={values.timeout_minutes}
                  onChange={(event) => change('timeout_minutes', event.target.value)}
                />
              </label>
            </div>
            <p className="mt-5 text-xs leading-6 text-neutral-500">
              Store limits apply to new requests. Changing parallel capacity does not interrupt
              running work or reset request deadlines.
            </p>
          </fieldset>

          <div className="flex flex-wrap items-center justify-between gap-4">
            <p className="text-xs leading-6 text-neutral-500">
              Changes are recorded in security history.
            </p>
            <button type="submit" disabled={blocked || conflict} className="ui-primary">
              {saving ? 'Saving…' : 'Save store settings'}
            </button>
          </div>
        </form>
      )}
    </section>
  )
}
