import { useKilnStore } from '../../store/useKilnStore'
import type { AlltimeManifest, IngestRun, LstReading, TileManifest } from '../../lib/types'
import { formatCoords, formatMonthYear, formatTemp, formatUtcDate } from '../../lib/format'
import { LayerToggle } from '../LayerToggle/LayerToggle'
import styles from './HeaderPanel.module.css'

// The pipeline runs daily; readings older than two full days mean it is
// failing and the layer must say so rather than pass old data off as current.
function isStale(readingDate: string): boolean {
  const age = Date.now() - new Date(`${readingDate}T00:00:00Z`).getTime()
  return age > 2.5 * 24 * 60 * 60 * 1000
}

interface HeaderPanelProps {
  topAlltime: LstReading[]
  latestRun: IngestRun | null
  readingsCount: number
  manifest: TileManifest | null
  alltimeManifest: AlltimeManifest | null
  alltimeCount: number
  loading: boolean
  error: string | null
}

export function HeaderPanel({
  topAlltime,
  latestRun,
  readingsCount,
  manifest,
  alltimeManifest,
  alltimeCount,
  loading,
  error,
}: HeaderPanelProps) {
  const activeLayer = useKilnStore((s) => s.activeLayer)
  const setActiveLayer = useKilnStore((s) => s.setActiveLayer)
  const setScreen = useKilnStore((s) => s.setScreen)
  const map = useKilnStore((s) => s.map)

  function goToReading(reading: LstReading) {
    if (!map) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      map.jumpTo({ center: [reading.max_lon, reading.max_lat], zoom: 5 })
    } else {
      map.flyTo({ center: [reading.max_lon, reading.max_lat], zoom: 5, duration: 1200 })
    }
  }

  return (
    <section className={styles.panel} aria-label="Map controls">
      <h1 className={styles.wordmark}>The Hottest Place in the World</h1>
      <p className={styles.tagline}>Measured by satellite.</p>

      <LayerToggle />

      {error !== null && (
        <p className={styles.status} role="alert">
          The data could not be loaded. The map still works; readings will appear on the next
          successful load.
        </p>
      )}

      {activeLayer === 'latest' && error === null && (
        <div className={styles.layerContent}>
          {loading && <p className={styles.status}>Loading readings…</p>}
          {!loading && latestRun === null && (
            <>
              <p className={styles.status}>
                Live satellite readings begin when the daily NASA pull starts.
              </p>
              <button
                type="button"
                className={styles.emptyAction}
                onClick={() => setActiveLayer('alltime')}
              >
                See the all-time view
              </button>
            </>
          )}
          {!loading && latestRun !== null && (
            <p className={styles.status}>
              {manifest !== null ? (
                <>
                  Satellite passes of{' '}
                  <span className={styles.mono}>{formatUtcDate(manifest.date)}</span>, drawn at 1 km.
                  Blank regions are cloud cover or missing passes, not cool ground. Readings that
                  coincide with active wildfire detections are excluded.
                </>
              ) : (
                <>
                  <span className={styles.mono}>{readingsCount}</span> hot tiles, satellite passes
                  of <span className={styles.mono}>{formatUtcDate(latestRun.reading_date)}</span>.
                  Blank regions are cloud cover or missing passes, not cool ground. The very
                  hottest readings can be wildfires, not bare ground.
                </>
              )}
              {isStale(latestRun.reading_date) && (
                <span className={styles.stale}>
                  {' '}
                  These readings are stale: the daily pull has not succeeded since then.
                </span>
              )}
            </p>
          )}
        </div>
      )}

      {activeLayer === 'alltime' && error === null && !loading && (
        <div className={styles.layerContent}>
          <p className={styles.status}>
            {alltimeManifest !== null ? (
              <>
                The hottest each place has been since{' '}
                <span className={styles.mono}>{formatUtcDate(alltimeManifest.since)}</span>, drawn
                at 1 km, from this map's own archive. Wildfires, volcanoes, and unverified
                readings are set aside. The archive's records:
              </>
            ) : alltimeCount > 0 ? (
              <>The hottest each place has been since the archive began. The archive's records:</>
            ) : (
              <>The archive starts accumulating with the next daily pull.</>
            )}
          </p>
          <ul className={styles.recordList} aria-label="All-time records">
          {topAlltime.slice(0, 3).map((reading) => (
            <li key={`${reading.tile_lat},${reading.tile_lon}`}>
              <button
                type="button"
                className={styles.recordRow}
                onClick={() => goToReading(reading)}
              >
                <span className={styles.recordValue}>{formatTemp(reading.max_c)}</span>
                <span className={styles.recordPlace}>
                  {reading.place_name ?? formatCoords(reading.max_lat, reading.max_lon)}
                </span>
                <span className={styles.recordChip}>{formatMonthYear(reading.reading_date)}</span>
              </button>
            </li>
          ))}
          </ul>
        </div>
      )}

      <p className={styles.leaderboardLink}>
        <button type="button" className={styles.linkButton} onClick={() => setScreen('leaderboard')}>
          Leaderboard
        </button>
      </p>

      <p className={styles.attribution}>
        Satellite data: NASA MODIS land-surface temperature via LANCE. Records cited per entry.
      </p>
    </section>
  )
}
