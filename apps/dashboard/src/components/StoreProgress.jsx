export function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent motion-reduce:animate-none"
    />
  )
}

export default function StoreProgress({ status }) {
  if (!['Provisioning', 'Deleting'].includes(status)) return null
  return (
    <div
      role="status"
      className="mt-4 flex items-start gap-3 rounded-xl border border-neutral-200 bg-neutral-50 p-4 text-sm"
    >
      <span className="mt-0.5">
        <Spinner />
      </span>
      <div>
        <p className="font-medium">
          {status === 'Provisioning' ? 'Creating store…' : 'Deleting store…'}
        </p>
        <p className="mt-1 text-xs leading-5 text-neutral-500">
          {status === 'Provisioning'
            ? 'This can take a few minutes. Progress updates automatically; you can leave and return.'
            : 'Removing the store and its saved data. Progress updates automatically.'}
        </p>
      </div>
    </div>
  )
}
