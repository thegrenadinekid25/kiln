import { create } from 'zustand'
import type maplibregl from 'maplibre-gl'

export type ActiveLayer = 'latest' | 'alltime'
export type Screen = 'map' | 'leaderboard'

interface KilnState {
  activeLayer: ActiveLayer
  setActiveLayer: (layer: ActiveLayer) => void
  screen: Screen
  setScreen: (screen: Screen) => void
  map: maplibregl.Map | null
  setMap: (map: maplibregl.Map | null) => void
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
}))
