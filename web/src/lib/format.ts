export type TempUnit = 'C' | 'F'

export function toUnit(valueC: number, unit: TempUnit): number {
  return unit === 'F' ? valueC * 1.8 + 32 : valueC
}

export function formatTemp(valueC: number, unit: TempUnit = 'C'): string {
  return `${toUnit(valueC, unit).toFixed(1)} °${unit}`
}

// Popup bodies are assembled as HTML strings; place names and QC notes come
// from the database and must never be able to close a tag.
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export function formatUtcDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

export function formatUtcDateTime(iso: string): string {
  const d = new Date(iso)
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
  const time = d.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'UTC',
  })
  return `${date}, ${time} UTC`
}

export const MEASUREMENT_LABEL: Record<'air' | 'land_surface', string> = {
  air: 'air',
  land_surface: 'surface',
}

// The chip must not let a range or an average read as an absolute record.
export function recordChipLabel(record: {
  measurement_type: 'air' | 'land_surface'
  record_kind: 'all_time_max' | 'avg_annual' | 'diurnal_range'
}): string {
  const base = MEASUREMENT_LABEL[record.measurement_type]
  if (record.record_kind === 'diurnal_range') return `${base} swing`
  if (record.record_kind === 'avg_annual') return `${base} avg`
  return base
}

export const MEASUREMENT_EXPLAINER: Record<'air' | 'land_surface', string> = {
  air: 'Air temperature, measured by a weather station about 1.5 m above ground.',
  land_surface: 'Land-surface temperature, measured by satellite: how hot the ground itself gets.',
}

export function formatCoords(lat: number, lon: number): string {
  const ns = lat >= 0 ? 'N' : 'S'
  const ew = lon >= 0 ? 'E' : 'W'
  return `${Math.abs(lat).toFixed(1)}\u00b0${ns} ${Math.abs(lon).toFixed(1)}\u00b0${ew}`
}

export function formatMonthYear(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-US', {
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

export const ANOMALY_CAUSE_LABEL: Record<string, string> = {
  volcanic: 'Volcanic',
  wildfire: 'Wildfire',
  'failed corroboration': 'Failed cross-check',
  uncorroborated: 'Unverified',
}

// Empirical station-air-temperature estimate from surface temperature:
// Tair_max = 0.6443 x LSTmax + 3.1876 (R^2 0.82), the barren-land relation in
// Mildrexler, Zhao & Running (2011), J. Geophys. Res. 116, G03025, fig. 4a.
// The fit's data ends near 62 C surface; hotter readings are extrapolations.
export const AIR_ESTIMATE_FIT_LIMIT_C = 62

export function estimateAirTempC(surfaceC: number): number {
  return 0.6443 * surfaceC + 3.1876
}

export function formatAirEstimate(surfaceC: number, unit: TempUnit = 'C'): string {
  return `air \u2248 ${toUnit(estimateAirTempC(surfaceC), unit).toFixed(0)} \u00b0${unit}`
}

export const AIR_ESTIMATE_NOTE =
  'Air temperatures are estimates derived from the surface reading ' +
  '(Mildrexler, Zhao & Running 2011, barren-land relation; approximate, and ' +
  'extrapolated beyond the studied range above 62 \u00b0C surface).'
