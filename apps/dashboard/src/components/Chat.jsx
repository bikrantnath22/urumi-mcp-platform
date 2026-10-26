import { useCallback, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api.js'
import { newId } from '../uuid.js'
import Icon from './Icon.jsx'
import ImageDestination from './ImageDestination.jsx'
import ChatRunStatus from './ChatRunStatus.jsx'
import ConfirmDialog from './ConfirmDialog.jsx'
import useConfirmation from './useConfirmation.js'

const examples = [
  'List five products.',
  'Show today’s order counts.',
  'Summarize sales by currency.',
]

const markdownClasses =
  'text-sm leading-7 break-words [&_p]:my-3 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0 [&_h1]:my-4 [&_h1]:text-xl [&_h1]:font-bold [&_h2]:my-4 [&_h2]:text-lg [&_h2]:font-bold [&_h3]:my-3 [&_h3]:font-semibold [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-1 [&_a]:underline [&_a]:underline-offset-2 [&_pre]:overflow-auto [&_pre]:rounded-lg [&_pre]:bg-neutral-100 [&_pre]:p-3 [&_code]:font-mono [&_code]:text-xs [&_hr]:my-4 [&_hr]:border-neutral-200 [&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-neutral-200 [&_th]:p-2 [&_td]:border [&_td]:border-neutral-200 [&_td]:p-2'

function toolText(call) {
  if (!call.result) {
    return 'No result returned. Verify the outcome before repeating this action.'
  }

  const texts = (call.result.content || [])
    .filter((item) => item.type === 'text')
    .map((item) => {
      try {
        return JSON.stringify(JSON.parse(item.text), null, 2)
      } catch {
        return item.text
      }
    })

  return texts.length
    ? texts.join('\n\n')
    : JSON.stringify(call.result.structuredContent || call.result, null, 2)
}

function rememberChat(userId, id) {
  try {
    localStorage.setItem(`urumi-active-chat:${userId}`, id || 'new')
  } catch {
    /* optional preference */
  }
}

function rememberedChat(userId) {
  try {
    return localStorage.getItem(`urumi-active-chat:${userId}`)
  } catch {
    return null
  }
}

function dateText(value) {
  if (!value) return ''
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  }).format(new Date(value))
}

function uniqueTurns(items) {
  return [...new Map(items.map((item) => [item.id, item])).values()].sort(
    (a, b) => (a.created_at || '').localeCompare(b.created_at || '') || a.id.localeCompare(b.id)
  )
}

function ToolActivity({ calls }) {
  if (!calls.length) return null

  const labels = {
    completed: 'Completed',
    tool_error: 'Error',
    rate_limited: 'Limited',
    unknown: 'Unconfirmed',
    pending: 'Pending',
  }

  return (
    <div className="mt-4 space-y-2">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500">
        Tool activity
      </p>

      {calls.map((call, index) => (
        <details
          key={`${call.name}-${index}`}
          className="rounded-lg border border-neutral-200 bg-neutral-50/60"
        >
          <summary className="cursor-pointer px-3 py-2.5 text-xs">
            <span className="font-mono font-semibold">{call.name}</span>
            <span className="ml-3 text-neutral-500">{labels[call.status] || call.status}</span>
          </summary>

          <div className="space-y-3 border-t border-neutral-200 p-4">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500">
              Arguments
            </p>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs">
              {JSON.stringify(call.arguments, null, 2)}
            </pre>

            <p className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500">
              Result
            </p>
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs leading-6">
              {toolText(call)}
            </pre>
          </div>
        </details>
      ))}
    </div>
  )
}

