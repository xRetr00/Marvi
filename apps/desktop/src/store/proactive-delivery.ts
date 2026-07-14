import { translateNow } from '@/i18n'
import { playSpeechText } from '@/lib/voice-playback'

import { showIslandCard } from './island-cards'
import { dispatchNativeNotification } from './native-notifications'
import { notify } from './notifications'
import { $voicePlayback } from './voice-playback'
import { $presenceEnabled } from './voice-presence-settings'
import { isSecondaryWindow } from './windows'

const POLL_MS = 5_000
const CURSOR_KEY = 'marvi:proactive-delivery-cursor'

interface ProactiveRun {
  at?: null | string
  job_id?: null | string
  outcome?: null | string
  source?: null | string
  summary?: null | string
  thought?: null | string
}

interface ProactiveActivityResponse {
  runs: ProactiveRun[]
}

let timer: number | null = null
let polling = false

function runKey(run: ProactiveRun): string {
  return `${run.at ?? ''}:${run.job_id ?? ''}:${run.source ?? ''}`
}

function cursor(): string {
  try {
    return window.localStorage.getItem(CURSOR_KEY) ?? ''
  } catch {
    return ''
  }
}

function saveCursor(value: string): void {
  try {
    window.localStorage.setItem(CURSOR_KEY, value)
  } catch {
    // Delivery still works for this process when storage is unavailable.
  }
}

export function proactiveMessage(run: ProactiveRun): string {
  return String(run.thought || run.summary || '').trim()
}

export function unseenProactiveRuns(runs: readonly ProactiveRun[], lastSeen: string): ProactiveRun[] {
  const chronological = [...runs].reverse()

  if (!lastSeen) {
    return []
  }

  const index = chronological.findIndex(run => runKey(run) === lastSeen)
  const candidates = index >= 0 ? chronological.slice(index + 1) : chronological

  return candidates.filter(run => run.outcome === 'message' && Boolean(proactiveMessage(run)))
}

function surface(run: ProactiveRun): void {
  const message = proactiveMessage(run)

  if (!message) {
    return
  }

  const id = `proactive:${runKey(run)}`
  const body = message.length > 600 ? `${message.slice(0, 597)}…` : message
  const title = translateNow('mind.proactiveTitle')

  showIslandCard({ id, kind: 'result', title, body })
  notify({
    id,
    kind: 'info',
    title,
    message: body,
    durationMs: 12_000,
    placement: 'default'
  })
  dispatchNativeNotification({
    kind: 'backgroundDone',
    title,
    body,
    global: true,
    silent: true
  })

  // Voice Presence is the user's consent switch for unsolicited audio.
  if ($presenceEnabled.get() && $voicePlayback.get().status === 'idle') {
    void playSpeechText(message, { messageId: id, source: 'read-aloud' }).catch(() => undefined)
  }
}

async function poll(): Promise<void> {
  if (polling) {
    return
  }

  polling = true

  try {
    const response = await window.hermesDesktop.api<ProactiveActivityResponse>({
      path: '/api/subconscious/activity?limit=20'
    })

    const runs = Array.isArray(response.runs) ? response.runs : []
    const newest = runs[0]
    const previous = cursor()

    if (previous) {
      for (const run of unseenProactiveRuns(runs, previous)) {
        surface(run)
      }
    }

    if (newest) {
      saveCursor(runKey(newest))
    }
  } catch {
    // The backend may be starting/restarting; the next poll catches up.
  } finally {
    polling = false
  }
}

export function startProactiveDeliveryPolling(): void {
  if (timer !== null || isSecondaryWindow()) {
    return
  }

  void poll()
  timer = window.setInterval(() => void poll(), POLL_MS)
}

export function stopProactiveDeliveryPolling(): void {
  if (timer !== null) {
    window.clearInterval(timer)
  }

  timer = null
  polling = false
}
