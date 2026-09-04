import { Map } from './components/Map/Map'
import { HeaderPanel } from './components/HeaderPanel/HeaderPanel'
import { LiveLayer, TILES_BASE } from './components/LiveLayer/LiveLayer'
import { Leaderboard } from './components/Leaderboard/Leaderboard'
import { useKilnData } from './lib/useKilnData'
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

  const isAlltime = activeLayer === 'alltime'
  const viewReadings = isAlltime ? alltimeReadings : readings
  const viewTileUrl = isAlltime
    ? alltimeManifest && `${TILES_BASE}${alltimeManifest.tile_url_template}`
    : manifest && `${TILES_BASE}${manifest.tile_url_template.replace('{date}', manifest.date)}`

  return (
    <>
      <Map />
      <LiveLayer
        readings={viewReadings}
        tileUrl={viewTileUrl ?? null}
        popupLabel={isAlltime ? 'All-time max' : 'Daily max'}
      />
      <HeaderPanel
        topAlltime={alltimeReadings}
        latestRun={latestRun}
        readingsCount={readings.length}
        manifest={manifest}
        alltimeManifest={alltimeManifest}
        alltimeCount={alltimeReadings.length}
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
