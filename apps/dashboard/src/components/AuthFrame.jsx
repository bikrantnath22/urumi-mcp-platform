export default function AuthFrame({ children }) {
  return (
    <main className="flex min-h-dvh flex-col bg-neutral-50 px-6 py-8 text-neutral-900">
      <a href="/" className="mx-auto flex items-center gap-2.5 text-lg font-semibold">
        <span className="grid h-9 w-9 place-items-center rounded border border-neutral-900 bg-neutral-900 text-white">
          U
        </span>
        Urumi
      </a>
      <div className="mx-auto my-auto w-full max-w-[420px] py-10">
        <section className="rounded-lg border border-neutral-200 bg-white p-6 sm:p-8">
          {children}
        </section>
      </div>
      <p className="text-center text-xs text-neutral-500">Stores, orders and team access</p>
    </main>
  )
}
