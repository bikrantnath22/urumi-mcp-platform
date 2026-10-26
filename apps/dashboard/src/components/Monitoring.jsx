import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { startMonitoring } from '../monitoring.js'
import Icon from './Icon.jsx'

const time = (value) =>
  value
    ? new Date(value).toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        dateStyle: 'medium',
        timeStyle: 'short',
      }) + ' IST'
    : 'Not checked'
const metric = (value, suffix = '') =>
  value === null ? '-' : value.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + suffix

function Card({ title, value, detail }) {
  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-5">
      <p className="text-xs font-medium text-neutral-500">{title}</p>
      <p className="mt-3 text-2xl font-semibold tracking-tight">{value}</p>
      <p className="mt-2 text-xs leading-5 text-neutral-500">{detail}</p>
    </div>
  )
}

export default function Monitoring({ active }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [windowMinutes, setWindowMinutes] = useState(60)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    if (!active) return
    return startMonitoring(
      api,
      (value) => {
        setData(value)
        setError('')
      },
      setError,
      { windowMinutes }
    )
  }, [active, windowMinutes, refresh])
  const maximum = Math.max(1, ...(data?.series.map((point) => point.calls) ?? []))
  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Monitoring</h1>
          <p className="mt-2 text-sm text-neutral-500">
            Tool activity, upstream observations and store provisioning.
          </p>
        </div>
        <div className="flex gap-3">
          <label className="sr-only" htmlFor="monitor-window">
            Time window
          </label>
          <select
            id="monitor-window"
            className="ui-input w-auto"
            value={windowMinutes}
            onChange={(event) => {
              setData(null)
              setError('')
              setWindowMinutes(Number(event.target.value))
            }}
          >
            <option value={60}>Last hour</option>
            <option value={360}>Last 6 hours</option>
            <option value={1440}>Last 24 hours</option>
          </select>
          <button
            type="button"
            className="ui-button"
            onClick={() => {
              setData(null)
              setError('')
              setRefresh((value) => value + 1)
            }}
          >
            <Icon name="refresh" />
            Refresh
          </button>
        </div>
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"
        >
          {error}
        </p>
      )}
      {!data && !error && (
        <p role="status" className="text-sm text-neutral-500">
          Loading monitoring…
        </p>
      )}
      {data && (
        <>
          <p className="text-xs leading-6 text-neutral-500">
            Snapshot: {time(data.generated_at)}. Refreshes every 30 seconds while visible; results
            may be cached for 20 seconds. Upstream status reflects its last discovery or call.
          </p>
          {data.truncated && (
            <p role="status" className="ui-banner text-sm">
              The server table is limited to 200 entries. Overall totals include all recorded calls.
            </p>
          )}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Card
              title="Tool calls"
              value={metric(data.tools.calls)}
              detail={`${data.tools.success} succeeded · ${data.tools.denied} denied · ${data.tools.uncertain} unconfirmed`}
            />
            <Card
              title="Error rate"
              value={metric(data.tools.error_rate_percent, '%')}
              detail={`${data.tools.errors} failed · excludes denied and unconfirmed calls`}
            />
            <Card
              title="P95 tool latency"
              value={metric(data.tools.p95_ms, ' ms')}
              detail={`Average ${metric(data.tools.average_ms, ' ms')} · confirmed executions`}
            />
            <Card
              title="Active store jobs"
              value={metric(data.jobs.queued + data.jobs.running)}
              detail={`${data.jobs.queued} queued · ${data.jobs.running} running · ${data.jobs.overdue} past deadline`}
            />
          </div>
          <div className="rounded-xl border border-neutral-200 bg-white p-5 sm:p-6">
            <h2 className="text-sm font-semibold">Alerts</h2>
            <p className="mt-1 text-xs text-neutral-500">
              Alerts appear here when this page refreshes. No email or external notifications are
              sent.
            </p>
            {data.alerts.length ? (
              <ul className="mt-4 grid gap-3">
                {data.alerts.map((alert) => (
                  <li
                    key={alert.code}
                    className={`rounded-lg border p-3 text-sm ${alert.severity === 'critical' ? 'border-red-200 bg-red-50 text-red-900' : 'border-amber-200 bg-amber-50 text-amber-900'}`}
                  >
                    <strong>{alert.count}</strong> · {alert.message}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-4 text-sm text-neutral-600">No alerts in this snapshot.</p>
            )}
          </div>
          <figure className="rounded-xl border border-neutral-200 bg-white p-5 sm:p-6">
            <figcaption className="text-sm font-semibold">Tool calls over time</figcaption>
            <p className="mt-1 text-xs text-neutral-500">
              Each bar covers one twelfth of the selected window. Red shows confirmed failures.
            </p>
            <div
              className="mt-6 flex h-40 items-end gap-2"
              role="img"
              aria-label={`${data.tools.calls} tool calls in the selected window; ${data.tools.errors} confirmed failures.`}
            >
              {data.series.map((point) => (
                <div
                  key={point.start}
                  className="flex h-full min-w-0 flex-1 flex-col justify-end"
                  title={`${time(point.start)} – ${time(point.end)}: ${point.calls} calls, ${point.errors} failures`}
                >
                  <div
                    className="relative min-h-px overflow-hidden rounded-t bg-neutral-700"
                    style={{ height: `${(point.calls / maximum) * 100}%` }}
                  >
                    <div
                      className="absolute inset-x-0 top-0 bg-red-500"
                      style={{ height: `${point.calls ? (point.errors / point.calls) * 100 : 0}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-3 flex justify-between text-[10px] text-neutral-500">
              <span>{time(data.series[0].start)}</span>
              <span>{time(data.series[11].end)}</span>
            </div>
          </figure>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card
              title="Completed creations"
              value={metric(data.jobs.created)}
              detail={`Average request-to-completion ${metric(data.jobs.average_creation_seconds, ' seconds')} in this window`}
            />
            <Card
              title="Failed creations"
              value={metric(data.jobs.failed_creations)}
              detail="Finished creation failures in this window; cleanup can continue beyond the original deadline."
            />
            <Card
              title="Store inventory"
              value={metric(data.stores.Ready) + ' Ready'}
              detail={`${data.stores.Provisioning} provisioning · ${data.stores.Failed} failed · ${data.stores.Deleting} deleting`}
            />
          </div>
          <div className="rounded-xl border border-neutral-200 bg-white p-5">
            <h2 className="text-sm font-semibold">Gateway HTTP requests</h2>
            <p className="mt-2 text-sm text-neutral-700">
              {metric(data.http_process.requests)} requests · {data.http_process.errors} server
              errors · {data.http_process.rejected} rejected · {data.http_process.cancelled}{' '}
              cancelled · average {metric(data.http_process.average_ms, ' ms')}
            </p>
            <p className="mt-2 text-xs leading-5 text-neutral-500">
              This gateway process since {time(data.http_process.since)}. Resets on restart;
              excludes probes and monitoring polls. Tool and store metrics above come from
              PostgreSQL and survive restarts.
            </p>
          </div>
          <div className="overflow-hidden rounded-xl border border-neutral-200 bg-white">
            <div className="p-5">
              <h2 className="text-sm font-semibold">Upstreams</h2>
              <p className="mt-1 text-xs text-neutral-500">
                Latency and call counts use the selected window. Status is the last recorded
                observation.
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[950px] text-left text-xs">
                <thead className="border-y border-neutral-200 bg-neutral-50 text-neutral-500">
                  <tr>
                    {[
                      'Server',
                      'Last observed status',
                      'Calls',
                      'Errors',
                      'Error rate',
                      'Average',
                      'P95',
                    ].map((label) => (
                      <th key={label} className="px-5 py-3 font-medium">
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-100">
                  {data.servers.map((server) => (
                    <tr key={server.id}>
                      <td className="px-5 py-4">
                        <p className="font-semibold">{server.name}</p>
                        <p className="mt-1 text-neutral-500">{server.slug}</p>
                      </td>
                      <td className="max-w-72 px-5 py-4">
                        <p
                          className={
                            server.status === 'Unreachable'
                              ? 'font-semibold text-amber-700'
                              : 'font-medium'
                          }
                        >
                          {server.status}
                        </p>
                        <p className="mt-1 leading-5 text-neutral-500">{server.status_note}</p>
                        <p className="mt-1 text-[10px] text-neutral-400">
                          {time(server.last_checked_at)}
                        </p>
                      </td>
                      <td className="px-5 py-4">{server.metrics.calls}</td>
                      <td className="px-5 py-4">{server.metrics.errors}</td>
                      <td className="px-5 py-4">
                        {metric(server.metrics.error_rate_percent, '%')}
                      </td>
                      <td className="px-5 py-4">{metric(server.metrics.average_ms, ' ms')}</td>
                      <td className="px-5 py-4">{metric(server.metrics.p95_ms, ' ms')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!data.servers.length && (
                <p className="p-5 text-sm text-neutral-500">No upstream servers registered.</p>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  )
}
