-- Place names (decision 2026-09-02): readings show real place names before
-- coordinates. Reverse-geocoded pipeline-side at write time; null means the
-- geocoder had nothing, and the UI falls back to formatted coordinates.

alter table kiln.alltime_readings add column if not exists place_name text;
alter table kiln.anomaly_readings add column if not exists place_name text;
alter table kiln.lst_readings add column if not exists place_name text;

-- Geocode cache, keyed by half-degree cell so one lookup serves every reading
-- in the neighborhood. Written by the pipeline, readable by anyone.
create table if not exists kiln.place_names (
  cell_lat numeric(5, 1) not null,
  cell_lon numeric(5, 1) not null,
  place_name text,
  source text not null default 'nominatim',
  resolved_at timestamptz not null default now(),
  primary key (cell_lat, cell_lon)
);

alter table kiln.place_names enable row level security;
create policy "public read" on kiln.place_names
  for select to anon, authenticated using (true);
grant select on kiln.place_names to anon, authenticated;
grant all on kiln.place_names to service_role;
