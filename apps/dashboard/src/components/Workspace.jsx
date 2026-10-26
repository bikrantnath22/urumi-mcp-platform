import { useCallback, useState } from 'react'
import Chat from './Chat.jsx'
import UsageLimits from './UsageLimits.jsx'
import ApiKeys from './ApiKeys.jsx'
import Audit from './Audit.jsx'
import Servers from './Servers.jsx'
import Monitoring from './Monitoring.jsx'
import Stores from './Stores.jsx'
import Users from './Users.jsx'
import Icon from './Icon.jsx'
import SessionStatus from './SessionStatus.jsx'
import useDashboardSession from './useDashboardSession.js'

function restoreWorkspacePage(user) {
  try {
    const saved = window.sessionStorage.getItem('urm.workspace-page.' + user.id)
    const allowed =
      user.role === 'admin'
        ? ['chat', 'stores', 'keys', 'audit', 'servers', 'users', 'limits', 'monitoring']
        : ['chat', 'stores', 'keys', 'audit']
    return allowed.includes(saved) ? saved : 'chat'
  } catch {
    return 'chat'
  }
}

export default function Workspace({
  user,
  onBusyChange,
  onLogout,
  signingOut,
  sessionError,
  onReauthenticate,
}) {
  const [page, setPage] = useState(() => restoreWorkspacePage(user))
  const session = useDashboardSession(user)
  const [mobileMenu, setMobileMenu] = useState(false)
  const [keysVisited, setKeysVisited] = useState(() => page === 'keys')
  const [auditVisited, setAuditVisited] = useState(() => page === 'audit')
  const [serversVisited, setServersVisited] = useState(() => page === 'servers')
  const [usersVisited, setUsersVisited] = useState(() => page === 'users')
  const [limitsVisited, setLimitsVisited] = useState(() => page === 'limits')
  const [busy, setBusy] = useState(false)
  const handleBusy = useCallback(
    (value) => {
      setBusy(value)
      onBusyChange(value)
    },
    [onBusyChange]
  )
  const pages = [
    { id: 'chat', label: 'Chat' },
    { id: 'stores', label: 'Stores' },
    { id: 'keys', label: 'My API keys' },
    { id: 'audit', label: 'Audit history' },
    ...(user.role === 'admin'
      ? [
          { id: 'servers', label: 'MCP servers' },
          { id: 'users', label: 'Users & invites' },
          { id: 'limits', label: 'Usage limits' },
          { id: 'monitoring', label: 'Monitoring' },
        ]
      : []),
  ]
  function navigate(id) {
    if (busy || signingOut) return
    if (id === 'keys') setKeysVisited(true)
    if (id === 'audit') setAuditVisited(true)
    if (id === 'servers') setServersVisited(true)
    if (id === 'users') setUsersVisited(true)
    if (id === 'limits') setLimitsVisited(true)
    try {
      window.sessionStorage.setItem(`urm.workspace-page.${user.id}`, id)
    } catch {
      /* Browser storage is optional. */
    }
    setPage(id)
    setMobileMenu(false)
  }
  const initials = user.email.slice(0, 2).toUpperCase()
  return (
    <div className="min-h-dvh bg-[#f5f5f5] text-neutral-900">
      <a
        href="#workspace-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-white focus:px-4 focus:py-3"
      >
        Skip to content
      </a>
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 flex-col overflow-hidden border-r border-white/5 bg-[#252525] text-neutral-300 lg:flex">
        <a href="/" className="flex shrink-0 items-center gap-3 px-6 py-7">
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-white text-lg font-semibold text-neutral-900">
            U
          </span>
          <div>
            <p className="text-base font-semibold tracking-tight text-white">Urumi</p>
            <p className="mt-0.5 text-[9px] uppercase tracking-[0.2em] text-neutral-400">
              Team workspace
            </p>
          </div>
        </a>
        <nav
          aria-label="Main navigation"
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-3 pt-5"
        >
          <p className="mb-3 px-3 text-[10px] font-semibold uppercase tracking-[0.15em] text-neutral-400">
            Workspace
          </p>
          {pages.map((item) => (
            <div key={item.id}>
              {item.id === 'servers' && (
                <p className="mb-3 mt-9 px-3 text-[10px] font-semibold uppercase tracking-[0.15em] text-neutral-400">
                  Administration
                </p>
              )}
              <button
                type="button"
                disabled={busy || signingOut}
                onClick={() => navigate(item.id)}
                aria-current={page === item.id ? 'page' : undefined}
                className={`mb-1 flex w-full items-center gap-3 rounded-lg border px-3 py-3 text-[13px] font-medium transition-colors disabled:opacity-50 ${page === item.id ? 'border-white/10 bg-white/10 text-white' : 'border-transparent text-neutral-400 hover:bg-white/5 hover:text-white'}`}
              >
                {item.label}
                {page === item.id && (
                  <span className="ml-auto h-1.5 w-1.5 rounded-full bg-neutral-300" />
                )}
              </button>
            </div>
          ))}
        </nav>
        <div className="m-3 shrink-0 rounded-xl border border-white/10 bg-white/[0.025] p-4">
          <div className="flex items-center gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-white/10 bg-white/5 text-xs font-medium text-neutral-300">
              {initials}
            </span>
            <div className="min-w-0">
              <p title={user.email} className="truncate text-xs font-medium text-neutral-200">
                {user.email}
              </p>
              <p className="mt-1 text-[10px] capitalize text-neutral-400">{user.role} account</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onLogout}
            disabled={busy || signingOut}
            className="mt-4 flex w-full items-center gap-2 border-t border-white/10 pt-3 text-xs text-neutral-400 transition-colors hover:text-white disabled:opacity-50"
          >
            {signingOut ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      </aside>
      <div className="min-w-0 lg:ml-60">
        <header className="sticky top-0 z-30 flex h-[72px] items-center justify-between gap-3 border-b border-neutral-200 bg-white/95 px-5 backdrop-blur sm:px-8 xl:px-10">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              aria-label={mobileMenu ? 'Close navigation' : 'Open navigation'}
              aria-expanded={mobileMenu}
              aria-controls="mobile-navigation"
              disabled={busy || signingOut}
              onClick={() => setMobileMenu((value) => !value)}
              className="rounded-lg p-2 text-neutral-600 hover:bg-neutral-100 disabled:opacity-50 lg:hidden"
            >
              <Icon name={mobileMenu ? 'close' : 'menu'} className="h-5 w-5" />
            </button>
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-neutral-900 text-sm font-semibold text-white lg:hidden">
              U
            </span>
            <span className="hidden text-xs text-neutral-500 sm:inline">Workspace</span>
            <Icon name="chevron" className="hidden h-3.5 w-3.5 text-neutral-300 sm:block" />
            <span className="truncate text-xs font-medium text-neutral-700">
              {pages.find((item) => item.id === page).label}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <span className="rounded border border-neutral-200 bg-neutral-50 px-2.5 py-1 text-[10px] font-semibold capitalize tracking-wide text-neutral-600">
              {user.role}
            </span>
            <span
              title={user.email}
              className="grid h-8 w-8 place-items-center rounded border border-neutral-200 bg-neutral-100 text-[10px] font-semibold text-neutral-600"
            >
              {initials}
            </span>
            <button
              type="button"
              onClick={onLogout}
              disabled={busy || signingOut}
              aria-label="Sign out"
              className="rounded-lg p-2 text-neutral-500 disabled:opacity-50 lg:hidden"
            >
              <Icon name="logout" />
            </button>
          </div>
          {mobileMenu && (
            <nav
              id="mobile-navigation"
              aria-label="Mobile navigation"
              onKeyDown={(event) => {
                if (event.key === 'Escape') setMobileMenu(false)
              }}
              className="absolute left-0 right-0 top-full grid max-h-[calc(100dvh_-_72px)] gap-1 overflow-y-auto overscroll-contain border-b border-neutral-200 bg-white p-3 shadow-lg lg:hidden"
            >
              {pages.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  disabled={busy || signingOut}
                  onClick={() => navigate(item.id)}
                  aria-current={page === item.id ? 'page' : undefined}
                  className={`flex items-center gap-3 rounded-lg px-4 py-3 text-left text-sm font-medium disabled:opacity-50 ${page === item.id ? 'bg-neutral-900 text-white' : 'text-neutral-600 hover:bg-neutral-100'}`}
                >
                  {item.label}
                </button>
              ))}
            </nav>
          )}
        </header>
        <main
          id="workspace-content"
          className="mx-auto max-w-[1440px] px-4 py-6 sm:px-8 sm:py-8 xl:px-10"
        >
          <SessionStatus
            user={user}
            session={session}
            onSignedIn={onReauthenticate}
            onLogout={onLogout}
          />
          {sessionError && (
            <p role="alert" className="ui-banner mb-5">
              {sessionError}
            </p>
          )}
          <div hidden={page !== 'chat'}>
            <Chat user={user} onBusyChange={handleBusy} />
          </div>
          {page === 'stores' && <Stores key={user.id} user={user} onBusyChange={handleBusy} />}
          {keysVisited && (
            <div hidden={page !== 'keys'}>
              <ApiKeys onBusyChange={handleBusy} />
            </div>
          )}
          {auditVisited && (
            <div hidden={page !== 'audit'}>
              <Audit user={user} active={page === 'audit'} />
            </div>
          )}
          {user.role === 'admin' && usersVisited && (
            <div hidden={page !== 'users'}>
              <Users currentUser={user} active={page === 'users'} onBusyChange={handleBusy} />
            </div>
          )}
          {user.role === 'admin' && serversVisited && (
            <div hidden={page !== 'servers'}>
              <Servers active={page === 'servers'} onBusyChange={handleBusy} />
            </div>
          )}
          {user.role === 'admin' && page === 'monitoring' && <Monitoring active />}
          {user.role === 'admin' && limitsVisited && (
            <div hidden={page !== 'limits'}>
              <UsageLimits active={page === 'limits'} onBusyChange={handleBusy} />
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
