-- Curated all-time records (historical-records spec).
-- SATELLITE-DERIVED ONLY (decision 2026-08-31): every record must come from the
-- same MODIS land-surface-temperature lineage the live layer shows. Station
-- air-temperature records (Death Valley, Dallol) are deliberately out of scope.
-- Update policy: only when a new peer-reviewed or NASA-published figure supersedes.

insert into kiln.record_holders
  (slug, title, place_name, lat, lon, measurement_type, record_kind, value_c,
   observed_on, period, method, source_name, source_url, notes)
values
  (
    'lut-desert-lst',
    'Hottest land-surface temperature ever recorded',
    'Lut Desert, Iran',
    30.6, 58.5,
    'land_surface', 'all_time_max', 80.8,
    null, '2002-2019 MODIS Aqua analysis (record year 2018)',
    'Satellite land-surface temperature, MODIS Aqua at 1 km resolution',
    'Zhao et al. 2021, Bulletin of the American Meteorological Society',
    'https://doi.org/10.1175/BAMS-D-20-0325.1',
    'Matched by the Sonoran Desert the following year. Supersedes the widely cited 70.7 C (2005) figure, also from Lut, measured at coarser 5 km resolution.'
  ),
  (
    'sonoran-desert-lst',
    'Hottest land-surface temperature ever recorded (tied)',
    'Sonoran Desert, Mexico',
    31.6, -113.8,
    'land_surface', 'all_time_max', 80.8,
    null, '2002-2019 MODIS Aqua analysis (record year 2019)',
    'Satellite land-surface temperature, MODIS Aqua at 1 km resolution',
    'Zhao et al. 2021, Bulletin of the American Meteorological Society',
    'https://doi.org/10.1175/BAMS-D-20-0325.1',
    'Tied with the Lut Desert at 80.8 C in the same 18-year MODIS analysis.'
  ),
  (
    'qaidam-basin-diurnal',
    'Most extreme single-day temperature swing',
    'Qaidam Basin, China',
    37.8, 95.0,
    'land_surface', 'diurnal_range', 81.8,
    null, '2002-2019 MODIS Aqua analysis',
    'Satellite land-surface temperature, MODIS Aqua at 1 km resolution',
    'Zhao et al. 2021, Bulletin of the American Meteorological Society',
    'https://doi.org/10.1175/BAMS-D-20-0325.1',
    'Largest recorded one-day land-surface temperature range on Earth.'
  )
on conflict (slug) do update set
  title = excluded.title,
  place_name = excluded.place_name,
  lat = excluded.lat,
  lon = excluded.lon,
  measurement_type = excluded.measurement_type,
  record_kind = excluded.record_kind,
  value_c = excluded.value_c,
  observed_on = excluded.observed_on,
  period = excluded.period,
  method = excluded.method,
  source_name = excluded.source_name,
  source_url = excluded.source_url,
  notes = excluded.notes,
  updated_at = now();
