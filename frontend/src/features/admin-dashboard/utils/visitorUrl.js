const configuredBase =
  (import.meta.env.VITE_VISITOR_BASE_URL || '').trim() ||
  'https://seminarmobile.vercel.app';

export function visitorPoiUrl(poiId) {
  if (!configuredBase || !poiId) return null;
  try {
    const base = new URL(configuredBase);
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password) return null;
    const url = new URL(`/poi/${encodeURIComponent(String(poiId))}`, base.origin);
    url.searchParams.set('source', 'qr');
    return url.toString();
  } catch {
    return null;
  }
}
