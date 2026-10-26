function label(value) {
  const words = value.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export function permissionGroups(catalog, grants, query = '', access = 'all') {
  const granted = new Set(grants.map((item) => item.name))
  const rows = new Map()
  for (const tool of catalog) {
    rows.set(tool.name, { ...tool, available: true })
  }
  for (const tool of grants) {
    if (!rows.has(tool.name)) {
      rows.set(tool.name, {
        name: tool.name,
        available: false,
        description: 'Server disabled or tool unavailable. You can still revoke this grant.',
      })
    }
  }
  const groups = new Map()
  for (const tool of rows.values()) {
    const split = tool.name.indexOf('__')
    if (split < 1) continue
    const slug = tool.name.slice(0, split)
    const shortName = tool.name.slice(split + 2)
    const description = tool.description || ''
    const heading = /^\[([^\]]+)\]\s*/.exec(description)
    if (!groups.has(slug)) {
      groups.set(slug, {
        slug,
        title: heading?.[1] || label(slug),
        tools: [],
        total: 0,
        granted: 0,
        available: 0,
      })
    }
    const group = groups.get(slug)
    group.total += 1
    if (granted.has(tool.name)) group.granted += 1
    if (tool.available) group.available += 1
    group.tools.push({
      ...tool,
      shortName,
      label: label(shortName),
      description: heading ? description.slice(heading[0].length) : description,
      granted: granted.has(tool.name),
    })
  }
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return [...groups.values()]
    .map((group) => ({
      ...group,
      tools: group.tools
        .filter((tool) => {
          if (access === 'granted' && !tool.granted) return false
          if (access === 'ungranted' && tool.granted) return false
          if (access === 'unavailable' && tool.available) return false
          const text = [group.title, tool.name, tool.label, tool.description]
            .join(' ')
            .toLowerCase()
          return terms.every((term) => text.includes(term))
        })
        .sort((a, b) => a.shortName.localeCompare(b.shortName)),
    }))
    .filter((group) => group.tools.length)
    .sort((a, b) => a.title.localeCompare(b.title) || a.slug.localeCompare(b.slug))
}
