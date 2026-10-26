import { useEffect, useId, useRef } from 'react'
import Icon from './Icon.jsx'

export default function ConfirmDialog({ request, onFinish }) {
  const dialog = useRef(null)
  const titleId = useId()
  const descriptionId = useId()
  useEffect(() => {
    if (request && !dialog.current.open) dialog.current.showModal()
    if (!request && dialog.current.open) dialog.current.close()
  }, [request])
  return (
    <dialog
      ref={dialog}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault()
        onFinish(false)
      }}
      className="m-auto w-[calc(100%_-_2rem)] max-w-md rounded-2xl border border-neutral-200 bg-white p-0 text-neutral-900 shadow-2xl backdrop:bg-neutral-950/45"
    >
      {request && (
        <div className="p-6 sm:p-7">
          <div className="mb-5 grid h-11 w-11 place-items-center rounded-xl border border-neutral-200 bg-neutral-50">
            <Icon name="shield" className="h-5 w-5" />
          </div>
          <h2 id={titleId} className="text-lg font-semibold tracking-tight">
            {request.title}
          </h2>
          <p id={descriptionId} className="mt-3 break-words text-sm leading-6 text-neutral-600">
            {request.description}
          </p>
          <div className="mt-7 flex justify-end gap-3">
            <button autoFocus type="button" onClick={() => onFinish(false)} className="ui-button">
              Cancel
            </button>
            <button type="button" onClick={() => onFinish(true)} className="ui-primary">
              {request.label || 'Confirm'}
            </button>
          </div>
        </div>
      )}
    </dialog>
  )
}
