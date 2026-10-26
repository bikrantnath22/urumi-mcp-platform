import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import ServerForm from './ServerForm.jsx'

const buttonClass = 'ui-button'

function config(server) {
  return {
    slug: server.slug,
    name: server.name,
    url: server.url,
    enabled: server.enabled,
    timeout_s: server.timeout_s,
  }
}

function displayDate(value) {
  if (!value) return 'Never'

  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  }).format(new Date(value))
}

export default function Servers({ active, onBusyChange }) {
  const [servers, setServers] = useState([])
  const [discoveries, setDiscoveries] = useState({})
  const [editor, setEditor] = useState(null)
  const [deleteId, setDeleteId] = useState(null)
  const [refreshCount, setRefreshCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const inFlight = useRef(false)
  const readVersion = useRef(0)
  const readController = useRef(null)

  useEffect(() => {
    if (!active || inFlight.current) return

    const version = ++readVersion.current
    const controller = new AbortController()
    readController.current = controller

    setLoading(true)
    setError('')

    async function load() {
      try {
        const data = await api('/servers', {
          signal: controller.signal,
        })

        if (version === readVersion.current) {
          setServers(data)
          setDiscoveries({})
        }
      } catch (err) {
        if (version === readVersion.current && err.name !== 'AbortError') {
          setError(err.message)
        }
      } finally {
        if (version === readVersion.current) setLoading(false)
      }
    }

    load()

    return () => {
      readVersion.current += 1
      controller.abort()
    }
  }, [active, refreshCount])

  function receiveServer(saved) {
    setServers((previous) =>
      [...previous.filter((item) => item.id !== saved.id), saved].sort((a, b) => a.id - b.id)
    )
  }

  function forgetTools(id) {
    setDiscoveries((previous) => {
      const next = { ...previous }
      delete next[id]
      return next
    })
  }

  async function perform(action) {
    if (inFlight.current || loading) return

    inFlight.current = true
    readVersion.current += 1
    readController.current?.abort()

    setBusy(true)
    onBusyChange(true)
    setError('')
    setNotice('')

    try {
      await action()
    } catch (err) {
      setError(
        err.status
          ? err.message
          : `${err.message} Refresh servers to verify their state before repeating this request.`
      )
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  async function saveServer(body) {
    await perform(async () => {
      const existing = editor.server

      const saved = await api(existing ? `/servers/${existing.id}` : '/servers', {
        method: existing ? 'PUT' : 'POST',
        body,
      })

      receiveServer(saved)
      forgetTools(saved.id)
      setEditor(null)
      setNotice(`Saved "${saved.name}".`)
    })
  }

  async function toggleServer(server) {
    await perform(async () => {
      const saved = await api(`/servers/${server.id}`, {
        method: 'PUT',
        body: { ...config(server), enabled: !server.enabled },
      })

      receiveServer(saved)
      forgetTools(saved.id)
      setNotice(`${saved.name}: ${saved.enabled ? 'Enabled' : 'Disabled'}.`)
    })
  }

  async function checkServer(server) {
    await perform(async () => {
      const result = await api(`/servers/${server.id}/check`, {
        method: 'POST',
      })

      receiveServer(result.server)
      setDiscoveries((previous) => ({
        ...previous,
        [server.id]: result.tools,
      }))
      setNotice(`${result.server.name}: ${result.server.status}.`)
    })
  }

  async function deleteServer(server) {
    await perform(async () => {
      await api(`/servers/${server.id}`, { method: 'DELETE' })

      setServers((previous) => previous.filter((item) => item.id !== server.id))
      forgetTools(server.id)
      setDeleteId(null)
      setEditor((previous) => (previous?.server?.id === server.id ? null : previous))
      setNotice(`Deleted the "${server.name}" registration.`)
    })
  }

  const waiting = busy || loading

  return (
    <section className="ui-panel space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">MCP servers</h1>
          <p className="mt-2 text-sm text-neutral-500">
            Manage the servers available through your gateway.
          </p>
        </div>

        <div className="flex gap-3">
          <button
            type="button"
            disabled={waiting}
            onClick={() => setRefreshCount((value) => value + 1)}
            className={buttonClass}
          >
            Refresh
          </button>

          <button
            type="button"
            disabled={waiting}
            onClick={() => {
              setEditor({ server: null })
              setError('')
              setNotice('')
            }}
            className="ui-primary"
          >
            Add server
          </button>
        </div>
      </header>

      {!loading && (
        <div className="grid grid-cols-3 gap-2 sm:gap-3">
          {[
            { label: 'Registered servers', value: servers.length },
            {
              label: 'Connected',
              value: servers.filter((item) => item.status === 'Connected').length,
            },
            { label: 'Disabled', value: servers.filter((item) => !item.enabled).length },
          ].map((item) => (
            <div
              key={item.label}
              className="rounded-xl border border-neutral-200 bg-neutral-50/50 px-3 py-3 sm:px-5 sm:py-4"
            >
              <p className="text-[10px] font-semibold uppercase tracking-wider text-neutral-400">
                {item.label}
              </p>
              <p className="mt-2 text-2xl font-semibold tracking-tight">{item.value}</p>
            </div>
          ))}
        </div>
      )}
      {error && (
        <p role="alert" className="rounded-xl border border-neutral-400 bg-neutral-100 p-4 text-sm">
          {error}
        </p>
      )}

      {notice && (
        <p role="status" className="text-sm text-neutral-600">
          {notice}
        </p>
      )}

      {busy && (
        <p role="status" className="text-sm text-neutral-500">
          Working…
        </p>
      )}

      {editor && (
        <ServerForm
          key={editor.server?.id ?? 'new'}
          server={editor.server}
          busy={waiting}
          onSave={saveServer}
          onCancel={() => setEditor(null)}
        />
      )}

      {loading ? (
        <p role="status" className="text-sm text-neutral-500">
          Loading servers…
        </p>
      ) : (
        <div className="space-y-4">
          {servers.map((server) => {
            const tools = discoveries[server.id]

            return (
              <article
                key={server.id}
                className="rounded-xl border border-neutral-200 bg-neutral-50/30 p-5 transition-colors hover:border-neutral-300"
              >
                <header className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">{server.name}</h2>
                    <p className="mt-1 font-mono text-xs text-neutral-500">{server.slug}</p>
                  </div>

                  <span className="inline-flex items-center gap-2 rounded border border-neutral-200 bg-white px-3 py-1 text-[10px] font-medium">
                    <span className="h-1.5 w-1.5 rounded-full bg-neutral-500" />
                    {server.status}
                  </span>
                </header>

                <p className="mt-4 break-all font-mono text-xs leading-6">{server.url}</p>

                <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-neutral-500">
                  <span>Timeout: {server.timeout_s}s</span>
                  <span>Credential: {server.has_credential ? 'Configured' : 'None'}</span>
                  <span>Last checked · IST: {displayDate(server.last_checked_at)}</span>
                </div>

                {server.status_reason && (
                  <p className="mt-3 text-sm text-neutral-600">{server.status_reason}</p>
                )}

                <div className="mt-5 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={waiting || !server.enabled}
                    onClick={() => checkServer(server)}
                    className={buttonClass}
                  >
                    Check tools
                  </button>

                  <button
                    type="button"
                    disabled={waiting}
                    onClick={() => toggleServer(server)}
                    className={buttonClass}
                  >
                    {server.enabled ? 'Disable' : 'Enable'}
                  </button>

                  <button
                    type="button"
                    disabled={waiting}
                    onClick={() => {
                      setEditor({ server })
                      setError('')
                      setNotice('')
                    }}
                    className={buttonClass}
                  >
                    Edit
                  </button>

                  <button
                    type="button"
                    disabled={waiting}
                    onClick={() => setDeleteId(server.id)}
                    className={buttonClass}
                  >
                    Delete
                  </button>
                </div>

                {deleteId === server.id && (
                  <div className="mt-4 rounded-lg border border-neutral-400 bg-neutral-50 p-4">
                    <p className="text-sm">
                      Delete the "{server.name}" registration from the gateway?
                    </p>
                    <div className="mt-3 flex gap-3">
                      <button
                        type="button"
                        disabled={waiting}
                        onClick={() => deleteServer(server)}
                        className={buttonClass}
                      >
                        Delete registration
                      </button>
                      <button
                        type="button"
                        disabled={waiting}
                        onClick={() => setDeleteId(null)}
                        className={buttonClass}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {!server.enabled ? (
                  <p className="mt-5 text-xs text-neutral-500">Tools disabled.</p>
                ) : tools === undefined ? (
                  <p className="mt-5 text-xs text-neutral-500">
                    Select Check tools to load tool details.
                  </p>
                ) : (
                  <details className="mt-5 border-t border-neutral-200 pt-4">
                    <summary className="cursor-pointer text-sm font-semibold">
                      {tools.length} tools from last check
                    </summary>

                    <div className="mt-4 space-y-3">
                      {tools.map((tool) => (
                        <div key={tool.name} className="rounded-lg bg-neutral-50 p-4">
                          <p className="break-all font-mono text-xs font-semibold">
                            {server.slug}__{tool.name}
                          </p>

                          {tool.description && (
                            <p className="mt-2 text-xs leading-6 text-neutral-600">
                              {tool.description}
                            </p>
                          )}

                          <details className="mt-3">
                            <summary className="cursor-pointer text-xs text-neutral-500">
                              Input schema
                            </summary>
                            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">
                              {JSON.stringify(tool.inputSchema, null, 2)}
                            </pre>
                          </details>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </article>
            )
          })}

          {!servers.length && !error && (
            <p className="py-8 text-center text-sm text-neutral-500">No servers registered.</p>
          )}
        </div>
      )}
    </section>
  )
}
