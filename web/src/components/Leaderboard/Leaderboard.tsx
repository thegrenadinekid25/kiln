import { useState } from 'react'
import { useKilnStore } from '../../store/useKilnStore'
import type { AnomalyReading, LstReading, VolcanicSource } from '../../lib/types'
import {
  AREA_LABEL,
  PERIOD_LABEL,
  inArea,
  inPeriod,
  matchesQuery,
  type Area,
  type Period,
} from '../../lib/geo'
import {
  AIR_ESTIMATE_NOTE,
  ANOMALY_CAUSE_LABEL,
  formatAirEstimate,
  formatCoords,
  formatMonthYear,
  formatTemp,
} from '../../lib/format'
import { LayerToggle } from '../LayerToggle/LayerToggle'
import styles from './Leaderboard.module.css'

interface LeaderboardProps {
  readings: LstReading[]
  alltimeReadings: LstReading[]
  anomalies: AnomalyReading[]
  volcanoes: Map<string, VolcanicSource>
  loading: boolean
}

const ROWS = 10

export function Leaderboard({
  readings,
  alltimeReadings,
  anomalies,
  volcanoes,
  loading,
}: LeaderboardProps) {
  const activeLayer = useKilnStore((s) => s.activeLayer)
  const setScreen = useKilnStore((s) => s.setScreen)
  const map = useKilnStore((s) => s.map)

  const [query, setQuery] = useState('')
  const [area, setArea] = useState<Area>('everywhere')
  const [period, setPeriod] = useState<Period>('all')

  const bounds = map?.getBounds()
  const viewBounds = bounds
    ? {
        west: bounds.getWest(),
        south: bounds.getSouth(),
        east: bounds.getEast(),
        north: bounds.getNorth(),
      }
    : null

  const filtered = (activeLayer === 'alltime' ? alltimeReadings : readings).filter(
    (reading) =>
      inArea(reading.max_lat, reading.max_lon, area, viewBounds) &&
      (activeLayer !== 'alltime' || inPeriod(reading.reading_date, period)) &&
      matchesQuery(query, reading.place_name, reading.country),
  )
  const rows = filtered.slice(0, ROWS)

  const anomalyRows = anomalies
    .filter((anomaly) => {
      const vent = anomaly.source_slug ? volcanoes.get(anomaly.source_slug) : undefined
      return (
        inArea(anomaly.max_lat, anomaly.max_lon, area, viewBounds) &&
        inPeriod(anomaly.reading_date, period) &&
        matchesQuery(query, anomaly.place_name, anomaly.country, vent?.name, vent?.country)
      )
    })
    .slice(0, 5)

  const filtersActive = query.trim() !== '' || area !== 'everywhere' || period !== 'all'

  function goTo(lat: number, lon: number) {
    setScreen('map')
    if (!map) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      map.jumpTo({ center: [lon, lat], zoom: 6 })
    } else {
      map.flyTo({ center: [lon, lat], zoom: 6, duration: 1200 })
    }
  }

  function anomalyPlace(anomaly: AnomalyReading): string {
    if (anomaly.source_slug) {
      const vent = volcanoes.get(anomaly.source_slug)
      if (vent) return `${vent.name}, ${vent.country}`
    }
    return anomaly.place_name ?? formatCoords(anomaly.max_lat, anomaly.max_lon)
  }

  return (
    <main className={styles.page}>
      <div className={styles.back}>
        <button type="button" className={styles.link} onClick={() => setScreen('map')}>
          Map
        </button>
      </div>
      <div className={styles.column}>
        <h1 className={styles.title}>Leaderboard</h1>
        <p className={styles.tagline}>The hottest ground on Earth, ranked.</p>
        <div className={styles.toggleWrap}>
          <LayerToggle />
        </div>

        <div className={styles.filters}>
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
            {Object.entries(AREA_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          {activeLayer === 'alltime' && (
            <select
              className={styles.select}
              aria-label="Period"
              value={period}
              onChange={(event) => setPeriod(event.target.value as Period)}
            >
              {Object.entries(PERIOD_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          )}
        </div>

        {loading && <p className={styles.status}>Loading readings…</p>}

        {!loading && rows.length === 0 && (
          <p className={styles.status}>
            {filtersActive
              ? 'No readings match these filters.'
              : 'Readings appear after the next daily satellite pull.'}
          </p>
        )}

        <ol className={styles.list}>
          {rows.map((reading, index) => (
            <li key={`${reading.tile_lat},${reading.tile_lon},${reading.reading_date}`}>
              <button
                type="button"
                className={index === 0 ? `${styles.row} ${styles.first}` : styles.row}
                onClick={() => goTo(reading.max_lat, reading.max_lon)}
              >
                <span className={styles.rank}>{index + 1}</span>
                <span className={styles.place}>
                  {reading.place_name ?? formatCoords(reading.max_lat, reading.max_lon)}
                </span>
                <span className={styles.tempStack}>
                  <span className={styles.temp}>{formatTemp(reading.max_c)}</span>
                  <span className={styles.airEst}>{formatAirEstimate(reading.max_c)}</span>
                </span>
                <span className={styles.date}>{formatMonthYear(reading.reading_date)}</span>
              </button>
            </li>
          ))}
        </ol>

        {!loading && anomalyRows.length > 0 && (
          <section className={styles.anomalies} aria-label="Not weather">
            <h2 className={styles.sectionHeading}>Not weather</h2>
            <p className={styles.sectionNote}>
              Heat the satellites saw that is not the climate: volcanoes, wildfires, and
              readings that failed verification. Excluded from the rankings above.
            </p>
            <ol className={styles.list}>
              {anomalyRows.map((anomaly) => (
                <li key={`${anomaly.max_lat},${anomaly.max_lon},${anomaly.reading_date},${anomaly.cause}`}>
                  <button
                    type="button"
                    className={styles.row}
                    onClick={() => goTo(anomaly.max_lat, anomaly.max_lon)}
                  >
                    <span className={styles.chip}>{ANOMALY_CAUSE_LABEL[anomaly.cause]}</span>
                    <span className={styles.place}>{anomalyPlace(anomaly)}</span>
                    <span className={styles.temp}>{formatTemp(anomaly.max_c)}</span>
                    <span className={styles.date}>{formatMonthYear(anomaly.reading_date)}</span>
                  </button>
                </li>
              ))}
            </ol>
          </section>
        )}

        <p className={styles.attribution}>
          Every reading fire-masked and cross-checked between satellites. NASA MODIS via LANCE.{' '}
          {AIR_ESTIMATE_NOTE}
        </p>
      </div>
    </main>
  )
}
