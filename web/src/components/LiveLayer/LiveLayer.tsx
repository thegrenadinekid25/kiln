import { useEffect } from 'react'
import maplibregl from 'maplibre-gl'
import { useKilnStore } from '../../store/useKilnStore'
import type { LstReading } from '../../lib/types'
import { formatAirEstimate, formatTemp, formatUtcDateTime } from '../../lib/format'
import styles from './LiveLayer.module.css'

interface LiveLayerProps {
  readings: LstReading[]
  // Resolved absolute XYZ template for the active view's pyramid, or null when
  // that view has no raster yet (fall back to visible 1-degree fills).
  tileUrl: string | null
  popupLabel: string
}

const SOURCE_ID = 'kiln-lst-tiles'
const FILL_LAYER_ID = 'kiln-lst-tiles-fill'
const LINE_LAYER_ID = 'kiln-lst-tiles-line'
const RASTER_SOURCE_ID = 'kiln-lst-raster'
const RASTER_LAYER_ID = 'kiln-lst-raster-layer'

export const TILES_BASE = `${import.meta.env.VITE_SUPABASE_URL}/storage/v1/object/public/kiln-tiles/`

// The heat ramp steps mirror --heat-1..5 in tokens.css. MapLibre expressions
// need literal colors, so the values are duplicated here on purpose; change
// both places together.
const HEAT_COLOR: maplibregl.ExpressionSpecification = [
  'step',
  ['get', 'max_c'],
  '#C9B896',
  50, '#C79B5B',
  58, '#BC7431',
  66, '#9A4E17',
  74, '#6E3410',
]

function tileFeature(reading: LstReading): GeoJSON.Feature {
  const { tile_lat: lat, tile_lon: lon } = reading
  return {
    type: 'Feature',
    properties: {
      max_c: reading.max_c,
      satellite: reading.satellite,
      observed_at: reading.observed_at,
      qc_note: reading.qc_note,
    },
    geometry: {
      type: 'Polygon',
      coordinates: [
        [
          [lon, lat],
          [lon + 1, lat],
          [lon + 1, lat + 1],
          [lon, lat + 1],
          [lon, lat],
        ],
      ],
    },
  }
}

