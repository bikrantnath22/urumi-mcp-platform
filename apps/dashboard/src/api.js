let authenticationEpoch = 0
let authenticationChannel
try {
  authenticationChannel = new BroadcastChannel('urumi-session')
  authenticationChannel.addEventListener('message', () => {
    authenticationEpoch += 1
  })
} catch {
  /* Optional cross-tab coordination. */
}
if (import.meta.hot) import.meta.hot.dispose(() => authenticationChannel?.close())

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

function csrfToken() {
  const cookie = document.cookie.split('; ').find((item) => item.startsWith('urm_csrf='))

  return cookie ? decodeURIComponent(cookie.slice('urm_csrf='.length)) : ''
}

function errorMessage(data) {
  if (typeof data?.error?.message === 'string') {
    return data.error.message
  }
  if (typeof data?.detail === 'string') return data.detail
  if (Array.isArray(data?.detail)) {
    return data.detail.map((item) => item.msg).join('; ')
  }
  return 'The request could not be completed.'
}

export async function api(path, { method = 'GET', body, upload, signal } = {}) {
  if (body !== undefined && upload !== undefined) {
    throw new Error('Choose either a JSON body or a file upload.')
  }
  const headers = new Headers()
  if (upload !== undefined) {
    headers.set('Content-Type', upload.type || 'application/octet-stream')
    headers.set('X-File-Name', encodeURIComponent(upload.name))
  }

  if (body !== undefined) headers.set('Content-Type', 'application/json')

  if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
    const token = csrfToken()
    if (token) headers.set('X-CSRF-Token', token)
  }

  const requestEpoch = authenticationEpoch
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    credentials: 'same-origin',
    body: upload !== undefined ? upload : body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  if (response.ok && ['/auth/login', '/auth/logout'].includes(path)) {
    authenticationEpoch += 1
    try {
      authenticationChannel?.postMessage({ type: 'authentication-changed' })
    } catch {
      /* Optional notification. */
    }
  }
  if (
    response.status === 401 &&
    requestEpoch === authenticationEpoch &&
    !['/auth/login', '/auth/accept-invite'].includes(path)
  ) {
    window.dispatchEvent(new Event('urumi-session-expired'))
  }
  if (response.status === 204) return null

  let data
  try {
    data = await response.json()
  } catch {
    throw new ApiError(
      'Unexpected server response. Check that the gateway is running.',
      response.status,
      null
    )
  }

  if (!response.ok) {
    const seconds = Number(
      response.headers.get('Retry-After') || data?.error?.retry_after_s || data?.retry_after_s || 0
    )
    const message = errorMessage(data)
    const error = new ApiError(
      response.status === 429 && seconds > 0
        ? `${message} Try again after ${seconds} seconds.`
        : message,
      response.status,
      data
    )
    error.retryAfterS = Number.isFinite(seconds) ? Math.max(0, seconds) : 0
    throw error
  }

  return data
}
