import { useEffect, useState } from 'react'
import { supabase } from './supabase'
import type {
  AlltimeManifest,
  AnomalyReading,
  IngestRun,
  LstReading,
  RecordHolder,
  TileManifest,
  VolcanicSource,
} from './types'

interface KilnData {
  records: RecordHolder[]
  readings: LstReading[]
  latestRun: IngestRun | null
  manifest: TileManifest | null
  alltimeReadings: LstReading[]
  alltimeManifest: AlltimeManifest | null
  anomalies: AnomalyReading[]
  volcanoes: Map<string, VolcanicSource>
  loading: boolean
  error: string | null
}

const READINGS_PAGE = 1000
const READINGS_MAX_PAGES = 12

const STORAGE_BASE = `${import.meta.env.VITE_SUPABASE_URL}/storage/v1/object/public/kiln-tiles`
const MANIFEST_URL = `${STORAGE_BASE}/manifest.json`
const ALLTIME_MANIFEST_URL = `${STORAGE_BASE}/manifest-alltime.json`

// The raster pyramid ships with a manifest; absence just means the raster
// pipeline has not run yet and the map falls back to the 1-degree fills.
async function fetchManifest(): Promise<TileManifest | null> {
  try {
    const res = await fetch(`${MANIFEST_URL}?t=${Date.now()}`)
    if (!res.ok) return null
    const body = (await res.json()) as TileManifest
    if (!body.date || !body.tile_url_template) return null
    return body
  } catch {
    return null
  }
}

async function fetchAlltimeManifest(): Promise<AlltimeManifest | null> {
  try {
    const res = await fetch(`${ALLTIME_MANIFEST_URL}?t=${Date.now()}`)
    if (!res.ok) return null
    const body = (await res.json()) as AlltimeManifest
    if (!body.since || !body.tile_url_template) return null
    return body
  } catch {
    return null
  }
}

// The all-time table holds one row per tile ever, already deduped and screened.
async function fetchAlltimeReadings(): Promise<LstReading[]> {
  const all: LstReading[] = []
  for (let page = 0; page < READINGS_MAX_PAGES; page++) {
    const from = page * READINGS_PAGE
    const { data, error } = await supabase
      .from('alltime_readings')
      .select(
        'record_date, satellite, product, tile_lat, tile_lon, max_c, max_lat, max_lon, observed_at, qc_note, place_name, country',
      )
      .order('max_c', { ascending: false })
      .range(from, from + READINGS_PAGE - 1)
    if (error) return all
    const rows = ((data ?? []) as Array<LstReading & { record_date: string }>).map((r) => ({
      ...r,
      reading_date: r.record_date,
    }))
    all.push(...rows)
    if (rows.length < READINGS_PAGE) break
  }
  return all
}

async function fetchAnomalies(): Promise<AnomalyReading[]> {
  const { data, error } = await supabase
    .from('anomaly_readings')
    .select('reading_date, satellite, max_c, max_lat, max_lon, observed_at, cause, source_slug, place_name, country')
    .order('max_c', { ascending: false })
    .limit(50)
  if (error) return []
  return (data as AnomalyReading[]) ?? []
}

async function fetchVolcanoes(): Promise<Map<string, VolcanicSource>> {
  const { data, error } = await supabase
    .from('volcanic_sources')
    .select('slug, name, country, source_name, source_url')
  if (error) return new Map()
  return new Map(((data as VolcanicSource[]) ?? []).map((v) => [v.slug, v]))
}

// PostgREST caps every response at 1000 rows; a hot day writes several
// thousand. Page through explicitly so the map never silently truncates.
async function fetchAllReadings(readingDate: string): Promise<LstReading[]> {
  const all: LstReading[] = []
  for (let page = 0; page < READINGS_MAX_PAGES; page++) {
    const from = page * READINGS_PAGE
    const { data, error } = await supabase
      .from('lst_readings')
      .select(
        'reading_date, satellite, product, tile_lat, tile_lon, max_c, max_lat, max_lon, observed_at, qc_note, place_name, country',
      )
      .eq('reading_date', readingDate)
      .order('max_c', { ascending: false })
      .range(from, from + READINGS_PAGE - 1)
    if (error) throw new Error(error.message)
    const rows = (data as LstReading[]) ?? []
    all.push(...rows)
    if (rows.length < READINGS_PAGE) break
  }
  return all
}

// One read per page load. The data changes once a day; live refetching
// would be motion without information.
export function useKilnData(): KilnData {
  const [data, setData] = useState<KilnData>({
    records: [],
    readings: [],
    latestRun: null,
    manifest: null,
    alltimeReadings: [],
    alltimeManifest: null,
    anomalies: [],
    volcanoes: new Map(),
    loading: true,
    error: null,
  })

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const [recordsRes, runRes, manifest, alltimeManifest, alltimeReadings, anomalies, volcanoes] =
          await Promise.all([
            supabase.from('record_holders').select('*').order('value_c', { ascending: false }),
            supabase
              .from('ingest_runs')
              .select('reading_date, product, started_at, finished_at, status')
              .eq('status', 'succeeded')
              // By reading_date, not started_at: historical backfill runs execute
              // now but describe old days, and must not win "most recent".
              .order('reading_date', { ascending: false })
              .order('started_at', { ascending: false })
              .limit(1),
            fetchManifest(),
            fetchAlltimeManifest(),
            fetchAlltimeReadings(),
            fetchAnomalies(),
            fetchVolcanoes(),
          ])
        if (recordsRes.error) throw new Error(recordsRes.error.message)
        if (runRes.error) throw new Error(runRes.error.message)

        const latestRun = (runRes.data?.[0] as IngestRun | undefined) ?? null

        let readings: LstReading[] = []
        if (latestRun) {
          const rows = await fetchAllReadings(latestRun.reading_date)
          // Terra and Aqua each write their own row per tile; keep only the
          // hottest per tile. Rows arrive hottest-first, so first wins.
          const byTile = new Map<string, LstReading>()
          for (const reading of rows) {
            const key = `${reading.tile_lat},${reading.tile_lon}`
            if (!byTile.has(key)) byTile.set(key, reading)
          }
          readings = [...byTile.values()]
        }

        if (!cancelled) {
          setData({
            records: (recordsRes.data as RecordHolder[]) ?? [],
            readings,
            latestRun,
            manifest,
            alltimeReadings,
            alltimeManifest,
            anomalies,
            volcanoes,
            loading: false,
            error: null,
          })
        }
      } catch (err) {
        if (!cancelled) {
          setData((prev) => ({
            ...prev,
            loading: false,
            error: err instanceof Error ? err.message : 'Could not load data',
          }))
        }
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [])

  return data
}