// Renders the day's per-tile maxima as 1-degree cells. Tiles the pipeline did
// not write are simply absent — cloud gaps stay gaps, nothing is interpolated.
export function LiveLayer({ readings, tileUrl, popupLabel }: LiveLayerProps) {
  const map = useKilnStore((s) => s.map)

  useEffect(() => {
    if (!map || readings.length === 0) return

    const collection: GeoJSON.FeatureCollection = {
      type: 'FeatureCollection',
      features: readings.map(tileFeature),
    }

    // With a raster pyramid available, the ~1 km raster carries the visual and
    // the 1-degree fills become a near-invisible hit layer for click popups.
    // Without one (raster pipeline not yet run), the fills remain the visual.
    const hasRaster = tileUrl !== null

    // Keep the raster under the basemap's place labels.
    const firstSymbolLayer = map.getStyle().layers?.find((l) => l.type === 'symbol')?.id

    if (hasRaster) {
      map.addSource(RASTER_SOURCE_ID, {
        type: 'raster',
        tiles: [tileUrl as string],
        tileSize: 256,
        minzoom: 0,
        maxzoom: 7,
        attribution: 'Temperature: NASA MODIS via LANCE',
      })
      map.addLayer(
        {
          id: RASTER_LAYER_ID,
          type: 'raster',
          source: RASTER_SOURCE_ID,
          paint: {
          // Linear resampling plus a zoom-eased opacity: past the pyramid's
          // native ~1 km, sparse hot pixels soften instead of becoming a
          // field of hard dots.
          'raster-opacity': ['interpolate', ['linear'], ['zoom'], 4, 0.78, 7, 0.68, 10, 0.5],
          'raster-resampling': 'linear',
        },
        },
        firstSymbolLayer,
      )
    }

    map.addSource(SOURCE_ID, { type: 'geojson', data: collection })
    map.addLayer(
      {
        id: FILL_LAYER_ID,
        type: 'fill',
        source: SOURCE_ID,
        paint: { 'fill-color': HEAT_COLOR, 'fill-opacity': hasRaster ? 0.01 : 0.7 },
      },
      hasRaster ? firstSymbolLayer : undefined,
    )
    map.addLayer({
      id: LINE_LAYER_ID,
      type: 'line',
      source: SOURCE_ID,
      paint: {
        'line-color': 'rgba(27, 30, 27, 0.18)',
        'line-width': hasRaster ? 0 : 0.5,
      },
    })

    const popup = new maplibregl.Popup({ closeButton: false, maxWidth: '260px' })

    function onClick(event: maplibregl.MapLayerMouseEvent) {
      const feature = event.features?.[0]
      if (!feature) return
      const props = feature.properties as {
        max_c: number
        satellite: string
        observed_at: string
        qc_note: string | null
      }
      const note = props.qc_note ? `<div>${props.qc_note}</div>` : ''
      popup
        .setLngLat(event.lngLat)
        .setHTML(
          `<strong style="font-family: var(--font-mono)">${formatTemp(props.max_c)}</strong>` +
            `<div>${formatAirEstimate(props.max_c)} (estimated)</div>` +
            `<div>${popupLabel}, ${props.satellite}</div>` +
            `<div>As of ${formatUtcDateTime(props.observed_at)}</div>` +
            note,
        )
        .addTo(map!)
    }

    function onEnter() {
      map!.getCanvas().style.cursor = 'pointer'
    }
    function onLeave() {
      map!.getCanvas().style.cursor = ''
    }

    map.on('click', FILL_LAYER_ID, onClick)
    map.on('mouseenter', FILL_LAYER_ID, onEnter)
    map.on('mouseleave', FILL_LAYER_ID, onLeave)

    // The 1-degree tiles are near-invisible at world zoom; the day's hottest
    // readings get labeled markers so the layer communicates without zooming.
    // readings arrive sorted hottest-first.
    const markers = readings.slice(0, 3).map((reading) => {
      const el = document.createElement('button')
      el.type = 'button'
      el.className = styles.hotspot
      el.textContent = formatTemp(reading.max_c)

      el.addEventListener('click', (event) => {
        event.stopPropagation()
        const note = reading.qc_note ? `<div>${reading.qc_note}</div>` : ''
        popup
          .setLngLat([reading.max_lon, reading.max_lat])
          .setHTML(
            `<strong style="font-family: var(--font-mono)">${formatTemp(reading.max_c)}</strong>` +
              `<div>${formatAirEstimate(reading.max_c)} (estimated)</div>` +
              `<div>${popupLabel}, ${reading.satellite}</div>` +
              `<div>As of ${formatUtcDateTime(reading.observed_at)}</div>` +
              note,
          )
          .addTo(map!)
      })

      const marker = new maplibregl.Marker({ element: el, anchor: 'bottom' })
        .setLngLat([reading.max_lon, reading.max_lat])
        .addTo(map!)
      el.setAttribute(
        'aria-label',
        `${popupLabel} ${formatTemp(reading.max_c)}, as of ${formatUtcDateTime(reading.observed_at)}`,
      )
      return marker
    })

    return () => {
      markers.forEach((m) => m.remove())
      popup.remove()
      map.off('click', FILL_LAYER_ID, onClick)
      map.off('mouseenter', FILL_LAYER_ID, onEnter)
      map.off('mouseleave', FILL_LAYER_ID, onLeave)
      if (map.getLayer(LINE_LAYER_ID)) map.removeLayer(LINE_LAYER_ID)
      if (map.getLayer(FILL_LAYER_ID)) map.removeLayer(FILL_LAYER_ID)
      if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID)
      if (map.getLayer(RASTER_LAYER_ID)) map.removeLayer(RASTER_LAYER_ID)
      if (map.getSource(RASTER_SOURCE_ID)) map.removeSource(RASTER_SOURCE_ID)
    }
  }, [map, readings, tileUrl, popupLabel])

  return null
}
