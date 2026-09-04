import { useKilnStore } from '../../store/useKilnStore'
import type { AlltimeManifest, IngestRun, LstReading, TileManifest } from '../../lib/types'
import { AREA_LABEL, type Area } from '../../lib/geo'
import {
  AIR_ESTIMATE_NOTE,
  formatCoords,
  formatMonthYear,
  formatTemp,
  formatUtcDate,
} from '../../lib/format'
import { LayerToggle } from '../LayerToggle/LayerToggle'
import styles from './HeaderPanel.module.css'

// The pipeline runs daily; readings older than two full days mean it is
// failing and the layer must say so rather than pass old data off as current.
function isStale(readingDate: string): boolean {
  const age = Date.now() - new Date(`${readingDate}T00:00:00Z`).getTime()
  return age > 2.5 * 24 * 60 * 60 * 1000
}

// "This map area" needs bounds that change as the map moves; on the map itself
// that would redraw the layer on every pan, so it stays a leaderboard filter.
const MAP_AREAS = (Object.keys(AREA_LABEL) as Area[]).filter((area) => area !== 'view')

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10)
}

interface HeaderPanelProps {
  topAlltime: LstReading[]
  latestRun: IngestRun | null
  readingsCount: number
  manifest: TileManifest | null
  alltimeManifest: AlltimeManifest | null
  alltimeCount: number
  // Readings actually drawn, after the map filters.
  shownCount: number
  // Readings for a rewound date before filtering, and whether it is in flight.
  rewindCount: number
  rewindLoading: boolean
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
  shownCount,
  rewindCount,
  rewindLoading,
  loading,
  error,
}: HeaderPanelProps) {
  const activeLayer = useKilnStore((s) => s.activeLayer)
  const setActiveLayer = useKilnStore((s) => s.setActiveLayer)
  const setScreen = useKilnStore((s) => s.setScreen)
  const map = useKilnStore((s) => s.map)
  const query = useKilnStore((s) => s.mapQuery)
  const setQuery = useKilnStore((s) => s.setMapQuery)
  const area = useKilnStore((s) => s.mapArea)
  const setArea = useKilnStore((s) => s.setMapArea)
  const showAnomalies = useKilnStore((s) => s.showAnomalies)
  const setShowAnomalies = useKilnStore((s) => s.setShowAnomalies)
  const tempMode = useKilnStore((s) => s.tempMode)
  const setTempMode = useKilnStore((s) => s.setTempMode)
  const unit = useKilnStore((s) => s.unit)
  const setUnit = useKilnStore((s) => s.setUnit)
  const rewindDate = useKilnStore((s) => s.rewindDate)
  const setRewindDate = useKilnStore((s) => s.setRewindDate)

  const today = todayUtc()
  const filtersActive = query.trim() !== '' || area !== 'everywhere'
  const noMatches = !loading && !rewindLoading && shownCount === 0 && filtersActive

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
          {!loading && latestRun !== null && rewindDate !== null && (
            <p className={styles.status}>
              {rewindLoading ? (
                <>
                  Loading the satellite passes of{' '}
                  <span className={styles.mono}>{formatUtcDate(rewindDate)}</span>…
                </>
              ) : rewindCount === 0 ? (
                <>
                  No satellite pass recorded for{' '}
                  <span className={styles.mono}>{formatUtcDate(rewindDate)}</span>. The archive
                  starts when the daily pull first ran; pick another date or return to today.
                </>
              ) : (
                <>
                  <span className={styles.mono}>{rewindCount}</span> hot tiles, satellite passes of{' '}
                  <span className={styles.mono}>{formatUtcDate(rewindDate)}</span>. Blank regions
                  are cloud cover or missing passes, not cool ground.
                </>
              )}
            </p>
          )}
          {!loading && latestRun !== null && rewindDate === null && (
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
                <span className={styles.recordValue}>{formatTemp(reading.max_c, unit)}</span>
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

      <div className={styles.controls}>
        <h2 className={styles.controlsHeading}>Filter and display</h2>

        <div className={styles.filterRow}>
          <input
            type="search"
            className={styles.search}
            placeholder="Search places and countries"
            aria-label="Search places and countries"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <select
            className={styles.select}
            aria-label="Area"
            value={area}
            onChange={(event) => setArea(event.target.value as Area)}
          >
            {MAP_AREAS.map((value) => (
              <option key={value} value={value}>
                {AREA_LABEL[value]}
              </option>
            ))}
          </select>
        </div>

        {noMatches && <p className={styles.note}>No readings match these filters.</p>}
        {filtersActive && !noMatches && (
          <p className={styles.note}>
            Filtered views are drawn on the coarser 1-degree grid; the 1 km detail returns when
            the filter is cleared.
          </p>
        )}

        <div className={styles.controlRow}>
          <span className={styles.controlLabel} id="temp-mode-label">
            Color by
          </span>
          <div className={styles.segmented} role="group" aria-labelledby="temp-mode-label">
            <button
              type="button"
              className={styles.segment}
              aria-pressed={tempMode === 'ground'}
              onClick={() => setTempMode('ground')}
            >
              Ground temp
            </button>
            <button
              type="button"
              className={styles.segment}
              aria-pressed={tempMode === 'air'}
              onClick={() => setTempMode('air')}
            >
              Feels like
            </button>
          </div>
        </div>

        {tempMode === 'air' && (
          <p className={styles.note}>
            Ground temperature is shown at full detail; estimated air temperature uses the same
            coarser grid as the click targets. {AIR_ESTIMATE_NOTE}
          </p>
        )}

        <div className={styles.controlRow}>
          <span className={styles.controlLabel} id="unit-label">
            Units
          </span>
          <div className={styles.segmented} role="group" aria-labelledby="unit-label">
            <button
              type="button"
              className={styles.segment}
              aria-pressed={unit === 'C'}
              onClick={() => setUnit('C')}
            >
              °C
            </button>
            <button
              type="button"
              className={styles.segment}
              aria-pressed={unit === 'F'}
              onClick={() => setUnit('F')}
            >
              °F
            </button>
          </div>
        </div>

        {activeLayer === 'latest' && (
          <div className={styles.controlRow}>
            <label className={styles.controlLabel} htmlFor="rewind-date">
              Date
            </label>
            <input
              id="rewind-date"
              type="date"
              className={styles.date}
              value={rewindDate ?? today}
              min={alltimeManifest?.since}
              max={today}
              onChange={(event) => {
                const value = event.target.value
                setRewindDate(value === '' || value === today ? null : value)
              }}
            />
            <button
              type="button"
              className={styles.reset}
              onClick={() => setRewindDate(null)}
              disabled={rewindDate === null}
            >
              Today
            </button>
          </div>
        )}

        <label className={styles.checkRow}>
          <input
            type="checkbox"
            className={styles.checkbox}
            checked={showAnomalies}
            onChange={(event) => setShowAnomalies(event.target.checked)}
          />
          <span>Show wildfires &amp; volcanoes</span>
        </label>

        {showAnomalies && (
          <p className={styles.note}>
            Diamonds mark heat that is not the climate. Select one for its cause. These readings
            are excluded from the rankings.
          </p>
        )}
      </div>

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
