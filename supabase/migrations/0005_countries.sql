-- Country capture (2026-09-02): the leaderboard filters by country, so the
-- geocoder stores Nominatim's country field alongside the display name.
alter table kiln.place_names add column if not exists country text;
alter table kiln.alltime_readings add column if not exists country text;
alter table kiln.anomaly_readings add column if not exists country text;
alter table kiln.lst_readings add column if not exists country text;
