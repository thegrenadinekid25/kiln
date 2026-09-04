-- All-time view (decision 2026-08-31): the hottest reading Kiln's own pipeline
-- has ever recorded per 1-degree tile, accumulating daily from 2026-08-30.
-- Same shape as lst_readings plus the date the record was set.

create table kiln.alltime_readings (
  id bigint generated always as identity primary key,
  record_date date not null,
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
  updated_at timestamptz not null default now(),
  unique (tile_lat, tile_lon)
);

create index alltime_readings_temp_idx on kiln.alltime_readings (max_c desc);

alter table kiln.alltime_readings enable row level security;

create policy "public read" on kiln.alltime_readings
  for select to anon, authenticated using (true);

grant select on kiln.alltime_readings to anon, authenticated;
grant all on kiln.alltime_readings to service_role;
grant usage, select on all sequences in schema kiln to service_role;
