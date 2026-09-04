import { create } from 'zustand'
import type maplibregl from 'maplibre-gl'
import type { Area } from '../lib/geo'
import type { TempUnit } from '../lib/format'

export type ActiveLayer = 'latest' | 'alltime'
export type Screen = 'map' | 'leaderboard'
// Which temperature the live layer colors by: the satellite's own surface
// reading, or the air estimate derived from it.
export type TempMode = 'ground' | 'air'

const UNIT_KEY = 'kiln.unit'

// Storage can be unavailable (private browsing, blocked site data). A missing
// preference is not an error; it just means the default.
function storedUnit(): TempUnit {
  try {
    return window.localStorage.getItem(UNIT_KEY) === 'F' ? 'F' : 'C'
  } catch {
    return 'C'
  }
}

function persistUnit(unit: TempUnit): void {
  try {
    window.localStorage.setItem(UNIT_KEY, unit)
  } catch {
    // Preference stays for this session only.
  }
}

interface KilnState {
  activeLayer: ActiveLayer
  setActiveLayer: (layer: ActiveLayer) => void
  screen: Screen
  setScreen: (screen: Screen) => void
  map: maplibregl.Map | null
  setMap: (map: maplibregl.Map | null) => void
  mapQuery: string
  setMapQuery: (query: string) => void
  mapArea: Area
  setMapArea: (area: Area) => void
  showAnomalies: boolean
  setShowAnomalies: (show: boolean) => void
  tempMode: TempMode
  setTempMode: (mode: TempMode) => void
  unit: TempUnit
  setUnit: (unit: TempUnit) => void
  rewindDate: string | null
  setRewindDate: (date: string | null) => void
}

export const useKilnStore = create<KilnState>((set) => ({
  activeLayer: 'latest',
  setActiveLayer: (layer) => set({ activeLayer: layer }),
  screen: window.location.hash === '#leaderboard' ? 'leaderboard' : 'map',
  setScreen: (screen) => {
    window.location.hash = screen === 'leaderboard' ? '#leaderboard' : ''
    set({ screen })
  },
  map: null,
  setMap: (map) => set({ map }),
  mapQuery: '',
  setMapQuery: (mapQuery) => set({ mapQuery }),
  mapArea: 'everywhere',
  setMapArea: (mapArea) => set({ mapArea }),
  showAnomalies: false,
  setShowAnomalies: (showAnomalies) => set({ showAnomalies }),
  tempMode: 'ground',
  setTempMode: (tempMode) => set({ tempMode }),
  unit: storedUnit(),
  setUnit: (unit) => {
    persistUnit(unit)
    set({ unit })
  },
  rewindDate: null,
  setRewindDate: (rewindDate) => set({ rewindDate }),
}))
