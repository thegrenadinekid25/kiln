import { useKilnStore } from '../../store/useKilnStore'
import styles from './LayerToggle.module.css'

export function LayerToggle() {
  const activeLayer = useKilnStore((state) => state.activeLayer)
  const setActiveLayer = useKilnStore((state) => state.setActiveLayer)

  return (
    <div className={styles.toggle} role="group" aria-label="Map layer">
      <button
        type="button"
        className={styles.button}
        aria-pressed={activeLayer === 'latest'}
        onClick={() => setActiveLayer('latest')}
      >
        Most recent
      </button>
      <button
        type="button"
        className={styles.button}
        aria-pressed={activeLayer === 'alltime'}
        onClick={() => setActiveLayer('alltime')}
      >
        All-time
      </button>
    </div>
  )
}
