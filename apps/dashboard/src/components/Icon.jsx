const paths = {
  trash: ['M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'],
  chat: [
    'M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9H13a8.48 8.48 0 0 1 8 8v.5Z',
  ],
  key: ['m21 2-2 2m-7.6 7.6a6 6 0 1 1-8.5 8.5 6 6 0 0 1 8.5-8.5Zm0 0L19 4m0 0 3 3-3 3-3-3'],
  audit: [
    'M8 3H6a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-2',
    'M9 2h6v4H9z',
    'M8 11h8M8 15h8M8 19h5',
  ],
  server: ['M4 3h16v7H4zM4 14h16v7H4zM8 6.5h.01M8 17.5h.01M12 6.5h5M12 17.5h5'],
  users: [
    'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87',
    'M13 3.13a4 4 0 0 1 0 7.75',
    'M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  ],
  logout: ['M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9'],
  arrow: ['M12 19V5m-7 7 7-7 7 7'],
  plus: ['M12 5v14M5 12h14'],
  clip: [
    'm21 11.5-9.2 9.2a6 6 0 0 1-8.5-8.5l10-10a4 4 0 0 1 5.7 5.7l-10 10a2 2 0 0 1-2.8-2.8l9.2-9.2',
  ],
  check: ['m5 12 4 4L19 6'],
  close: ['m6 6 12 12M6 18 18 6'],
  chevron: ['m9 5 7 7-7 7'],
  refresh: [
    'M20 7v5h-5M4 17v-5h5',
    'M6.1 7a7 7 0 0 1 11.55-2.6L20 7M4 17l2.35 2.6A7 7 0 0 0 17.9 17',
  ],
  shield: ['M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z', 'm9 12 2 2 4-4'],
  eye: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z', 'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z'],
  hide: [
    'm3 3 18 18M10.6 5.1A12 12 0 0 1 12 5c6.5 0 10 7 10 7a16 16 0 0 1-3 4M6.5 6.5A18 18 0 0 0 2 12s3.5 7 10 7a12 12 0 0 0 5.5-1.5',
  ],
  note: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z', 'M14 2v6h6M8 13h8M8 17h6'],
  chart: ['M3 3v18h18M7 16v-4M12 16V8M17 16V5'],
  box: ['m21 8-9 5-9-5M12 22V13', 'm3 8 9-5 9 5v10l-9 5-9-5V8Z'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  clock: ['M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z', 'M12 6v6l4 2'],
}

export default function Icon({ name, className = 'h-4 w-4', ...props }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      {...props}
    >
      {(paths[name] || paths.chat).map((path, index) => (
        <path key={index} d={path} />
      ))}
    </svg>
  )
}
