import { atom } from 'nanostores'

import { persistBoolean, storedBoolean } from '@/lib/storage'

/**
 * Desktop-only preferences for the always-on voice presence. These live in
 * localStorage (not the shared hermes config) because they govern this
 * machine's overlay/mic behavior, not the agent.
 *
 *  - presenceEnabled: the master switch. Off = no app-wide wake-word listening
 *    (no background mic) and no island.
 *  - islandEnabled: the Dynamic Island visual. Off keeps the wake word working
 *    but never opens the island window.
 *  - cardsEnabled: whether show_card / approval cards surface on the presence.
 *  - debug: print detailed [voice-presence] logs to the console for
 *    troubleshooting the wake word, island window, and cards.
 */

const PRESENCE_KEY = 'hermes.desktop.voice-presence.enabled.v1'
const ISLAND_KEY = 'hermes.desktop.voice-presence.island.v1'
const CARDS_KEY = 'hermes.desktop.voice-presence.cards.v1'
const DEBUG_KEY = 'hermes.desktop.voice-presence.debug.v1'

export const $presenceEnabled = atom(storedBoolean(PRESENCE_KEY, true))
export const $islandEnabled = atom(storedBoolean(ISLAND_KEY, true))
export const $presenceCardsEnabled = atom(storedBoolean(CARDS_KEY, true))
export const $voicePresenceDebug = atom(storedBoolean(DEBUG_KEY, false))

$presenceEnabled.subscribe(value => persistBoolean(PRESENCE_KEY, value))
$islandEnabled.subscribe(value => persistBoolean(ISLAND_KEY, value))
$presenceCardsEnabled.subscribe(value => persistBoolean(CARDS_KEY, value))
$voicePresenceDebug.subscribe(value => persistBoolean(DEBUG_KEY, value))

export function setPresenceEnabled(value: boolean): void {
  $presenceEnabled.set(value)
}

export function setIslandEnabled(value: boolean): void {
  $islandEnabled.set(value)
}

export function setPresenceCardsEnabled(value: boolean): void {
  $presenceCardsEnabled.set(value)
}

export function setVoicePresenceDebug(value: boolean): void {
  $voicePresenceDebug.set(value)
}
