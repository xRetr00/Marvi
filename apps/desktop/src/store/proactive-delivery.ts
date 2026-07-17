import { requestVoiceStart, requestVoiceStop } from '@/app/chat/composer/focus'
import { translateNow } from '@/i18n'
import { playSpeechText } from '@/lib/voice-playback'

import { dismissIslandCard, showIslandCard } from './island-cards'
import { dispatchNativeNotification } from './native-notifications'
import { notify } from './notifications'
import { $voicePlayback } from './voice-playback'
import { $presenceEnabled } from './voice-presence-settings'
import { isSecondaryWindow } from './windows'

const POLL_MS = 5_000
const CURSOR_KEY = 'marvi:proactive-delivery-cursor'
const SEEN_SUGGESTIONS_KEY = 'marvi:proactive-delivery-suggestions'

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

interface ProactiveSuggestion {
  id: string
  title?: null | string
  summary?: null | string
}

interface ProactiveSuggestionsResponse {
  suggestions: ProactiveSuggestion[]
}

let timer: number | null = null
let polling = false
let activeAlarmCardId: null | string = null

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

function seenSuggestionIds(): Set<string> | null {
  try {
    const raw = window.localStorage.getItem(SEEN_SUGGESTIONS_KEY)
    if (raw === null) {
      return null
    }
    const value = JSON.parse(raw)
    return new Set(Array.isArray(value) ? value.filter(id => typeof id === 'string') : [])
  } catch {
    return null
  }
}

function saveSeenSuggestionIds(ids: Set<string>): void {
  try {
    window.localStorage.setItem(SEEN_SUGGESTIONS_KEY, JSON.stringify([...ids].slice(-100)))
  } catch {
    // The durable suggestion still remains readable in Mind.
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

function unseenRuns(runs: readonly ProactiveRun[], lastSeen: string): ProactiveRun[] {
  const chronological = [...runs].reverse()
  const index = chronological.findIndex(run => runKey(run) === lastSeen)

  return index >= 0 ? chronological.slice(index + 1) : chronological
}

function surface(run: ProactiveRun): void {
  const message = proactiveMessage(run)

  if (!message) {
    return
  }

  const id = `proactive:${runKey(run)}`
  const body = message.length > 600 ? `${message.slice(0, 597)}…` : message
  const title = translateNow('mind.proactiveTitle')

  if (run.source === 'smart_room_alarm') {
    activeAlarmCardId = id
    showIslandCard(
      {
        id,
        kind: 'approval',
        title: run.summary || 'Alarm',
        body,
        actions: [
          { id: 'awake', label: "I'm awake", value: "I'm awake. Acknowledge and stop the active Smart Room alarm now." }
        ]
      },
      { allowWhenFocused: true }
    )
    dispatchNativeNotification({
      kind: 'backgroundDone',
      title: run.summary || 'Alarm',
      body,
      global: true,
      silent: false
    })
    void playSpeechText(message, { messageId: id, source: 'read-aloud' })
      .catch(() => undefined)
      .finally(() => requestVoiceStart())

    return
  }

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
  if (run.source === 'smart_room_welcome' || ($presenceEnabled.get() && $voicePlayback.get().status === 'idle')) {
    void playSpeechText(message, { messageId: id, source: 'read-aloud' }).catch(() => undefined)
  }
}

function surfaceSuggestion(suggestion: ProactiveSuggestion): void {
  const message = String(suggestion.summary || suggestion.title || '').trim()
  if (!message) {
    return
  }

  const id = `suggestion:${suggestion.id}`
  const title = suggestion.title || translateNow('mind.proactiveTitle')
  const body = message.length > 600 ? `${message.slice(0, 597)}…` : message

  showIslandCard({ id, kind: 'approval', title, body })
  notify({ id, kind: 'info', title, message: body, durationMs: 12_000, placement: 'default' })
  dispatchNativeNotification({ kind: 'backgroundDone', title, body, global: true, silent: true })

  if ($presenceEnabled.get() && $voicePlayback.get().status === 'idle') {
    void playSpeechText(message, { messageId: id, source: 'read-aloud' }).catch(() => undefined)
  }
}

async function pollSuggestions(): Promise<void> {
  const response = await window.hermesDesktop.api<ProactiveSuggestionsResponse>({
    path: '/api/subconscious/suggestions'
  })
  const suggestions = Array.isArray(response.suggestions) ? response.suggestions : []
  const seen = seenSuggestionIds()

  if (seen === null) {
    const adopted = new Set<string>()
    for (const suggestion of suggestions) {
      adopted.add(suggestion.id)
    }
    saveSeenSuggestionIds(adopted)
    return
  }

  for (const suggestion of suggestions) {
    if (!seen.has(suggestion.id)) {
      seen.add(suggestion.id)
      surfaceSuggestion(suggestion)
    }
  }
  saveSeenSuggestionIds(seen)
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

    if (!previous && newest?.source === 'smart_room_alarm' && newest.outcome === 'message') {
      // An active alarm must survive a Desktop restart instead of being silently
      // adopted as the initial cursor like ordinary background activity.
      surface(newest)
    } else if (previous) {
      for (const run of unseenRuns(runs, previous)) {
        if (run.source === 'smart_room_alarm' && run.outcome === 'diff_silent') {
          if (activeAlarmCardId) {
            dismissIslandCard(activeAlarmCardId)
            activeAlarmCardId = null
          }

          requestVoiceStop()
        } else if (run.outcome === 'message' && proactiveMessage(run)) {
          surface(run)
        }
      }
    }

    if (newest) {
      saveCursor(runKey(newest))
    }

    await pollSuggestions()
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
