import { useCallback, useEffect, useRef, useState } from 'react'

export default function useConfirmation() {
  const [request, setRequest] = useState(null)
  const resolver = useRef(null)
  const confirm = useCallback((options) => {
    if (resolver.current) return Promise.resolve(false)
    return new Promise((resolve) => {
      resolver.current = resolve
      setRequest(options)
    })
  }, [])
  const finish = useCallback((accepted) => {
    const resolve = resolver.current
    resolver.current = null
    setRequest(null)
    resolve?.(accepted)
  }, [])
  useEffect(
    () => () => {
      resolver.current?.(false)
      resolver.current = null
    },
    []
  )
  return { request, confirm, finish }
}
