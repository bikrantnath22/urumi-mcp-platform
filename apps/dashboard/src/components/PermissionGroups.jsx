import { useId, useState } from 'react'
import { permissionGroups } from '../permissionGroups.js'
import Icon from './Icon.jsx'

export default function PermissionGroups({ catalog, grants, disabled, onChange }) {
  const id = useId()
  const [query, setQuery] = useState('')
  const [access, setAccess] = useState('all')
  const [opened, setOpened] = useState({})
  const groups = permissionGroups(catalog, grants, query, access)
  const filtering = Boolean(query.trim()) || access !== 'all'
  const visible = groups.reduce((total, group) => total + group.tools.length, 0)

  function changeQuery(value) {
    setQuery(value)
    setOpened({})
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-[1fr_180px]">
        <label className="grid gap-1.5 text-xs font-medium text-neutral-600">
          Search connections and tools
          <input
            type="search"
            className="ui-input"
            value={query}
            placeholder="Search by store, service or tool"
            onChange={(event) => changeQuery(event.target.value)}
          />
        </label>
        <label className="grid gap-1.5 text-xs font-medium text-neutral-600">
          Access
          <select
            className="ui-input"
            value={access}
            onChange={(event) => {
              setAccess(event.target.value)
              setOpened({})
            }}
          >
            <option value="all">All tools</option>
            <option value="granted">Granted</option>
            <option value="ungranted">No access</option>
            <option value="unavailable">Unavailable</option>
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" className="text-xs text-neutral-500">
          {groups.length} connections · {visible} tools
        </p>
        <div className="flex gap-3 text-xs font-medium">
          <button
            type="button"
            className="rounded px-1 py-1 text-neutral-600 hover:text-neutral-950"
            onClick={() => setOpened(Object.fromEntries(groups.map((group) => [group.slug, true])))}
          >
            Expand all
          </button>
          <button
            type="button"
            className="rounded px-1 py-1 text-neutral-600 hover:text-neutral-950"
            onClick={() =>
              setOpened(Object.fromEntries(groups.map((group) => [group.slug, false])))
            }
          >
            Collapse all
          </button>
        </div>
      </div>
      <div className="space-y-3">
        {groups.map((group) => {
          const expanded = Object.hasOwn(opened, group.slug) ? opened[group.slug] : filtering
          const panel = `${id}-${group.slug}`
          return (
            <section
              key={group.slug}
              className="overflow-hidden rounded-xl border border-neutral-200 bg-white"
            >
              <h4>
                <button
                  type="button"
                  aria-expanded={expanded}
                  aria-controls={panel}
                  className="flex w-full items-center gap-3 bg-neutral-50/70 p-4 text-left hover:bg-neutral-100/80"
                  onClick={() => setOpened((current) => ({ ...current, [group.slug]: !expanded }))}
                >
                  <Icon
                    name="chevron"
                    className={`h-4 w-4 shrink-0 text-neutral-400 transition-transform ${expanded ? 'rotate-90' : ''}`}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block break-words text-sm font-semibold text-neutral-900">
                      {group.title}
                    </span>
                    <span className="mt-1 block break-all font-mono text-[11px] font-normal text-neutral-500">
                      {group.slug}
                    </span>
                    <span className="mt-1 block text-xs font-normal text-neutral-500">
                      {group.granted} of {group.total} granted
                      {group.available < group.total &&
                        ` · ${group.total - group.available} unavailable`}
                    </span>
                  </span>
                  <span className="shrink-0 rounded border border-neutral-200 bg-white px-2.5 py-1 text-xs font-medium text-neutral-600">
                    {group.available === 0 ? 'Unavailable' : `${group.total} tools`}
                  </span>
                </button>
              </h4>
              <div id={panel} hidden={!expanded} className="divide-y divide-neutral-100">
                {group.tools.map((tool) => (
                  <div key={tool.name} className="flex items-start gap-3 p-4 sm:pl-11">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-sm font-medium text-neutral-900">{tool.label}</p>
                        <span
                          className={`rounded px-2 py-0.5 text-[11px] font-medium ${tool.granted ? 'bg-emerald-50 text-emerald-800' : 'bg-neutral-100 text-neutral-500'}`}
                        >
                          {tool.granted ? 'Granted' : 'No access'}
                        </span>
                      </div>
                      {!tool.available && (
                        <p className="mt-1 text-xs text-neutral-500">
                          Currently unavailable; existing access can be revoked.
                        </p>
                      )}
                      <details className="mt-2 text-xs text-neutral-500">
                        <summary className="w-fit cursor-pointer rounded hover:text-neutral-900">
                          Tool details
                        </summary>
                        <p className="mt-2 break-all font-mono leading-5">{tool.name}</p>
                        <p className="mt-1 whitespace-pre-line break-words leading-5">
                          {tool.description}
                        </p>
                      </details>
                    </div>
                    <button
                      type="button"
                      disabled={disabled || (!tool.available && !tool.granted)}
                      aria-label={`${tool.granted ? 'Revoke' : 'Grant'} ${tool.label} on ${group.title} (${group.slug})`}
                      className="ui-button shrink-0"
                      onClick={() => onChange(tool.name, tool.granted)}
                    >
                      {tool.granted ? 'Revoke' : 'Grant'}
                    </button>
                  </div>
                ))}
              </div>
            </section>
          )
        })}
        {!groups.length && (
          <p className="rounded-xl border border-dashed border-neutral-200 p-6 text-center text-sm text-neutral-500">
            {catalog.length || grants.length
              ? 'No tools match these filters.'
              : 'No tools are currently available.'}
          </p>
        )}
      </div>
    </div>
  )
}
