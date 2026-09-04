import { useEffect, useMemo, useRef } from 'react'
import { Map } from './components/Map/Map'
import { HeaderPanel } from './components/HeaderPanel/HeaderPanel'
import { LiveLayer } from './components/LiveLayer/LiveLayer'
import { AnomalyLayer } from './components/AnomalyLayer/AnomalyLayer'
import { Leaderboard } from './components/Leaderboard/Leaderboard'
import { useKilnData } from './lib/useKilnData'
import { useRewindData } from './lib/useRewindData'
import { TILES_BASE } from './lib/tiles'
import { inArea, matchesQuery } from './lib/geo'
import { useKilnStore } from './store/useKilnStore'

function App() {
  const {
    readings,
    latestRun,
    manifest,
    alltimeReadings,
    alltimeManifest,
    anomalies,
    volcanoes,
    loading,
    error,
  } = useKilnData()
  const activeLayer = useKilnStore((s) => s.activeLayer)
  const screen = useKilnStore((s) => s.screen)
  const query = useKilnStore((s) => s.mapQuery)
  const area = useKilnStore((s) => s.mapArea)
  const showAnomalies = useKilnStore((s) => s.showAnomalies)
  const rewindDate = useKilnStore((s) => s.rewindDate)
  const map = useKilnStore((s) => s.map)

  // Rewind only applies to the daily view; the all-time layer has no date.
  const isAlltime = activeLayer === 'alltime'
  const rewind = useRewindData(isAlltime ? null : rewindDate)
  const isRewound = !isAlltime && rewindDate !== null

  const viewReadings = isAlltime ? alltimeReadings : isRewound ? rewind.readings : readings
  const viewTileUrl = isAlltime
    ? alltimeManifest && `${TILES_BASE}${alltimeManifest.tile_url_template}`
    : isRewound
      ? rewind.tileUrl
      : manifest && `${TILES_BASE}${manifest.tile_url_template.replace('{date}', manifest.date)}`

  // The same filter the leaderboard applies, applied to what the map draws.
  // "This map area" is excluded from the map's own filter, so bounds are unused.
  const shownReadings = useMemo(
    () =>
      viewReadings.filter(
        (reading) =>
          inArea(reading.max_lat, reading.max_lon, area, null) &&
          matchesQuery(query, reading.place_name, reading.country),
      ),
    [viewReadings, area, query],
  )

  // The raster pyramid is a pre-rendered picture of every reading, so it cannot
  // honour a filter. While one is on, the coarse fills carry the view instead
  // of leaving excluded heat painted underneath.
  const filtersActive = query.trim() !== '' || area !== 'everywhere'

  const shownAnomalies = useMemo(
    () =>
      showAnomalies
        ? anomalies.filter((anomaly) => {
            const vent = anomaly.source_slug ? volcanoes.get(anomaly.source_slug) : undefined
            return (
              inArea(anomaly.max_lat, anomaly.max_lon, area, null) &&
              matchesQuery(query, anomaly.place_name, anomaly.country, vent?.name, vent?.country)
            )
          })
        : [],
    [showAnomalies, anomalies, volcanoes, area, query],
  )

  const shownRef = useRef(shownReadings)
  useEffect(() => {
    shownRef.current = shownReadings
  }, [shownReadings])

  // Choosing a continent is a deliberate act, so answering it with movement is
  // expected; typing in the search box is not, and must not yank the view.
  useEffect(() => {
    if (!map || area === 'everywhere') return
    const points = shownRef.current
    if (points.length === 0) return
    let west = 180
    let south = 90
    let east = -180
    let north = -90
    for (const reading of points) {
      west = Math.min(west, reading.max_lon)
      east = Math.max(east, reading.max_lon)
      south = Math.min(south, reading.max_lat)
      north = Math.max(north, reading.max_lat)
    }
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      { padding: 64, maxZoom: 6, duration: reduced ? 0 : 900 },
    )
  }, [map, area])

  return (
    <>
      <Map />
      <LiveLayer
        readings={shownReadings}
        tileUrl={filtersActive ? null : (viewTileUrl ?? null)}
        popupLabel={isAlltime ? 'All-time max' : 'Daily max'}
      />
      <AnomalyLayer anomalies={shownAnomalies} volcanoes={volcanoes} />
      <HeaderPanel
        topAlltime={alltimeReadings}
        latestRun={latestRun}
        readingsCount={readings.length}
        manifest={manifest}
        alltimeManifest={alltimeManifest}
        alltimeCount={alltimeReadings.length}
        shownCount={shownReadings.length}
        rewindCount={rewind.readings.length}
        rewindLoading={rewind.loading}
        loading={loading}
        error={error}
      />
      {screen === 'leaderboard' && (
        <Leaderboard
          readings={readings}
          alltimeReadings={alltimeReadings}
          anomalies={anomalies}
          volcanoes={volcanoes}
          loading={loading}
        />
      )}
    </>
  )
}

export default App
