-- Verified volcanic heat sources for kiln's MODIS anomaly classifier.
-- Generated from ingest/kiln_ingest/data/volcanic_sources.json -- keep both files in sync.
-- Coordinates verified against the Smithsonian Global Volcanism Program (volcano.si.edu)
-- or, where noted, USGS Hawaiian Volcano Observatory.

INSERT INTO kiln.volcanic_sources
  (slug, name, country, lat, lon, radius_km, source_name, source_url, notes)
VALUES
  ('erta-ale', 'Erta Ale', 'Ethiopia', 13.6, 40.67, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=221080', 'Persistent lava lake in summit caldera, one of the world''s few long-lived lava lakes.'),
  ('nyiragongo', 'Nyiragongo', 'DR Congo', -1.52, 29.25, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=223030', 'Lava lake active in the summit crater since at least 1971; fissure eruption reached Goma in May 2021.'),
  ('nyamuragira', 'Nyamuragira', 'DR Congo', -1.41, 29.2, 8.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=223020', 'Africa''s most active volcano, with frequent effusive basaltic eruptions from summit and flank vents.'),
  ('piton-de-la-fournaise', 'Piton de la Fournaise', 'France (Reunion)', -21.23, 55.71, 8.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=233020', 'One of the world''s most frequently active basaltic shield volcanoes, erupting roughly every 1-2 years.'),
  ('ol-doinyo-lengai', 'Ol Doinyo Lengai', 'Tanzania', -2.76, 35.91, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=222120', 'Only volcano known to erupt natrocarbonatite lava, an unusually low-temperature magma.'),
  ('mount-michael', 'Mount Michael', 'United Kingdom (South Sandwich Islands)', -57.78, -26.45, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=390090', 'Persistent lava lake confirmed via satellite thermal imagery, near-continuous eruption since November 2014.'),
  ('heard-island-big-ben', 'Heard Island (Big Ben)', 'Australia', -53.11, 73.51, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=234010', 'Remote, glacier-covered stratovolcano; active summit vent is Mawson Peak.'),
  ('erebus', 'Erebus', 'Antarctica', -77.53, 167.17, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=390020', 'One of the few volcanoes on Earth with a persistent lava lake, active since at least 1972.'),
  ('stromboli', 'Stromboli', 'Italy', 38.79, 15.21, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=211040', 'Essentially continuous mild Strombolian explosions recorded for over a millennium.'),
  ('etna', 'Etna', 'Italy', 37.73, 15.0, 9.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=211060', 'Europe''s most active volcano, with frequent summit and flank eruptions.'),
  ('kilauea', 'Kilauea', 'United States', 19.421, -155.287, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=332010', 'Persistent and recurring lava lake activity at Halemaumau crater within Kilauea caldera.'),
  ('kilauea-lerz-2018', 'Kilauea Lower East Rift Zone (2018)', 'United States', 19.462, -154.912, 10.0, 'USGS Hawaiian Volcano Observatory', 'https://data.usgs.gov/datacatalog/metadata/USGS.651b6b94d34e44db0e2cd5bf.xml', '2018 lower East Rift Zone eruption near Fissure 8 fed a lava channel to Kapoho Bay; distinct site about 14 km ESE of the Kilauea summit.'),
  ('mauna-loa', 'Mauna Loa', 'United States', 19.475, -155.608, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=332020', 'Erupted from its Northeast Rift Zone November-December 2022, its first eruption since 1984.'),
  ('ambrym', 'Ambrym', 'Vanuatu', -16.25, 168.12, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=257040', 'Active lava lakes historically maintained within the Marum and Benbow pit craters.'),
  ('yasur', 'Yasur', 'Vanuatu', -19.532, 169.447, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=257100', 'Essentially continuous Strombolian/Vulcanian activity since at least 1774.'),
  ('manam', 'Manam', 'Papua New Guinea', -4.08, 145.037, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=251020', 'Frequently active island stratovolcano 13 km off PNG''s north coast.'),
  ('bagana', 'Bagana', 'Papua New Guinea', -6.137, 155.196, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=255020', 'Persistently active, with viscous andesitic lava dome and flow effusion.'),
  ('nishinoshima', 'Nishinoshima', 'Japan', 27.247, 140.874, 10.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=284096', 'Island substantially enlarged by effusive and explosive activity across 2013-2023.'),
  ('sakurajima', 'Sakurajima (Aira caldera)', 'Japan', 31.5772, 130.6589, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=282080', 'Post-caldera cone of Aira caldera; frequent explosions and ash plumes at Minamidake crater.'),
  ('krakatau', 'Krakatau (Anak Krakatau)', 'Indonesia', -6.102, 105.423, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=262000', '2018 flank collapse triggered a deadly tsunami; the cone has rebuilt with frequent eruptions since.'),
  ('dukono', 'Dukono', 'Indonesia', 1.699, 127.878, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=268010', 'Near-continuous ash and thermal emission since 1933, one of the longest-running eruptions on record.'),
  ('ibu', 'Ibu', 'Indonesia', 1.488, 127.63, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=268030', 'Persistently active since 1998 lava dome growth began, with frequent explosions.'),
  ('semeru', 'Semeru', 'Indonesia', -8.108, 112.922, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=263300', 'Frequently active, with pyroclastic flows down the Besuk Kobokan drainage on the SE flank.'),
  ('merapi', 'Merapi', 'Indonesia', -7.541, 110.446, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=263250', 'Persistent lava dome growth and collapse generates frequent pyroclastic flows.'),
  ('bardarbunga-holuhraun', 'Bardarbunga (Holuhraun 2014-15)', 'Iceland', 64.85, -16.83, 10.0, 'Smithsonian Global Volcanism Program (Bardarbunga system)', 'https://volcano.si.edu/volcano.cfm?vn=373030', 'Holuhraun fissure eruption, August 2014-February 2015, produced Iceland''s largest lava flow since 1783; site about 18 km NE of Bardarbunga''s own caldera.'),
  ('fagradalsfjall', 'Fagradalsfjall', 'Iceland', 63.895, -22.258, 10.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=371032', 'First Reykjanes Peninsula eruption in about 800 years; effusive fissure activity in nearby Geldingadalir, March-September 2021.'),
  ('litli-hrutur-sundhnukur', 'Litli-Hrutur / Sundhnukur', 'Iceland', 63.879, -22.387, 10.0, 'Smithsonian Global Volcanism Program (Reykjanes reporting)', 'https://volcano.si.edu/showreport.cfm?wvar=GVP.WVAR20231220-371020', 'Sundhnukur crater row erupted repeatedly December 2023-2025 near Grindavik; preceded by the Litli-Hrutur fissure, July-August 2023.'),
  ('cumbre-vieja-tajogaite', 'Cumbre Vieja (Tajogaite 2021)', 'Spain', 28.613, -17.866, 10.0, 'Smithsonian Global Volcanism Program (La Palma)', 'https://volcano.si.edu/volcano.cfm?vn=383010', 'Longest historical eruption on La Palma, September-December 2021, with an extensive lava flow field to the coast.'),
  ('villarrica', 'Villarrica', 'Chile', -39.42, -71.93, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=357120', 'Persistently active basaltic-andesite stratovolcano with an intermittently active summit lava lake.'),
  ('sierra-negra', 'Sierra Negra', 'Ecuador', -0.83, -91.17, 10.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=353050', 'Largest caldera in the Galapagos; June-August 2018 fissure eruption produced lava flows covering over 30 sq km.'),
  ('wolf', 'Wolf', 'Ecuador', 0.02, -91.35, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=353020', 'Erupted in 2015 and again January-April 2022, with an SO2 plume and southeast-flank lava flows.'),
  ('fernandina', 'Fernandina', 'Ecuador', -0.37, -91.55, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=353010', 'Most frequently active Galapagos shield volcano; March 2024 eruption was its largest in 15 years.'),
  ('fuego', 'Fuego', 'Guatemala', 14.47, -90.88, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=342090', 'Near-daily Strombolian explosions, with occasional pyroclastic and lava flows.'),
  ('pacaya', 'Pacaya', 'Guatemala', 14.38, -90.6, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=342110', 'Persistent lava flow and Strombolian activity from Mackenney crater, continuously since 1961.'),
  ('masaya', 'Masaya', 'Nicaragua', 11.98, -86.16, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=344100', 'Santiago crater hosts a small persistent lava lake with ongoing degassing since October 2015.'),
  ('sangay', 'Sangay', 'Ecuador', -2.0, -78.34, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=352090', 'Ecuador''s most active volcano, with frequent daily explosions and ash-gas plumes.'),
  ('reventador', 'Reventador', 'Ecuador', -0.08, -77.66, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=352010', 'Most frequently active of Ecuador''s eastern-cordillera volcanoes, with recurring lava flows.'),
  ('popocatepetl', 'Popocatepetl', 'Mexico', 19.02, -98.62, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=341090', 'Current eruptive period began January 2005 and continues, with frequent gas-ash exhalations.'),
  ('tolbachik-2012-2013', 'Tolbachik (2012-2013 Fissure Eruption)', 'Russia', 55.77, 160.32, 10.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=300240', 'November 2012-September 2013 fissure eruption from vents about 6-7 km south of the Plosky Tolbachik summit produced Kamchatka''s largest basaltic lava flow field in decades.'),
  ('klyuchevskoy', 'Klyuchevskoy', 'Russia', 56.06, 160.64, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=300260', 'Highest and most active volcano on Kamchatka, with near-continuous explosive and effusive activity.'),
  ('shiveluch', 'Shiveluch', 'Russia', 56.65, 161.36, 7.0, 'Smithsonian Global Volcanism Program', 'https://volcano.si.edu/volcano.cfm?vn=300270', 'Persistent lava dome growth with frequent pyroclastic flows and ash emissions.')
ON CONFLICT (slug) DO UPDATE SET
  name = EXCLUDED.name,
  country = EXCLUDED.country,
  lat = EXCLUDED.lat,
  lon = EXCLUDED.lon,
  radius_km = EXCLUDED.radius_km,
  source_name = EXCLUDED.source_name,
  source_url = EXCLUDED.source_url,
  notes = EXCLUDED.notes,
  updated_at = now();
