import { useEffect, useState } from 'react'

export interface ActivityWatchStatus {
  checked: boolean
  checking: boolean
  reachable: boolean
}

const POLL_INTERVAL_MS = 30_000

/**
 * Polls the ActivityWatch (localhost:5600) reachability probe exposed by the
 * Electron main process (`hermes:presence:awStatus` — see electron/main.cjs),
 * so the Presence settings status dot reflects whether the desktop collector
 * is actually running, without the renderer eating a CORS error probing it
 * directly.
 */
export function useActivityWatchStatus(pollMs = POLL_INTERVAL_MS): ActivityWatchStatus {
  const [state, setState] = useState<ActivityWatchStatus>({ checked: false, checking: true, reachable: false })

  useEffect(() => {
    let cancelled = false

    async function probe() {
      setState(s => ({ ...s, checking: true }))

      try {
        const result = await window.hermesDesktop?.presence?.awStatus?.()

        if (!cancelled) {
          setState({ checked: true, checking: false, reachable: Boolean(result?.reachable) })
        }
      } catch {
        if (!cancelled) {
          setState({ checked: true, checking: false, reachable: false })
        }
      }
    }

    void probe()
    const intervalId = window.setInterval(() => void probe(), pollMs)

    return () => {
      cancelled = true
      window.clearInterval(intervalId)
    }
  }, [pollMs])

  return state
}
