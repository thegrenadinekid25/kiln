export type MeasurementType = 'air' | 'land_surface'

export interface RecordHolder {
  slug: string
  title: string
  place_name: string
  lat: number
  lon: number
  measurement_type: MeasurementType
  record_kind: 'all_time_max' | 'avg_annual' | 'diurnal_range'
  value_c: number
  observed_on: string | null
  period: string | null
  method: string
  source_name: string
  source_url: string
  notes: string | null
}

export interface LstReading {
  place_name?: string | null
  country?: string | null
  reading_date: string
  satellite: string
  product: string
  tile_lat: number
  tile_lon: number
  max_c: number
  max_lat: number
  max_lon: number
  observed_at: string
  qc_note: string | null
}

export interface TileManifest {
  date: string
  generated_at: string
  min_zoom: number
  max_zoom: number
  tile_url_template: string
  tile_count: number
}

export interface AlltimeManifest {
  since: string
  through: string
  generated_at: string
  min_zoom: number
  max_zoom: number
  tile_url_template: string
  tile_count: number
}

export interface IngestRun {
  reading_date: string
  product: string
  started_at: string
  finished_at: string | null
  status: 'running' | 'succeeded' | 'partial' | 'failed'
}

export interface AnomalyReading {
  place_name?: string | null
  country?: string | null
  reading_date: string
  satellite: string
  max_c: number
  max_lat: number
  max_lon: number
  observed_at: string
  cause: 'volcanic' | 'wildfire' | 'failed corroboration' | 'uncorroborated'
  source_slug: string | null
}

export interface VolcanicSource {
  slug: string
  name: string
  country: string
  source_name: string
  source_url: string
}
