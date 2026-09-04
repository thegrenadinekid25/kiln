// Public storage bucket holding the raster pyramids and their manifests.
// The daily pyramid is keyed by reading date: `{date}/{z}/{x}/{y}.png`.
export const STORAGE_BASE = `${import.meta.env.VITE_SUPABASE_URL}/storage/v1/object/public/kiln-tiles`
export const TILES_BASE = `${STORAGE_BASE}/`

export function dailyTileUrl(date: string): string {
  return `${TILES_BASE}${date}/{z}/{x}/{y}.png`
}

// A day's pyramid may have been pruned even though its readings remain in the
// table. Zoom 0 is a single world tile, so its presence answers "is there a
// raster for this date" in one request.
export async function hasDailyRaster(date: string): Promise<boolean> {
  try {
    const res = await fetch(`${TILES_BASE}${date}/0/0/0.png`, { method: 'HEAD' })
    return res.ok
  } catch {
    return false
  }
}
