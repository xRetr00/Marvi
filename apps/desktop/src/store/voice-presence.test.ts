import { afterEach, describe, expect, it } from 'vitest'

import { setVoicePlaybackState } from './voice-playback'
import {
  $bargeInEnabled,
  $conversation,
  $userCaption,
  $voiceState,
  $wakeStatus,
  deriveVoicePhase,
  publishBargeInEnabled,
  publishConversation,
  publishWakeStatus
} from './voice-presence'

const idlePlayback = { audioElement: null, messageId: null, sequence: 0, source: null, status: 'idle' as const }
const speakingReadAloud = { audioElement: null, messageId: 'm1', sequence: 1, source: 'read-aloud' as const, status: 'speaking' as const }

describe('deriveVoicePhase', () => {
  it('is off when nothing is active', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'armed' })).toBe('off')
  })

  it('lights as wake the moment the hotword is detected', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'woken' })).toBe('wake')
  })

  it('does not light while only armed for the hotword', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'armed' })).toBe('off')
  })

  it('keeps the island lit through the post-hotword capture states', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'woken' })).toBe('wake')
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'listening' })).toBe('wake')
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'transcribing' })).toBe('wake')
  })

  it('maps an active conversation status straight through', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'listening', wakeStatus: 'idle' })).toBe('listening')
    expect(deriveVoicePhase({ active: true, voiceStatus: 'speaking', wakeStatus: 'idle' })).toBe('speaking')
  })

  it('is off when a conversation is active but idle between turns', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'idle', wakeStatus: 'idle' })).toBe('off')
  })
})

describe('$voiceState (computed)', () => {
  afterEach(() => {
    $conversation.set({ active: false, status: 'idle', level: 0, muted: false, caption: null })
    $wakeStatus.set('idle')
    $userCaption.set(null)
    setVoicePlaybackState(idlePlayback)
    $bargeInEnabled.set(true)
  })

  it('reflects the conversation slice when active', () => {
    publishConversation({ active: true, status: 'listening', level: 0.5, muted: false, caption: 'hi' })
    expect($voiceState.get()).toEqual({ phase: 'listening', level: 0.5, muted: false, caption: 'hi', userCaption: null, bargeable: false })
  })

  it('lights as wake from the wake-word slice', () => {
    publishWakeStatus('woken')
    expect($voiceState.get()).toEqual({ phase: 'wake', level: 0, muted: false, caption: null, userCaption: null, bargeable: false })
  })

  it('is off when both slices are idle', () => {
    expect($voiceState.get()).toEqual({ phase: 'off', level: 0, muted: false, caption: null, userCaption: null, bargeable: false })
  })

  it('lights as speaking + bargeable from TTS playback in any mode (read-aloud/wake-word)', () => {
    // No hands-free conversation active; playback alone drives the island.
    setVoicePlaybackState(speakingReadAloud)
    expect($voiceState.get()).toMatchObject({ phase: 'speaking', bargeable: true })
  })

  it('is speaking but not bargeable when barge-in is disabled', () => {
    setVoicePlaybackState(speakingReadAloud)
    publishBargeInEnabled(false)
    expect($voiceState.get()).toMatchObject({ phase: 'speaking', bargeable: false })
  })
})
