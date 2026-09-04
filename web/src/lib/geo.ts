// Coarse client-side geography for leaderboard filtering. These are filter
// buckets, not cartography: boundaries are deliberately rough boxes, documented
// as such, and every reading we rank sits far from the ambiguous edges.

export type Area =
  | 'everywhere'
  | 'view'
  | 'north'
  | 'south'
  | 'africa'
  | 'asia'
  | 'europe'
  | 'north-america'
  | 'south-america'
  | 'oceania'

export const AREA_LABEL: Record<Area, string> = {
  everywhere: 'Everywhere',
  view: 'This map area',
  north: 'Northern Hemisphere',
  south: 'Southern Hemisphere',
  africa: 'Africa',
  asia: 'Asia',
  europe: 'Europe',
  'north-america': 'North America',
  'south-america': 'South America',
  oceania: 'Australia & Oceania',
}

export function continentOf(lat: number, lon: number): Exclude<Area, 'everywhere' | 'view' | 'north' | 'south'> | null {
  if (lon >= 110 && lon <= 180 && lat >= -50 && lat <= -8) return 'oceania'
  if (lon >= -170 && lon <= -50 && lat >= 12 && lat <= 75) return 'north-america'
  if (lon >= -120 && lon <= -50 && lat >= 7 && lat < 12) return 'north-america'
  if (lon >= -82 && lon <= -34 && lat >= -56 && lat < 12) return 'south-america'
  if (lat >= -35 && lat <= 37 && lon >= -18 && lon < 34) return 'africa'
  if (lat >= -35 && lat < 30 && lon >= 34 && lon <= 52) return 'africa'
  if (lat >= 40 && lon >= -10 && lon < 30) return 'europe'
  if (lon >= 25 && lat > -10) return 'asia'
  if (lon >= 90 && lon <= 180 && lat > -8 && lat <= 10) return 'asia'
  return null
}

export function inArea(
  lat: number,
  lon: number,
  area: Area,
  viewBounds: { west: number; south: number; east: number; north: number } | null,
): boolean {
  switch (area) {
    case 'everywhere':
      return true
    case 'view':
      if (!viewBounds) return true
      return (
        lat >= viewBounds.south &&
        lat <= viewBounds.north &&
        (viewBounds.west <= viewBounds.east
          ? lon >= viewBounds.west && lon <= viewBounds.east
          : lon >= viewBounds.west || lon <= viewBounds.east)
      )
    case 'north':
      return lat >= 0
    case 'south':
      return lat < 0
    default:
      return continentOf(lat, lon) === area
  }
}

export type Period = 'all' | '2020s' | '2010s' | '2000s' | 'year'

export const PERIOD_LABEL: Record<Period, string> = {
  all: 'All time',
  '2020s': 'Set in the 2020s',
  '2010s': 'Set in the 2010s',
  '2000s': 'Set in the 2000s',
  year: 'Set in the past year',
}

export function inPeriod(readingDate: string, period: Period): boolean {
  if (period === 'all') return true
  const year = Number(readingDate.slice(0, 4))
  if (period === '2020s') return year >= 2020 && year < 2030
  if (period === '2010s') return year >= 2010 && year < 2020
  if (period === '2000s') return year >= 2000 && year < 2010
  const cutoff = new Date()
  cutoff.setUTCFullYear(cutoff.getUTCFullYear() - 1)
  return new Date(`${readingDate}T00:00:00Z`) >= cutoff
}

export function matchesQuery(query: string, ...fields: Array<string | null | undefined>): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return fields.some((f) => (f ?? '').toLowerCase().includes(q))
}
