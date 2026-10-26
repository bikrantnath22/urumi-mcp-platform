import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function ImageDestination({ id, value, disabled, onSelect }) {
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState({ loading: true, items: [], error: '' })

  useEffect(() => {
    const controller = new AbortController()
    api('/tools', { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return
        const items = (data.tools || []).filter((tool) =>
          /^[a-z0-9-]+__upload_media$/.test(tool.name)
        )
        setState({
          loading: false,
          items,
          error: items.length ? '' : 'Ask your admin to grant Upload media for this store.',
        })
        onSelect(id, items.length === 1 ? items[0].name : '')
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setState({ loading: false, items: [], error: error.message })
        }
      })
    return () => controller.abort()
  }, [id, onSelect, revision])

  return (
    <div className="mb-3">
      <label className="grid gap-1.5 text-xs font-medium text-neutral-600">
        Store for this image
        <select
          className="ui-input"
          value={value || ''}
          disabled={disabled || state.loading || !state.items.length}
          onChange={(event) => onSelect(id, event.target.value)}
        >
          <option value="">{state.loading ? 'Loading stores…' : 'Choose a store'}</option>
          {state.items.map((tool) => {
            const heading = /^\[([^\]]+)\]/.exec(tool.description || '')
            return (
              <option key={tool.name} value={tool.name}>
                {heading?.[1] || tool.name.split('__')[0]}
                {state.items.length > 1 && ` (${tool.name.split('__')[0]})`}
              </option>
            )
          })}
        </select>
      </label>
      <button
        type="button"
        disabled={disabled || state.loading}
        className="mt-2 rounded px-1 py-1 text-xs text-neutral-500 underline disabled:opacity-50"
        onClick={() => {
          setState({ loading: true, items: [], error: '' })
          onSelect(id, '')
          setRevision((current) => current + 1)
        }}
      >
        Refresh stores
      </button>
      {state.error && (
        <p role="alert" className="ui-banner mt-2">
          {state.error}
        </p>
      )}
    </div>
  )
}
