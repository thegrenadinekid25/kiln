-- Anomalies (decision 2026-09-02): volcanoes, wildfires, and other non-weather
-- heat get their own section instead of contaminating the weather archive or
-- being silently discarded. One row per notable non-weather reading.

create table kiln.anomaly_readings (
  id bigint generated always as identity primary key,
  reading_date date not null,
  satellite text not null,
  product text not null,
  tile_lat smallint not null check (tile_lat between -90 and 89),
  tile_lon smallint not null check (tile_lon between -180 and 179),
  max_c numeric(5, 2) not null,
  max_lat double precision not null,
  max_lon double precision not null,
  observed_at timestamptz not null,
  granule_id text,
  -- Why this is not weather. Worded, shown verbatim in the UI.
  cause text not null check (cause in ('volcanic', 'wildfire', 'failed corroboration', 'uncorroborated')),
  -- For volcanic rows: which vent, so the UI can name and cite it.
  source_slug text,
  qc_note text,
  created_at timestamptz not null default now(),
  unique (reading_date, product, tile_lat, tile_lon, cause)
);

create index anomaly_readings_temp_idx on kiln.anomaly_readings (max_c desc);

-- Curated list of persistently hot volcanic sources, mirrored from the
-- pipeline's bundled list. Cited like record_holders.
create table kiln.volcanic_sources (
  slug text primary key,
  name text not null,
  country text not null,
  lat double precision not null,
  lon double precision not null,
  -- Pixels within this distance of the vent are classified volcanic.
  radius_km numeric(4, 1) not null default 7.0,
  source_name text not null,
  source_url text not null,
  notes text,
  updated_at timestamptz not null default now()
);

alter table kiln.anomaly_readings enable row level security;
alter table kiln.volcanic_sources enable row level security;

create policy "public read" on kiln.anomaly_readings
  for select to anon, authenticated using (true);
create policy "public read" on kiln.volcanic_sources
  for select to anon, authenticated using (true);

grant select on kiln.anomaly_readings, kiln.volcanic_sources to anon, authenticated;
grant all on kiln.anomaly_readings, kiln.volcanic_sources to service_role;
grant usage, select on all sequences in schema kiln to service_role;
