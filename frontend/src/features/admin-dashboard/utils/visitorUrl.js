const configuredBase = import.meta.env.VITE_VISITOR_BASE_URL || '';

export function visitorPoiUrl(poiId) {
  if (!configuredBase || !poiId) return null;
  try {
    const base = new URL(configuredBase);
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password) return null;
    return new URL(`/poi/${encodeURIComponent(String(poiId))}`, base.origin).toString();
  } catch {
    return null;
  }
}
