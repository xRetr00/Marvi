import { atom } from 'nanostores'

import { persistBoolean, storedBoolean } from '@/lib/storage'

/**
 * Desktop-only preferences for the always-on voice presence. These live in
 * localStorage (not the shared hermes config) because they govern this
 * machine's overlay/mic behavior, not the agent.
 *
 *  - presenceEnabled: the master switch. Off = no app-wide wake-word listening
 *    (no background mic) and no glow.
 *  - glowEnabled: the Apple-Intelligence edge glow visual. Off keeps wake word
 *    working but never opens the glow window.
 *  - cardsEnabled: whether show_card / approval cards surface on the presence.
 */

const PRESENCE_KEY = 'hermes.desktop.voice-presence.enabled.v1'
const GLOW_KEY = 'hermes.desktop.voice-presence.glow.v1'
const CARDS_KEY = 'hermes.desktop.voice-presence.cards.v1'

export const $presenceEnabled = atom(storedBoolean(PRESENCE_KEY, true))
export const $glowEnabled = atom(storedBoolean(GLOW_KEY, true))
export const $presenceCardsEnabled = atom(storedBoolean(CARDS_KEY, true))

$presenceEnabled.subscribe(value => persistBoolean(PRESENCE_KEY, value))
$glowEnabled.subscribe(value => persistBoolean(GLOW_KEY, value))
$presenceCardsEnabled.subscribe(value => persistBoolean(CARDS_KEY, value))

export function setPresenceEnabled(value: boolean): void {
  $presenceEnabled.set(value)
}

export function setGlowEnabled(value: boolean): void {
  $glowEnabled.set(value)
}

export function setPresenceCardsEnabled(value: boolean): void {
  $presenceCardsEnabled.set(value)
}
