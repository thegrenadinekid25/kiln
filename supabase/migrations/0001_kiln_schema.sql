-- Kiln: public read-only temperature map.
-- Lives in the shared tortoise Supabase project as its own schema (schema-per-app pattern).
-- Public surface is SELECT-only; the ingestion pipeline writes with service_role.

create schema if not exists kiln;

-- Curated all-time records (historical-records spec). Hand-maintained, cited.
create table kiln.record_holders (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  title text not null,
  place_name text not null,
  lat double precision not null,
  lon double precision not null,
  -- Air temperature and land-surface temperature are different measurements;
  -- the product's whole point is keeping them distinct.
  measurement_type text not null check (measurement_type in ('air', 'land_surface')),
  record_kind text not null check (record_kind in ('all_time_max', 'avg_annual', 'diurnal_range')),
  value_c numeric(5, 2) not null,
  observed_on date,
  period text,
  method text not null,
  source_name text not null,
  source_url text not null,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Daily max land-surface temperature per 1-degree tile (live-ingestion spec).
-- Only hot tiles above the pipeline's threshold are stored, plus the global top-N.
create table kiln.lst_readings (
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
  qc_note text,
  created_at timestamptz not null default now(),
  unique (reading_date, product, tile_lat, tile_lon)
);

create index lst_readings_date_temp_idx on kiln.lst_readings (reading_date desc, max_c desc);

-- One row per pipeline run; the frontend reads the latest to decide staleness.
create table kiln.ingest_runs (
  id bigint generated always as identity primary key,
  reading_date date not null,
  product text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running'
    check (status in ('running', 'succeeded', 'partial', 'failed')),
  granules_total integer,
  granules_processed integer,
  tiles_written integer,
  error text
);

create index ingest_runs_started_idx on kiln.ingest_runs (started_at desc);

-- Public read-only: RLS on, SELECT policies for anon, no write policies exist.
alter table kiln.record_holders enable row level security;
alter table kiln.lst_readings enable row level security;
alter table kiln.ingest_runs enable row level security;

create policy "public read" on kiln.record_holders
  for select to anon, authenticated using (true);
create policy "public read" on kiln.lst_readings
  for select to anon, authenticated using (true);
create policy "public read" on kiln.ingest_runs
  for select to anon, authenticated using (true);

grant usage on schema kiln to anon, authenticated, service_role;
grant select on all tables in schema kiln to anon, authenticated;
grant all on all tables in schema kiln to service_role;
grant usage, select on all sequences in schema kiln to service_role;

alter default privileges in schema kiln grant select on tables to anon, authenticated;
alter default privileges in schema kiln grant all on tables to service_role;
