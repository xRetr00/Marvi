import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { resolveDuplexPresentation } from '@/app/voice-island/duplex-presentation'
import { useDuplexVoice } from '@/app/voice-island/use-duplex-voice'
import { useI18n } from '@/i18n'
import { chatMessageText, collectUnspokenTurnSpeech } from '@/lib/chat-messages'
import { triggerHaptic } from '@/lib/haptics'
import { $voiceConversationStartRequest, takeVoiceConversationStart } from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import { notifyError } from '@/store/notifications'
import { $autoSpeakReplies, setAutoSpeakReplies } from '@/store/voice-prefs'
import { publishBargeInEnabled, publishConversation, setUserCaption, type VoiceStatus } from '@/store/voice-presence'

import type { ComposerTarget } from '../focus'
import { onComposerVoiceStartRequest, onComposerVoiceStopRequest, onComposerVoiceToggleRequest } from '../focus'
import { useComposerScope } from '../scope'
import type { ChatBarProps } from '../types'

import { useAutoSpeakReplies } from './use-auto-speak-replies'
import { useReadAloudBargeIn } from './use-read-aloud-barge-in'
import { useVoiceConversation } from './use-voice-conversation'
import { useVoiceRecorder } from './use-voice-recorder'

interface UseComposerVoiceArgs {
  bargeInEnabled?: boolean
  busy: boolean
  clearDraft: () => void
  disabled: boolean
  focusInput: () => void
  insertText: (text: string) => void
  maxRecordingSeconds: number
  onCancel: ChatBarProps['onCancel']
  onSubmit: ChatBarProps['onSubmit']
  onTranscribeAudio: ChatBarProps['onTranscribeAudio']
  sessionId: string | null | undefined
  semanticTurnEnabled?: boolean
  streamingSttEnabled?: boolean
  /** This composer's focus-bus key — voice toggles targeting another
   *  composer (or the active one, when not us) are ignored. */
  target: ComposerTarget
}

/**
 * The composer's voice engine: push-to-talk dictation (transcript → draft), the
 * full voice-conversation loop, and auto-speak of replies. Self-contained — it
 * consumes the draft/submit primitives passed in but nothing depends back on it,
 * so it lifts cleanly out of ChatBar.
 */
