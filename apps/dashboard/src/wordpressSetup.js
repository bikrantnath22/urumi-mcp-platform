// A setup capability lives only in memory and is POSTed to the selected store.
export function canSetupWordPress(store, user) {
  return (
    Number.isSafeInteger(user?.id) &&
    user.id > 0 &&
    store?.owner_id === user.id &&
    store.status === 'Ready' &&
    store.access_enabled !== false &&
    Boolean(store.access_guarded) &&
    Number.isSafeInteger(store.generation) &&
    store.generation >= 1
  )
}

export async function prepareWordPressSetup(store, user, request, signal) {
  if (!canSetupWordPress(store, user)) {
    throw new Error(
      'Only the store owner can set the WordPress password for an enabled Ready store.'
    )
  }
  const { id, generation } = store
  const target = new URL(store.url)
  if (
    !['http:', 'https:'].includes(target.protocol) ||
    target.username ||
    target.password ||
    target.pathname !== '/' ||
    target.search ||
    target.hash
  ) {
    throw new Error('The store website address is invalid.')
  }
  const url = target.origin + '/wp-login.php?action=urumi_setup'
  const result = await request('/stores/' + id + '/wordpress-setup', {
    method: 'POST',
    body: { generation },
    signal,
  })
  if (
    result?.store_id !== id ||
    result.url !== url ||
    !/^[A-Za-z0-9_-]{43}$/.test(result.token || '') ||
    !Number.isFinite(Date.parse(result.expires_at)) ||
    Date.parse(result.expires_at) <= Date.now()
  ) {
    throw new Error('WordPress setup could not be confirmed. Start setup again.')
  }
  return Object.freeze({ url, token: result.token, expires_at: result.expires_at })
}
