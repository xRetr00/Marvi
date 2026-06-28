import { atom } from 'nanostores'

import { createIslandQueue, type IslandCard, type IslandQueueSnapshot } from '@/lib/island-queue'

export const $islandCards = atom<IslandQueueSnapshot>({ active: null, queued: [] })

const queue = createIslandQueue({ maxQueue: 3, onChange: snap => $islandCards.set(snap) })

export function showIslandCard(card: IslandCard): void {
  queue.show(card, { force: card.kind === 'approval' })
}

export function dismissIslandCard(id?: string): void {
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
