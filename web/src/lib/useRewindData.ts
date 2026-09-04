import { useEffect, useState } from 'react'
import type { LstReading } from './types'
import { fetchReadingsForDate } from './useKilnData'
import { dailyTileUrl, hasDailyRaster } from './tiles'

export interface RewindData {
  readings: LstReading[]
  tileUrl: string | null
  loading: boolean
  error: string | null
}

interface Loaded extends RewindData {
  date: string
}

const EMPTY: RewindData = { readings: [], tileUrl: null, loading: false, error: null }
const PENDING: RewindData = { readings: [], tileUrl: null, loading: true, error: null }

// A day the user rewound to. Readings live in the table indefinitely; the
// raster pyramid may have been pruned, so its absence is expected and the
// 1-degree fills carry the view instead.
export function useRewindData(date: string | null): RewindData {
  const [loaded, setLoaded] = useState<Loaded | null>(null)

  useEffect(() => {
    if (date === null) return
    let cancelled = false

    async function load(readingDate: string) {
      try {
        const [readings, raster] = await Promise.all([
          fetchReadingsForDate(readingDate),
          hasDailyRaster(readingDate),
        ])
        if (cancelled) return
        setLoaded({
          date: readingDate,
          readings,
          tileUrl: raster && readings.length > 0 ? dailyTileUrl(readingDate) : null,
          loading: false,
          error: null,
        })
      } catch (err) {
        if (cancelled) return
        setLoaded({
          ...EMPTY,
          date: readingDate,
          error: err instanceof Error ? err.message : 'Could not load that date',
        })
      }
    }

    void load(date)
    return () => {
      cancelled = true
    }
  }, [date])

  if (date === null) return EMPTY
  // A result for a different date is a stale answer to a question already
  // replaced, so it reads as still loading rather than as this date's data.
  if (loaded === null || loaded.date !== date) return PENDING
  return loaded
}
