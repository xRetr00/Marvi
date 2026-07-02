import { atom } from 'nanostores'

import { createIslandQueue, type IslandCard, type IslandQueueSnapshot } from '@/lib/island-queue'
import { vpLog } from '@/lib/voice-presence-log'

import { $presenceCardsEnabled, $presenceEnabled } from './voice-presence-settings'

const MAX_ISLAND_QUEUE = 3

export const $islandCards = atom<IslandQueueSnapshot>({ active: null, queued: [] })

const queue = createIslandQueue({ maxQueue: MAX_ISLAND_QUEUE, onChange: snap => $islandCards.set(snap) })

export function showIslandCard(card: IslandCard): void {
  // Respect the desktop presence master switch and the "show cards" preference.
  if (!$presenceEnabled.get() || !$presenceCardsEnabled.get()) {
    return
  }

  vpLog('card', 'show', { kind: card.kind, id: card.id })
  queue.show(card, { force: card.kind === 'approval' })
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