export function useComposerVoice({
  bargeInEnabled,
  busy,
  clearDraft,
  disabled,
  focusInput,
  insertText,
  maxRecordingSeconds,
  onCancel,
  onSubmit,
  onTranscribeAudio,
  sessionId,
  semanticTurnEnabled,
  streamingSttEnabled,
  target
}: UseComposerVoiceArgs) {
  const { t } = useI18n()
  // A tile's composer speaks ITS transcript, not the primary chat's.
  const { $messages } = useComposerScope()
  const [voiceConversationActive, setVoiceConversationActive] = useState(false)
  const lastSpokenIdRef = useRef<string | null>(null)
  const handleDuplexConversationEnd = useCallback(() => setVoiceConversationActive(false), [])
  const duplex = useDuplexVoice(voiceConversationActive, handleDuplexConversationEnd)
  const legacyVoiceEnabled = voiceConversationActive && duplex.status === 'unavailable'
  const voiceStartRequest = useStore($voiceConversationStartRequest)

  const { dictate, voiceActivityState, voiceStatus } = useVoiceRecorder({
    focusInput,
    maxRecordingSeconds,
    onTranscript: insertText,
    onTranscribeAudio,
    streamingSttEnabled
  })

  /** Auto-speak selector: the latest unspoken reply only — a backlog collapses to the newest. */
  const pendingResponse = () => {
    const messages = $messages.get()
    const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)

    if (!last || last.id === lastSpokenIdRef.current) {
      return null
    }

    const text = chatMessageText(last).trim()

    if (!text) {
      return null
    }

    return {
      id: last.id,
      pending: Boolean(last.pending),
      text
    }
  }

  /**
   * Voice-conversation selector: every unspoken assistant bubble of the turn,
   * in order — narration interims AND the final answer, not just whichever
   * bubble happens to be last. See `collectUnspokenTurnSpeech`.
   */
  const pendingTurnResponse = () => collectUnspokenTurnSpeech($messages.get(), lastSpokenIdRef.current)

  const consumePendingResponse = () => {
    const messages = $messages.get()
    const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)

    if (last) {
      lastSpokenIdRef.current = last.id
    }
  }

  const submitVoiceTurn = async (text: string) => {
    if (busy) {
      return
    }

    triggerHaptic('submit')
    resetBrowseState(sessionId)
    clearDraft()
    await onSubmit(text)
  }

  const conversation = useVoiceConversation({
    bargeInEnabled,
    busy,
    consumePendingResponse,
    enabled: legacyVoiceEnabled,
    onFatalError: () => setVoiceConversationActive(false),
    onInterrupt: onCancel,
    onStopWord: () => setVoiceConversationActive(false),
    onSubmit: submitVoiceTurn,
    onTranscribeAudio,
    pendingResponse: pendingTurnResponse,
    semanticTurnEnabled,
    streamingSttEnabled
  })

  useReadAloudBargeIn({
    blocked: voiceConversationActive || voiceStatus !== 'idle',
    enabled: bargeInEnabled !== false,
    streamingSttEnabled
  })

  useEffect(() => {
    const duplexStatus: VoiceStatus =
      duplex.state.phase === 'listening'
        ? 'listening'
        : duplex.state.phase === 'replying'
          ? 'thinking'
          : duplex.state.phase === 'speaking'
            ? 'speaking'
            : 'idle'

    // Reuse the shared duplex→UI mapping (duplex-presentation.ts) rather than
    // re-deriving label/speaker/deep-work bookkeeping here — the same pure
    // function backs the ambient (wake-word/island) duplex session in
    // desktop-controller.tsx, so both duplex paths present identically.
    const presentation = duplex.status === 'active' ? resolveDuplexPresentation(duplex.state) : null

    publishConversation({
      active: voiceConversationActive,
      status: duplex.status === 'active' ? duplexStatus : conversation.status,
      level: duplex.status === 'active' ? duplex.level : conversation.level,
      muted: duplex.status === 'active' ? false : conversation.muted,
      caption: duplex.status === 'active' ? duplex.state.replyText : conversation.caption,
      activity: presentation?.activity ?? null,
      deepWorking: presentation?.deepWorking ?? false,
      deepMode: presentation?.deepMode ?? null,
      label: presentation?.label ?? null,
      speakerBadge: presentation?.speakerBadge ?? null,
      speakerName: presentation?.speakerName ?? null,
      captionIgnored: presentation?.captionIgnored ?? false
    })

    if (duplex.status === 'active') {
      setUserCaption(duplex.state.partialCaption ?? duplex.state.utteranceCaption)
    } else if (!voiceConversationActive) {
      setUserCaption(null)
    }
  }, [
    conversation.caption,
    conversation.level,
    conversation.muted,
    conversation.status,
    duplex.level,
    duplex.state,
    duplex.status,
    voiceConversationActive
  ])

  // duplex phase 3: one flag drives the island's "interrupt" affordance across
  // every mode (both speak paths arm barge-in from this same prop).
  useEffect(() => {
    publishBargeInEnabled(bargeInEnabled !== false)
  }, [bargeInEnabled])

  // The `composer.voice` hotkey (Ctrl+B) toggles the conversation. Starting
  // with STT unconfigured lets the conversation surface its own "configure
  // speech-to-text" notice rather than silently no-opping.
  const toggleVoiceConversation = useCallback(() => {
    if (disabled) {
      return
    }

    if (voiceConversationActive) {
      setVoiceConversationActive(false)
      void conversation.end()
    } else {
      setVoiceConversationActive(true)
    }
  }, [conversation, disabled, voiceConversationActive])

  useEffect(
    () => onComposerVoiceToggleRequest(toggled => toggled === target && toggleVoiceConversation()),
    [target, toggleVoiceConversation]
  )

  useEffect(
    () => onComposerVoiceStartRequest(started => started === target && !disabled && setVoiceConversationActive(true)),
    [disabled, target]
  )
  useEffect(
    () =>
      onComposerVoiceStopRequest(stopped => {
        if (stopped !== target) {
          return
        }
        setVoiceConversationActive(false)
        void conversation.end()
      }),
    [conversation, target]
  )

  useEffect(() => {
    if (target === 'main' && !disabled && takeVoiceConversationStart(voiceStartRequest) && !voiceConversationActive) {
      setVoiceConversationActive(true)
    }
  }, [disabled, target, voiceConversationActive, voiceStartRequest])

  // Explicit start/end for the on-screen conversation controls (the hotkey uses
  // the gated toggle above).
  const startConversation = useCallback(() => setVoiceConversationActive(true), [])

  const endConversation = useCallback(() => {
    setVoiceConversationActive(false)
    void conversation.end()
  }, [conversation])

  const handleToggleAutoSpeak = useCallback(() => {
    void setAutoSpeakReplies(!$autoSpeakReplies.get()).catch(error =>
      notifyError(error, t.settings.config.autosaveFailed)
    )
  }, [t])

  useAutoSpeakReplies({
    conversationActive: voiceConversationActive,
    failureLabel: t.assistant.thread.readAloudFailed,
    markSpoken: consumePendingResponse,
    pendingReply: pendingResponse,
    sessionId
  })

  return {
    conversation,
    dictate,
    endConversation,
    handleToggleAutoSpeak,
    startConversation,
    voiceActivityState,
    voiceConversationActive,
    voiceStatus
  }
}
