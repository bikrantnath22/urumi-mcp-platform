const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const REJECTED = new Set([400, 401, 403, 404, 405, 409, 413, 415, 422, 429])
const RESERVED = new Set(['admin', 'api', 'app', 'platform', 'store', 'www'])

export function creationValues({ name, slug }) {
  if (typeof name !== 'string' || typeof slug !== 'string') {
    throw new Error('Enter a store name and address.')
  }
  name = name.trim()
  slug = slug.trim().toLowerCase()
  if (
    !name ||
    name.length > 100 ||
    [...name].some((character) => {
      const code = character.codePointAt(0)
      return code <= 31 || (code >= 127 && code <= 159)
    })
  ) {
    throw new Error('Use a readable store name of up to 100 characters.')
  }
  if (!/^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/.test(slug) || RESERVED.has(slug)) {
    throw new Error('Use 1–32 letters, numbers or internal hyphens for the address.')
  }
  return { name, slug }
}

export function newStoreRequestId(random = globalThis.crypto) {
  if (typeof random?.randomUUID === 'function') return random.randomUUID()
  if (typeof random?.getRandomValues !== 'function') {
    throw new Error('Secure random generation is unavailable in this browser.')
  }
  const bytes = random.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}

export class StoreCreationRequest {
  constructor(owner, storage, request, makeId = newStoreRequestId) {
    if (!Number.isSafeInteger(owner) || owner < 1 || typeof request !== 'function') {
      throw new Error('A signed-in account is required.')
    }
    this.owner = owner
    this.storage = storage
    this.request = request
    this.makeId = makeId
    this.key = 'urm.store-create.v1.' + owner
    this.pending = null
    this.busy = false
    const saved = storage.getItem(this.key)
    if (saved !== null) {
      try {
        const value = JSON.parse(saved)
        const fields = creationValues(value)
        if (
          Object.keys(value).sort().join(',') !== 'name,request_id,slug' ||
          !UUID.test(value.request_id) ||
          fields.name !== value.name ||
          fields.slug !== value.slug
        ) {
          throw new Error('Invalid saved request')
        }
        this.pending = Object.freeze({ ...fields, request_id: value.request_id })
      } catch {
        throw new Error(
          'A saved store request could not be restored. Check Stores before clearing its browser data.'
        )
      }
    }
  }

  forget() {
    try {
      this.storage.removeItem(this.key)
      this.pending = null
    } catch {
      /* Retaining the same request remains safe to replay. */
    }
  }

  async submit(input) {
    if (this.busy) throw new Error('A store request is already being submitted.')
    this.busy = true
    try {
      const fields = creationValues(input)
      if (
        this.pending &&
        (fields.name !== this.pending.name || fields.slug !== this.pending.slug)
      ) {
        throw new Error('Retry the saved request before starting a different one.')
      }
      if (!this.pending) {
        const request_id = this.makeId()
        if (!UUID.test(request_id))
          throw new Error('A valid request identifier could not be generated.')
        const pending = Object.freeze({ ...fields, request_id })
        this.storage.setItem(this.key, JSON.stringify(pending))
        this.pending = pending
      }
      let result, timer
      try {
        const controller = new AbortController()
        timer = setTimeout(() => controller.abort(), 20_000)
        result = await this.request('/stores', {
          method: 'POST',
          body: { ...this.pending },
          signal: controller.signal,
        })
      } catch (error) {
        if (REJECTED.has(error.status)) this.forget()
        throw error
      } finally {
        clearTimeout(timer)
      }
      const store = result?.store
      if (
        !UUID.test(store?.id || '') ||
        store.owner_id !== this.owner ||
        store.name !== this.pending.name ||
        store.slug !== this.pending.slug ||
        result.operation?.action !== 'provision' ||
        result.operation.store_id !== store.id
      ) {
        throw new Error(
          'The reply could not be confirmed. Check Stores; retry checks the same request.'
        )
      }
      this.forget()
      return result
    } finally {
      this.busy = false
    }
  }
}
