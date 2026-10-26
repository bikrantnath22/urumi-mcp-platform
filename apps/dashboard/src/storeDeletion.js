import { newStoreRequestId } from './storeCreation.js'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const REJECTED = new Set([400, 401, 403, 404, 405, 409, 413, 415, 422, 429])

export class StoreDeletionRequest {
  constructor(user, store, storage, request, makeId = newStoreRequestId) {
    if (
      !Number.isSafeInteger(user) ||
      user < 1 ||
      !UUID.test(store) ||
      typeof request !== 'function'
    )
      throw new Error('A signed-in account and store are required.')
    this.key = 'urm.store-delete.v1.' + user + '.' + store
    this.store = store
    this.storage = storage
    this.request = request
    this.makeId = makeId
    this.pending = null
    this.busy = false
    const saved = storage.getItem(this.key)
    if (saved !== null) {
      try {
        const value = JSON.parse(saved)
        if (
          Object.keys(value).sort().join(',') !== 'generation,request_id' ||
          !UUID.test(value.request_id) ||
          !Number.isSafeInteger(value.generation) ||
          value.generation < 1
        )
          throw new Error('Invalid request')
        this.pending = Object.freeze(value)
      } catch {
        throw new Error(
          'The saved deletion could not be restored. Refresh Stores before clearing browser data.'
        )
      }
    }
  }

  forget() {
    try {
      this.storage.removeItem(this.key)
      this.pending = null
    } catch {
      /* Keep the original request safe to replay. */
    }
  }

  async submit(generation) {
    if (this.busy) throw new Error('A deletion request is already being submitted.')
    this.busy = true
    try {
      if (!this.pending) {
        const request_id = this.makeId()
        if (!Number.isSafeInteger(generation) || generation < 1 || !UUID.test(request_id)) {
          throw new Error('A valid store deletion identifier is required.')
        }
        const value = Object.freeze({ generation, request_id })
        this.storage.setItem(this.key, JSON.stringify(value))
        this.pending = value
      }
      let result, timer
      try {
        const controller = new AbortController()
        timer = setTimeout(() => controller.abort(), 20_000)
        result = await this.request('/stores/' + this.store, {
          method: 'DELETE',
          body: { ...this.pending },
          signal: controller.signal,
        })
      } catch (error) {
        if (REJECTED.has(error.status)) this.forget()
        throw error
      } finally {
        clearTimeout(timer)
      }
      if (
        result?.store?.id !== this.store ||
        !['Deleting', 'Deleted'].includes(result.store.status) ||
        result.operation?.action !== 'delete' ||
        result.operation.store_id !== this.store
      ) {
        throw new Error(
          'Deletion could not be confirmed. Refresh Stores or retry the saved request.'
        )
      }
      this.forget()
      return result
    } finally {
      this.busy = false
    }
  }
}
