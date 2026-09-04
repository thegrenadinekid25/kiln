import { useEffect } from 'react'
import maplibregl from 'maplibre-gl'
import { useKilnStore } from '../../store/useKilnStore'
import type { AnomalyReading, VolcanicSource } from '../../lib/types'
import {
  ANOMALY_CAUSE_LABEL,
  escapeHtml,
  formatCoords,
  formatTemp,
  formatUtcDate,
} from '../../lib/format'
import styles from './AnomalyLayer.module.css'

interface AnomalyLayerProps {
  anomalies: AnomalyReading[]
  volcanoes: Map<string, VolcanicSource>
}

// Heat the satellites saw that is not the climate. Deliberately not on the
// heat ramp: these readings are excluded from the rankings, so they must not
// read as part of the same measurement.
export function AnomalyLayer({ anomalies, volcanoes }: AnomalyLayerProps) {
  const map = useKilnStore((s) => s.map)
  const unit = useKilnStore((s) => s.unit)

  useEffect(() => {
    if (!map || anomalies.length === 0) return

    const popup = new maplibregl.Popup({ closeButton: true, maxWidth: '280px' })

    const markers = anomalies.map((anomaly) => {
      const vent = anomaly.source_slug ? volcanoes.get(anomaly.source_slug) : undefined
      const place = vent
        ? `${vent.name}, ${vent.country}`
        : (anomaly.place_name ?? formatCoords(anomaly.max_lat, anomaly.max_lon))
      const cause = ANOMALY_CAUSE_LABEL[anomaly.cause] ?? anomaly.cause

      const el = document.createElement('button')
      el.type = 'button'
      el.className = styles.marker
      el.appendChild(document.createElement('span'))

      el.addEventListener('click', (event) => {
        event.stopPropagation()
        const citation =
          vent && vent.source_url.startsWith('https://')
            ? `<div><a href="${escapeHtml(vent.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(vent.source_name)}</a></div>`
            : ''
        popup
          .setLngLat([anomaly.max_lon, anomaly.max_lat])
          .setHTML(
            `<strong style="font-family: var(--font-mono)">${formatTemp(anomaly.max_c, unit)}</strong>` +
              `<div>${escapeHtml(cause)}: ${escapeHtml(place)}</div>` +
              `<div>Seen ${formatUtcDate(anomaly.reading_date)}, ${escapeHtml(anomaly.satellite)}</div>` +
              `<div>Not counted in the rankings.</div>` +
              citation,
          )
          .addTo(map)
      })

      const marker = new maplibregl.Marker({ element: el, anchor: 'center' })
        .setLngLat([anomaly.max_lon, anomaly.max_lat])
        .addTo(map)
      // MapLibre stamps its own aria-label on a custom element, so the real
      // one has to be written back after the marker is added.
      el.setAttribute(
        'aria-label',
        `${cause}: ${place}, ${formatTemp(anomaly.max_c, unit)} on ${formatUtcDate(anomaly.reading_date)}`,
      )
      return marker
    })

    return () => {
      markers.forEach((m) => m.remove())
      popup.remove()
    }
  }, [map, anomalies, volcanoes, unit])

  return null
}