export default function Chat({ user, onBusyChange }) {
  const confirmation = useConfirmation()
  const [chatNotice, setChatNotice] = useState('')
  const [deleteError, setDeleteError] = useState('')
  const [draft, setDraft] = useState('')
  const [turns, setTurns] = useState([])
  const [busy, setBusy] = useState(false)
  const [conversationId, setConversationId] = useState('')
  const [conversations, setConversations] = useState([])
  const [moreConversations, setMoreConversations] = useState(null)
  const [olderMessages, setOlderMessages] = useState(null)
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState('')
  const [historyRevision, setHistoryRevision] = useState(0)
  const historyVersion = useRef(0)
  const loadedOlder = useRef(false)
  const [attachment, setAttachment] = useState(null)
  const [attachmentError, setAttachmentError] = useState('')
  const selectDestination = useCallback((id, name) => {
    setAttachment((current) => (current?.id === id ? { ...current, upload_tool: name } : current))
  }, [])
  const fileInput = useRef(null)
  const inFlight = useRef(false)
  const endRef = useRef(null)
  const transcriptRef = useRef(null)

  useEffect(() => {
    if (turns.length) endRef.current?.scrollIntoView({ block: 'nearest' })
    else if (transcriptRef.current) transcriptRef.current.scrollTop = 0
  }, [turns, busy])

  const loadChat = useCallback(
    async (id, { older = false, before = null, poll = false, signal } = {}) => {
      const version = ++historyVersion.current
      if (!poll) setHistoryLoading(true)
      setHistoryError('')
      try {
        const query = before ? `?before_id=${encodeURIComponent(before)}` : ''
        const data = await api(`/chats/${id}${query}`, { signal })
        if (signal?.aborted || version !== historyVersion.current) return
        setConversationId(data.conversation.id)
        rememberChat(user.id, data.conversation.id)
        setConversations((current) =>
          current.some((item) => item.id === data.conversation.id)
            ? current.map((item) => (item.id === data.conversation.id ? data.conversation : item))
            : [...current, data.conversation]
        )
        setTurns((current) =>
          older
            ? uniqueTurns([...data.turns, ...current])
            : poll
              ? uniqueTurns([...current, ...data.turns])
              : data.turns
        )
        if (older) loadedOlder.current = true
        if (!poll || !loadedOlder.current) setOlderMessages(data.next_before_id)
      } catch (err) {
        if (err.name !== 'AbortError' && version === historyVersion.current) {
          setHistoryError(err.message)
        }
        throw err
      } finally {
        if (version === historyVersion.current && !poll) setHistoryLoading(false)
      }
    },
    [user.id]
  )

  useEffect(() => {
    const controller = new AbortController()
    const version = ++historyVersion.current
    async function restore() {
      try {
        const data = await api('/chats', { signal: controller.signal })
        if (version !== historyVersion.current) return
        setConversations(data.items)
        setMoreConversations(data.next_before_id)
        const preference = rememberedChat(user.id)
        const latest = data.items[0]?.id
        const selected = preference === 'new' ? null : preference || latest
        if (selected) {
          try {
            await loadChat(selected, { signal: controller.signal })
          } catch (err) {
            if (err.status === 404) {
              if (latest && latest !== selected)
                await loadChat(latest, { signal: controller.signal })
              else {
                setConversationId('')
                rememberChat(user.id, '')
                setTurns([])
                setOlderMessages(null)
                setHistoryError('')
                setHistoryLoading(false)
              }
            } else throw err
          }
        } else {
          setHistoryError('')
          setHistoryLoading(false)
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setHistoryError(err.message)
          setHistoryLoading(false)
        }
      }
    }
    restore()
    return () => {
      controller.abort()
      historyVersion.current += 1
    }
  }, [user.id, historyRevision, loadChat])

  const hasPendingRequest = turns.some((turn) => turn.pending)
  useEffect(() => {
    if (!conversationId || busy || historyLoading || !hasPendingRequest) return
    const controller = new AbortController()
    let checking = false
    const timer = setInterval(async () => {
      if (checking) return
      checking = true
      try {
        await loadChat(conversationId, { poll: true, signal: controller.signal })
      } catch {
        /* error is shown by loadChat */
      } finally {
        checking = false
      }
    }, 3000)
    return () => {
      clearInterval(timer)
      controller.abort()
    }
  }, [conversationId, busy, historyLoading, hasPendingRequest, loadChat])

  async function selectSavedChat(id) {
    if (busy || historyLoading) return
    setDraft('')
    setAttachment(null)
    setAttachmentError('')
    loadedOlder.current = false
    if (!id) {
      historyVersion.current += 1
      setConversationId('')
      setTurns([])
      setOlderMessages(null)
      setHistoryError('')
      rememberChat(user.id, '')
      return
    }
    try {
      await loadChat(id)
    } catch {
      /* displayed in history banner */
    }
  }

  async function loadOlderChats() {
    if (!moreConversations || busy || historyLoading) return
    setHistoryLoading(true)
    try {
      const data = await api(`/chats?before_id=${encodeURIComponent(moreConversations)}`)
      setConversations((current) => [
        ...new Map([...current, ...data.items].map((item) => [item.id, item])).values(),
      ])
      setMoreConversations(data.next_before_id)
    } catch (err) {
      setHistoryError(err.message)
    } finally {
      setHistoryLoading(false)
    }
  }

  const previewUrl = attachment?.preview
  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl)
    }
  }, [previewUrl])

  function selectImage(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || inFlight.current) return
    setAttachmentError('')
    if (!file.size || file.size > 2 * 1024 * 1024) {
      setAttachmentError('Choose a nonempty image of at most 2 MB.')
      return
    }
    const allowedTypes = ['image/jpeg', 'image/png', 'image/webp', 'image/gif']
    if (file.type && !allowedTypes.includes(file.type)) {
      setAttachmentError('Choose a JPG, PNG, WebP or GIF image.')
      return
    }
    setAttachment({
      id: newId(),
      file,
      preview: URL.createObjectURL(file),
      image_id: null,
      upload_tool: '',
      uncertain: false,
    })
  }

  async function send(event) {
    event.preventDefault()
    const message = draft.trim()
    if (
      !message ||
      inFlight.current ||
      historyLoading ||
      historyError ||
      turns.some((turn) => turn.pending) ||
      attachment?.uncertain ||
      (attachment && !attachment.upload_tool)
    )
      return
    const selectedImage = attachment
    const id = newId()
    const started = performance.now()
    const uploadTrace = []
    let activeConversation = conversationId
    let phase = !activeConversation
      ? 'prepare'
      : selectedImage && !selectedImage.image_id
        ? 'upload'
        : 'chat'

    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setDraft('')
    setAttachmentError('')
    setTurns((previous) => [
      ...previous,
      {
        id,
        question: message,
        created_at: new Date().toISOString(),
        image_name: selectedImage?.file.name || '',
        image_id: selectedImage?.image_id || null,
        phase,
        reply: '',
        error: '',
        tool_calls: [],
        pending: true,
      },
    ])

    function finish(data) {
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === id
            ? {
                ...turn,
                reply: data.reply || '',
                error: data.error || '',
                tool_calls: [...uploadTrace, ...(data.tool_calls || [])],
                duration_ms: Math.round(performance.now() - started),
                retry_after_s: data.retry_after_s,
                retry_at: data.retry_at,
                error_provider: data.error_provider,
                limit_reason: data.limit_reason,
                model_requests: data.model_requests || [],
                pending: false,
              }
            : turn
        )
      )
    }

    try {
      if (!activeConversation) {
        activeConversation = newId()
        const saved = await api('/chats', {
          method: 'POST',
          body: { id: activeConversation, title: Array.from(message).slice(0, 80).join('') },
        })
        setConversationId(saved.id)
        rememberChat(user.id, saved.id)
        setConversations((current) => [saved, ...current.filter((item) => item.id !== saved.id)])
      }
      phase = selectedImage && !selectedImage.image_id ? 'upload' : 'chat'
      setTurns((previous) => previous.map((turn) => (turn.id === id ? { ...turn, phase } : turn)))
      let imageId = selectedImage?.image_id || null
      if (selectedImage && !imageId) {
        const uploaded = await api(
          `/media/upload?tool_name=${encodeURIComponent(selectedImage.upload_tool)}`,
          {
            method: 'POST',
            upload: selectedImage.file,
          }
        )
        if (
          uploaded.tool_name !== selectedImage.upload_tool ||
          !Number.isSafeInteger(uploaded.image_id) ||
          uploaded.image_id < 1
        ) {
          throw new Error(
            'Image upload returned an unexpected store or image ID. Check the Media Library.'
          )
        }
        imageId = uploaded.image_id
        setAttachment((current) =>
          current?.id === selectedImage.id ? { ...current, image_id: imageId } : current
        )
        uploadTrace.push({
          name: uploaded.tool_name,
          arguments: { filename: selectedImage.file.name },
          status: 'completed',
          result: {
            content: [{ type: 'text', text: JSON.stringify(uploaded) }],
            isError: false,
          },
        })
      }
      phase = 'chat'
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === id
            ? { ...turn, image_id: imageId, phase, tool_calls: [...uploadTrace] }
            : turn
        )
      )
      const data = await api('/chat', {
        method: 'POST',
        body: {
          message,
          conversation_id: activeConversation,
          request_id: id,
          ...(imageId
            ? {
                image_id: imageId,
                image_name: selectedImage.file.name,
                image_tool: selectedImage.upload_tool,
              }
            : {}),
        },
      })
      finish({
        ...data,
        error: data.error?.message || '',
        retry_after_s: data.error?.retry_after_s,
        retry_at: data.error?.retry_at,
        error_provider: data.error?.provider,
        limit_reason: data.error?.reason,
        results_complete: data.error?.results_complete === true,
      })
      if (!data.error) setAttachment(null)
    } catch (err) {
      if (phase === 'prepare') {
        setDraft(message)
        finish({ error: `Chat could not be started: ${err.message}` })
      } else if (phase === 'upload') {
        const uncertain = !err.status || err.status >= 500
        setAttachment((current) =>
          current?.id === selectedImage?.id ? { ...current, uncertain } : current
        )
        setDraft(message)
        finish({
          error: uncertain
            ? `${err.message} Ask your admin to check the Media Library before uploading again.`
            : err.message,
        })
      } else {
        if (err.data?.request_not_started === true) setDraft(message)
        finish({
          ...(err.data || {}),
          error: err.data
            ? err.data.error?.message || err.message
            : `${err.message} Tool execution could not be confirmed. Verify the outcome before repeating the request.`,
          retry_after_s: err.data?.error?.retry_after_s || err.retryAfterS,
          retry_at: err.data?.error?.retry_at,
          error_provider: err.data?.error?.provider,
          limit_reason: err.data?.error?.reason,
          results_complete: err.data?.error?.results_complete === true,
        })
      }
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  function newChat() {
    if (busy || historyLoading) return
    historyVersion.current += 1
    setConversationId('')
    rememberChat(user.id, '')
    setTurns([])
    setOlderMessages(null)
    setHistoryError('')
    loadedOlder.current = false
    setDraft('')
    setAttachment(null)
    setAttachmentError('')
  }
  async function deleteConversation(id, title) {
    if (!id || inFlight.current || historyLoading || hasPendingRequest) return
    const accepted = await confirmation.confirm({
      title: 'Delete this conversation?',
      description: `Remove “${title}” and its saved messages? Products, notes and audit records remain unchanged. This cannot be undone.`,
      label: 'Delete conversation',
    })
    if (!accepted || inFlight.current) return
    inFlight.current = true
    setBusy(true)
    onBusyChange(true)
    setChatNotice('')
    setDeleteError('')
    try {
      await api(`/chats/${id}`, { method: 'DELETE' })
      setConversations((current) => current.filter((item) => item.id !== id))
      if (id === conversationId) {
        historyVersion.current += 1
        setConversationId('')
        rememberChat(user.id, '')
        setTurns([])
        setOlderMessages(null)
        setHistoryError('')
        setDraft('')
        setAttachment(null)
        setAttachmentError('')
      }
      loadedOlder.current = false
      setChatNotice('Conversation deleted. Store data and audit records were kept.')
      // Refresh pagination anchors after deleting any listed conversation.
      try {
        const data = await api('/chats')
        setConversations(data.items)
        setMoreConversations(data.next_before_id)
      } catch {
        setMoreConversations(null)
        setChatNotice('Conversation deleted. Refresh the conversation list to load older chats.')
      }
    } catch (err) {
      setDeleteError(err.message)
    } finally {
      inFlight.current = false
      setBusy(false)
      onBusyChange(false)
    }
  }

  const selectedTitle =
    conversations.find((item) => item.id === conversationId)?.title || 'New conversation'
  const blocked = busy || historyLoading || hasPendingRequest
  const canSend =
    !blocked &&
    !historyError &&
    !hasPendingRequest &&
    draft.trim() &&
    !attachment?.uncertain &&
    (!attachment || attachment.upload_tool)

  return (
    <section className="flex h-[calc(100dvh-8rem)] min-h-[500px] min-w-0 overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-xs lg:h-[calc(100dvh-8.5rem)]">
      <aside
        aria-label="Saved conversations"
        className="hidden w-[220px] shrink-0 flex-col border-r border-neutral-200 bg-neutral-50/70 xl:flex"
      >
        <div className="border-b border-neutral-200 p-4">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-xs font-semibold text-neutral-600">Conversations</h2>
            <button
              type="button"
              disabled={blocked}
              aria-label="Refresh conversations"
              onClick={() => {
                loadedOlder.current = false
                setHistoryRevision((value) => value + 1)
              }}
              className="rounded-md p-1.5 text-neutral-500 hover:bg-neutral-200/60 hover:text-neutral-800 disabled:opacity-40"
            >
              <Icon name="refresh" className="h-3.5 w-3.5" />
            </button>
          </div>
          <button
            type="button"
            disabled={blocked}
            onClick={newChat}
            className="ui-button w-full justify-start"
          >
            <Icon name="plus" />
            New chat
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-3">
          {conversations.map((item) => (
            <div
              key={item.id}
              className={`group flex items-center rounded-lg border ${item.id === conversationId ? 'border-neutral-200 bg-white shadow-xs' : 'border-transparent hover:bg-neutral-200/40'}`}
            >
              <button
                type="button"
                disabled={blocked}
                onClick={() => selectSavedChat(item.id)}
                aria-current={item.id === conversationId ? 'true' : undefined}
                title={item.title}
                className="min-w-0 flex-1 p-3 text-left disabled:opacity-50"
              >
                <p className="line-clamp-2 text-xs font-medium leading-5 text-neutral-700">
                  {item.title}
                </p>
                <p className="mt-1.5 text-[10px] leading-4 text-neutral-500">
                  {dateText(item.created_at)}
                </p>
              </button>
              <button
                type="button"
                disabled={blocked}
                onClick={() => deleteConversation(item.id, item.title)}
                aria-label={`Delete conversation: ${item.title}`}
                className="mr-1 rounded-md p-2 text-neutral-400 hover:bg-neutral-200 hover:text-neutral-800 focus-visible:text-neutral-900 disabled:opacity-40"
              >
                <Icon name="trash" className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          {!conversations.length && !historyLoading && (
            <p className="px-2 py-5 text-xs leading-5 text-neutral-500">
              Your conversations will appear here after you send a message.
            </p>
          )}
          {moreConversations && (
            <button
              type="button"
              disabled={blocked}
              onClick={loadOlderChats}
              className="ui-button mt-3 w-full text-xs"
            >
              Load older chats
            </button>
          )}
        </div>
        <div className="flex items-start gap-2 border-t border-neutral-200 p-4 text-[10px] leading-5 text-neutral-500">
          <Icon name="shield" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <p>Chat history is saved to your account.</p>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-neutral-100 px-5 py-4 sm:px-7">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-neutral-200 bg-neutral-50">
              <Icon name="chat" className="h-4.5 w-4.5 text-neutral-600" />
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold tracking-tight" title={selectedTitle}>
                {selectedTitle}
              </h1>
              <p className="mt-1 text-[11px] text-neutral-500">
                Products, analytics, and team notes
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {conversationId && (
              <button
                type="button"
                disabled={blocked}
                onClick={() => deleteConversation(conversationId, selectedTitle)}
                aria-label="Delete current conversation"
                className="ui-button px-2.5"
              >
                <Icon name="trash" />
              </button>
            )}
            <button
              type="button"
              disabled={blocked}
              onClick={newChat}
              aria-label="New chat"
              className="ui-button xl:hidden"
            >
              <Icon name="plus" />
              <span className="hidden sm:inline">New chat</span>
            </button>
          </div>
        </header>
        <div className="border-b border-neutral-100 bg-neutral-50/60 px-5 py-3 xl:hidden">
          <div className="flex items-center gap-2">
            <label className="sr-only" htmlFor="saved-chats">
              Saved chats
            </label>
            <select
              id="saved-chats"
              value={conversationId}
              disabled={blocked}
              onChange={(event) => selectSavedChat(event.target.value)}
              className="ui-input py-2 text-xs"
            >
              <option value="">New conversation</option>
              {conversations.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.title}
                </option>
              ))}
              {conversationId && !conversations.some((item) => item.id === conversationId) && (
                <option value={conversationId}>Current saved chat</option>
              )}
            </select>
            <button
              type="button"
              disabled={blocked}
              aria-label="Refresh saved chats"
              onClick={() => {
                loadedOlder.current = false
                setHistoryRevision((value) => value + 1)
              }}
              className="ui-button px-2.5"
            >
              <Icon name="refresh" />
            </button>
          </div>
          {moreConversations && (
            <button
              type="button"
              disabled={blocked}
              onClick={loadOlderChats}
              className="mt-2 text-xs text-neutral-500 underline"
            >
              Load older chats
            </button>
          )}
        </div>
        {chatNotice && (
          <div role="status" className="ui-banner mx-5 mt-4">
            {chatNotice}
          </div>
        )}
        {deleteError && (
          <div role="alert" className="ui-banner mx-5 mt-4">
            {deleteError}
          </div>
        )}
        {historyError && (
          <div role="alert" className="ui-banner mx-5 mt-4">
            {historyError} Refresh conversations after checking your connection.
          </div>
        )}
        <div ref={transcriptRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
          {historyLoading && !turns.length && (
            <div
              role="status"
              className="flex items-center justify-center gap-3 py-20 text-sm text-neutral-500"
            >
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-neutral-200 border-t-neutral-600" />
              Loading your conversations…
            </div>
          )}
          {!turns.length && !historyLoading && (
            <div className="mx-auto flex min-h-full max-w-xl flex-col items-center justify-center py-2 text-center">
              <h2 className="text-2xl font-semibold tracking-tight sm:text-[28px]">
                Start a conversation
              </h2>
              <p className="mt-3 max-w-sm text-sm leading-6 text-neutral-500">
                Choose a store and describe what you need.
              </p>
              <div className="mt-8 grid w-full gap-2.5 sm:grid-cols-3">
                {examples.map((example) => (
                  <button
                    key={example}
                    type="button"
                    disabled={blocked}
                    onClick={() => setDraft(example)}
                    className="group rounded-xl border border-neutral-200 bg-white p-4 text-left transition-colors hover:border-neutral-400 hover:bg-neutral-50"
                  >
                    <span className="text-xs leading-5 text-neutral-600">{example}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
          {olderMessages && (
            <div className="mb-6 text-center">
              <button
                type="button"
                disabled={blocked}
                onClick={() => {
                  loadChat(conversationId, { older: true, before: olderMessages }).catch(() => {})
                }}
                className="ui-button text-xs"
              >
                <Icon name="clock" className="h-3.5 w-3.5" />
                Load earlier messages
              </button>
            </div>
          )}
          <div className="mx-auto max-w-3xl space-y-8">
            {turns.map((turn) => (
              <article key={turn.id} className="space-y-5">
                <div className="ml-auto max-w-[90%] sm:max-w-[80%]">
                  <div className="rounded-2xl rounded-tr-md border border-neutral-200/70 bg-neutral-100 px-4 py-3.5 text-sm leading-6 whitespace-pre-wrap">
                    {turn.question}
                    {turn.image_name && (
                      <p className="mt-3 flex items-center gap-2 break-all border-t border-neutral-200 pt-2 text-xs text-neutral-500">
                        <Icon name="clip" className="h-3.5 w-3.5 shrink-0" />
                        {turn.image_name}
                      </p>
                    )}
                  </div>
                  {turn.created_at && (
                    <p className="mt-2 text-right text-[10px] text-neutral-500">
                      {dateText(turn.created_at)} · IST
                    </p>
                  )}
                </div>
                <div className="flex gap-3 sm:gap-4">
                  <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-neutral-900 text-[11px] font-semibold text-white">
                    U
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="mb-2.5 flex items-center gap-2">
                      <p className="text-xs font-semibold text-neutral-800">Urumi</p>
                      {typeof turn.duration_ms === 'number' && (
                        <span className="text-[10px] text-neutral-500">
                          · {(turn.duration_ms / 1000).toFixed(1)}s
                        </span>
                      )}
                    </div>
                    {turn.pending && (
                      <p
                        role="status"
                        className="flex items-center gap-2.5 py-2 text-sm text-neutral-500"
                      >
                        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-200 border-t-neutral-600" />
                        {turn.phase === 'prepare'
                          ? 'Preparing your conversation…'
                          : turn.phase === 'upload'
                            ? 'Uploading your image…'
                            : 'Working on your request…'}
                      </p>
                    )}
                    {turn.reply && (
                      <div className={markdownClasses}>
                        <Markdown remarkPlugins={[remarkGfm]} skipHtml>
                          {turn.reply}
                        </Markdown>
                      </div>
                    )}
                    <ChatRunStatus turn={turn} />
                    <ToolActivity calls={turn.tool_calls} />
                  </div>
                </div>
              </article>
            ))}
          </div>
          <div ref={endRef} />
        </div>
        <form
          onSubmit={send}
          className="border-t border-neutral-100 bg-white px-5 pb-4 pt-4 sm:px-7"
        >
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            onChange={selectImage}
            disabled={blocked}
            aria-label="Choose a product image"
            className="hidden"
          />
          {attachment && (
            <div className="mb-3 flex items-center gap-3 rounded-xl border border-neutral-200 bg-neutral-50 p-3">
              <img
                src={attachment.preview}
                alt="Selected product image"
                className="h-12 w-12 rounded-lg object-cover"
              />
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium">{attachment.file.name}</p>
                <p className="mt-1 text-[10px] leading-5 text-neutral-500">
                  {attachment.uncertain
                    ? 'Upload unconfirmed. Check before uploading again.'
                    : attachment.image_id
                      ? 'Uploaded and ready to use.'
                      : 'This image uploads when you send.'}
                </p>
              </div>
              <button
                type="button"
                disabled={blocked}
                aria-label="Remove attachment"
                onClick={() => {
                  setAttachment(null)
                  setAttachmentError('')
                }}
                className="rounded-lg p-2 text-neutral-500 hover:bg-neutral-200"
              >
                <Icon name="close" />
              </button>
            </div>
          )}
          {attachment && (
            <ImageDestination
              key={attachment.id}
              id={attachment.id}
              value={attachment.upload_tool}
              disabled={blocked || Boolean(attachment.image_id) || attachment.uncertain}
              onSelect={selectDestination}
            />
          )}
          {attachmentError && (
            <p role="alert" className="ui-banner mb-3">
              {attachmentError}
            </p>
          )}
          <div className="rounded-xl border border-neutral-300 bg-white p-3 shadow-xs transition-colors focus-within:border-neutral-500">
            <label htmlFor="chat-message" className="sr-only">
              Your message
            </label>
            <textarea
              id="chat-message"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
                  event.preventDefault()
                  if (canSend) event.currentTarget.form.requestSubmit()
                }
              }}
              maxLength={4000}
              rows={2}
              disabled={blocked}
              placeholder="Type a message about a product, order or note"
              className="block w-full resize-y border-0 bg-transparent px-1 py-1 text-sm leading-6 outline-none placeholder:text-neutral-500 focus-visible:outline-none disabled:opacity-60"
            />
            <div className="mt-3 flex items-center justify-between gap-3">
              <button
                type="button"
                disabled={blocked}
                onClick={() => fileInput.current?.click()}
                className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs font-medium text-neutral-500 hover:bg-neutral-100 hover:text-neutral-800 disabled:opacity-50"
              >
                <Icon name="clip" />
                Attach image
              </button>
              <div className="flex items-center gap-3">
                <span className="text-[10px] tabular-nums text-neutral-500">
                  {draft.length}/4000
                </span>
                <button
                  type="submit"
                  disabled={!canSend}
                  className="ui-primary min-h-9 px-3 py-1.5"
                  aria-label="Send message"
                >
                  <span className="hidden sm:inline">{busy ? 'Working…' : 'Send'}</span>
                  <Icon name="arrow" className="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
          <div className="mt-2.5 flex flex-wrap items-center justify-between gap-1 text-[10px] leading-5 text-neutral-500">
            <span>JPG, PNG, WebP or GIF · Up to 2 MB</span>
            <span>Ctrl + Enter to send</span>
          </div>
        </form>
      </div>
      <ConfirmDialog request={confirmation.request} onFinish={confirmation.finish} />
    </section>
  )
}
