import { atom } from 'nanostores'

import { createIslandQueue, type IslandCard, type IslandQueueSnapshot } from '@/lib/island-queue'
import { vpLog } from '@/lib/voice-presence-log'
import { $mainWindowFocused } from '@/store/window-presence'

import { $presenceCardsEnabled, $presenceEnabled } from './voice-presence-settings'

const MAX_ISLAND_QUEUE = 3

export const $islandCards = atom<IslandQueueSnapshot>({ active: null, queued: [] })

const queue = createIslandQueue({ maxQueue: MAX_ISLAND_QUEUE, onChange: snap => $islandCards.set(snap) })

// Reading time for a card: base + ~220ms/word, clamped 2.5s-12s.
function readingTimeMs(text: string | undefined): number {
  const words = (text ?? '').trim().split(/\s+/).filter(Boolean).length
  return Math.min(12000, Math.max(2500, 2200 + words * 220))
}

export function showIslandCard(card: IslandCard): void {
  // Respect the desktop presence master switch and the "show cards" preference.
  if (!$presenceEnabled.get() || !$presenceCardsEnabled.get()) {
    return
  }

  // Don't surface island cards when the user is already looking at the main
  // Marvi window — the answer is right there in the chat. The card is for when
  // they're hands-free or in another app. (Minimized/unfocused → still show.)
  if ($mainWindowFocused.get()) {
    vpLog('card', 'suppressed', { id: card.id, reason: 'main-window-focused' })
    return
  }

  // Dynamic auto-dismiss: approval cards persist until resolved, and any card
  // the caller already gave an explicit duration to is left alone. Everything
  // else gets a reading-time-based timer so short cards vanish quickly and
  // long ones linger.
  const withTiming: IslandCard =
    card.kind === 'approval' || typeof card.duration === 'number'
      ? card
      : { ...card, duration: readingTimeMs(card.body || card.title), autoDismiss: true }

  vpLog('card', 'show', { kind: card.kind, id: card.id })
  queue.show(withTiming, { force: card.kind === 'approval' })
}

export function dismissIslandCard(id?: string): void {
  vpLog('card', 'dismiss', { id })
  queue.dismiss(id)
}

let submitHandler: ((text: string) => void) | null = null

/** Register how a card action's text becomes a real user turn. */
export function setIslandCardSubmitHandler(fn: ((text: string) => void) | null): void {
  submitHandler = fn
}

export function runIslandCardAction(text: string): void {
  submitHandler?.(text)
}
