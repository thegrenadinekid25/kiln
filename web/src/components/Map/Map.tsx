import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useKilnStore } from '../../store/useKilnStore'
import styles from './Map.module.css'

const BASEMAP_STYLE_URL = 'https://tiles.openfreemap.org/styles/positron'
const INITIAL_CENTER: [number, number] = [30, 20]
const INITIAL_ZOOM = 2

// Atlas Plate basemap: positron's near-white palette re-inked to the sage
// plate at load, so the heat sienna is the only saturation on screen.
const PLATE = '#9AA396'
const PLATE_WATER = '#86928B'
const PLATE_GREEN = '#93A08F'
const PLATE_BUILT = '#9EA79A'
const PLATE_LINE = '#AEB6AA'
const PLATE_INK = '#2A2E28'

async function plateStyle(): Promise<maplibregl.StyleSpecification | string> {
  try {
    const res = await fetch(BASEMAP_STYLE_URL)
    if (!res.ok) return BASEMAP_STYLE_URL
    const style = (await res.json()) as maplibregl.StyleSpecification
    for (const layer of style.layers) {
      const paint: Record<string, unknown> = (layer as { paint?: Record<string, unknown> }).paint ?? {}
      if (layer.type === 'background') paint['background-color'] = PLATE
      if (layer.type === 'fill') {
        if (/water/.test(layer.id)) paint['fill-color'] = PLATE_WATER
        else if (/park|wood|grass/.test(layer.id)) paint['fill-color'] = PLATE_GREEN
        else if (/residential|building|aeroway|ice|glacier|pier/.test(layer.id)) paint['fill-color'] = PLATE_BUILT
        else paint['fill-color'] = PLATE
        delete paint['fill-outline-color']
      }
      if (layer.type === 'line') {
        if (/water/.test(layer.id)) paint['line-color'] = PLATE_WATER
        else paint['line-color'] = PLATE_LINE
      }
      if (layer.type === 'symbol') {
        paint['text-color'] = PLATE_INK
        paint['text-halo-color'] = 'rgba(154, 163, 150, 0.75)'
      }
      ;(layer as { paint?: Record<string, unknown> }).paint = paint
    }
    return style
  } catch {
    return BASEMAP_STYLE_URL
  }
}

export function Map() {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)

  useEffect(() => {
    if (!containerRef.current || mapRef.current) {
      return
    }
    let cancelled = false
    const container = containerRef.current

    void plateStyle().then((style) => {
      if (cancelled) return
      const map = new maplibregl.Map({
        container,
        style,
        center: INITIAL_CENTER,
        zoom: INITIAL_ZOOM,
        attributionControl: { compact: true },
      })
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
      mapRef.current = map
      map.on('load', () => {
        useKilnStore.getState().setMap(map)
      })
    })

    return () => {
      cancelled = true
      useKilnStore.getState().setMap(null)
      mapRef.current?.remove()
      mapRef.current = null
    }
  }, [])

  return <div ref={containerRef} className={styles.map} aria-label="Map of Earth's hottest places" />
}
