import { useState } from 'react'

const inputClass = 'ui-input'

const fields = [
  { name: 'name', label: 'Server name', maxLength: 100 },
  {
    name: 'slug',
    label: 'Slug',
    maxLength: 32,
    pattern: '[a-z][a-z0-9]*(?:-[a-z0-9]+)*',
    placeholder: 'For example, team-notes',
  },
  {
    name: 'url',
    label: 'MCP URL',
    type: 'url',
    maxLength: 2048,
    placeholder: 'http://127.0.0.1:8087/mcp',
  },
  {
    name: 'timeout_s',
    label: 'Timeout (seconds)',
    type: 'number',
    min: 1,
    max: 60,
    step: 'any',
  },
]

export default function ServerForm({ server, busy, onSave, onCancel }) {
  const [values, setValues] = useState({
    name: server?.name || '',
    slug: server?.slug || '',
    url: server?.url || '',
    timeout_s: String(server?.timeout_s ?? 15),
    enabled: server?.enabled ?? true,
    header: server?.auth_header_name || 'Authorization',
    credential: '',
  })

  const [authMode, setAuthMode] = useState(server?.has_credential ? 'keep' : 'none')

  function change(event) {
    const { name, value, type, checked } = event.target
    setValues((previous) => ({
      ...previous,
      [name]: type === 'checkbox' ? checked : value,
    }))
  }

  function submit(event) {
    event.preventDefault()
    if (busy) return

    const body = {
      name: values.name.trim(),
      slug: values.slug.trim(),
      url: values.url.trim(),
      timeout_s: Number(values.timeout_s),
      enabled: values.enabled,
    }

    if (authMode === 'replace') {
      body.auth_header_name = values.header.trim()
      body.credential = values.credential
    } else if (authMode === 'none') {
      body.auth_header_name = null
      body.credential = null
    }

    onSave(body)
  }

  return (
    <form
      onSubmit={submit}
      className="space-y-5 rounded-xl border border-neutral-300 bg-neutral-50 p-5"
    >
      <div>
        <h2 className="font-semibold">{server ? 'Edit MCP server' : 'Add MCP server'}</h2>
        <p className="mt-2 text-xs leading-6 text-neutral-500">
          The slug prefixes tool names, for example notes__get_note.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {fields.map(({ name, label, ...attributes }) => (
          <label key={name} className="grid gap-2 text-xs font-semibold">
            {label}
            <input
              {...attributes}
              name={name}
              value={values[name]}
              onChange={change}
              required
              disabled={busy}
              className={inputClass}
            />
          </label>
        ))}
      </div>

      <label className="flex items-center gap-3 text-sm">
        <input
          name="enabled"
          type="checkbox"
          checked={values.enabled}
          onChange={change}
          disabled={busy}
          className="h-4 w-4 accent-neutral-900"
        />
        Enabled
      </label>

      <label className="grid gap-2 text-xs font-semibold">
        Static upstream credential
        <select
          value={authMode}
          disabled={busy}
          onChange={(event) => {
            setAuthMode(event.target.value)
            setValues((previous) => ({ ...previous, credential: '' }))
          }}
          className={inputClass}
        >
          {server?.has_credential && <option value="keep">Keep saved credential</option>}
          <option value="none">
            {server?.has_credential ? 'Remove saved credential' : 'No credential'}
          </option>
          <option value="replace">Set a new credential</option>
        </select>
      </label>

      {authMode === 'replace' && (
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="grid gap-2 text-xs font-semibold">
            Header name
            <input
              name="header"
              value={values.header}
              onChange={change}
              maxLength={100}
              required
              disabled={busy}
              className={inputClass}
            />
          </label>

          <label className="grid gap-2 text-xs font-semibold">
            Complete header value
            <input
              name="credential"
              type="password"
              value={values.credential}
              onChange={change}
              placeholder="For example, Bearer ..."
              autoComplete="off"
              maxLength={4096}
              required
              disabled={busy}
              className={inputClass}
            />
          </label>
        </div>
      )}

      <div className="flex gap-3">
        <button type="submit" disabled={busy} className="ui-primary">
          {busy ? 'Working…' : 'Save server'}
        </button>

        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="rounded-lg border border-neutral-300 px-4 py-2 text-sm font-semibold hover:bg-neutral-100 disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </form>
  )
}
