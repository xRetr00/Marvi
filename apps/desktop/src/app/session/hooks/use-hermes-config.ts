import { type MutableRefObject, useCallback, useRef, useState } from 'react'

import { setTerminalFontFamilyFromConfig } from '@/app/right-sidebar/terminal/terminal-font'
import { getHermesConfig, getHermesConfigDefaults } from '@/hermes'
import { BUILTIN_PERSONALITIES, normalizePersonalityValue, personalityNamesFromConfig } from '@/lib/chat-runtime'
import { normalize } from '@/lib/text'
import {
  $currentCwd,
  getComposerSelectionGeneration,
  getCurrentModelSource,
  setAvailablePersonalities,
  setCurrentCwd,
  setCurrentFastMode,
  setCurrentPersonality,
  setCurrentReasoningEffort,
  setCurrentServiceTier,
  setDefaultReasoningEffort,
  setIntroPersonality
} from '@/store/session'
import { normalizeWakeWordConfig, type WakeWordConfig } from '@/lib/wake-word'
import {
  applyAutoSpeakFromConfig,
  applyThinkingSoundFromConfig,
  applyVoiceStopPhraseFromConfig
} from '@/store/voice-prefs'

const DEFAULT_VOICE_SECONDS = 120
const FAST_TIERS = new Set(['fast', 'priority', 'on'])

function recordingLimit(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : DEFAULT_VOICE_SECONDS
}

/** config.yaml hands back whatever the user wrote — `reasoning_effort: false`
 *  (or `off`/`no`, which YAML also parses to boolean false) means thinking
 *  disabled, and a bare boolean must not throw on `.trim()`. */
function normalizeConfigEffort(value: unknown): string {
  if (value === false) {
    return 'none'
  }

  if (typeof value !== 'string') {
    return ''
  }

  const effort = normalize(value)

  return effort === 'false' || effort === 'disabled' ? 'none' : effort
}

interface HermesConfigOptions {
  activeSessionIdRef: MutableRefObject<string | null>
  refreshProjectBranch?: (cwd: string) => Promise<void>
}

export function useHermesConfig({ activeSessionIdRef, refreshProjectBranch }: HermesConfigOptions) {
  const [voiceMaxRecordingSeconds, setVoiceMaxRecordingSeconds] = useState(DEFAULT_VOICE_SECONDS)
  const [sttEnabled, setSttEnabled] = useState(true)
  const [sttProvider, setSttProvider] = useState('')
  const [streamingSttEnabled, setStreamingSttEnabled] = useState(false)
  const [streamingSttProvider, setStreamingSttProvider] = useState('')
  const [ttsProvider, setTtsProvider] = useState('')
  const [voiceBargeInEnabled, setVoiceBargeInEnabled] = useState(true)
  const [voiceSemanticTurnEnabled, setVoiceSemanticTurnEnabled] = useState(true)
  const [wakeWordConfig, setWakeWordConfig] = useState<WakeWordConfig>(() => normalizeWakeWordConfig(undefined))
  const profileRefreshEpochRef = useRef(0)

  const refreshHermesConfig = useCallback(
    async (force = false) => {
      if (force) {
        profileRefreshEpochRef.current += 1
      }

      const profileRefreshEpoch = profileRefreshEpochRef.current
      const selectionGeneration = getComposerSelectionGeneration()

      try {
        const [config, defaults] = await Promise.all([getHermesConfig(), getHermesConfigDefaults().catch(() => ({}))])

        if (profileRefreshEpochRef.current !== profileRefreshEpoch) {
          return
        }

        const personality = normalizePersonalityValue(
          typeof config.display?.personality === 'string' ? config.display.personality : ''
        )

        setIntroPersonality(personality)
        // Active sessions keep their per-session value; standalone falls back to config.
        setCurrentPersonality(prev => (activeSessionIdRef.current ? prev || personality : personality))
        setAvailablePersonalities([
          ...new Set([
            'none',
            ...BUILTIN_PERSONALITIES,
            ...personalityNamesFromConfig(defaults),
            ...personalityNamesFromConfig(config)
          ])
        ])

        const cwd = (config.terminal?.cwd ?? '').trim()
        const selectedCwd = $currentCwd.get()
        const branchCwd = selectedCwd || (cwd !== '.' ? cwd : '')

        if (branchCwd) {
          if (!selectedCwd && !activeSessionIdRef.current) {
            setCurrentCwd(branchCwd)
          }
          void refreshProjectBranch?.(branchCwd)
        }

        const reasoning = normalizeConfigEffort(config.agent?.reasoning_effort)
        const tier = (config.agent?.service_tier ?? '').trim()

        // Publish the profile default regardless of whether the composer is
        // reseeded below: picker rows and preset application resolve "the
        // default" from here, so a manual model pick must not leave them
        // rendering/applying Hermes' built-in medium over the user's config.
        setDefaultReasoningEffort(reasoning)

        const shouldSeedComposer =
          !activeSessionIdRef.current &&
          getComposerSelectionGeneration() === selectionGeneration &&
          (force || getCurrentModelSource() !== 'manual')

        if (shouldSeedComposer) {
          setCurrentReasoningEffort(reasoning)
          setCurrentFastMode(FAST_TIERS.has(tier.toLowerCase()))
        }

        setCurrentServiceTier(prev => (activeSessionIdRef.current ? prev : tier))

        setVoiceMaxRecordingSeconds(recordingLimit(config.voice?.max_recording_seconds))
        setVoiceBargeInEnabled(config.voice?.barge_in !== false)
        setVoiceSemanticTurnEnabled(config.voice?.semantic_turn !== false)
        setWakeWordConfig(normalizeWakeWordConfig(config.voice?.wake_word))
        setSttEnabled(config.stt?.enabled !== false)
        setSttProvider((config.stt?.provider ?? '').trim())
        setTtsProvider((config.tts?.provider ?? '').trim())
        setStreamingSttProvider((config.stt?.streaming?.provider ?? '').trim())
        setStreamingSttEnabled(config.stt?.streaming?.enabled !== false && Boolean(config.stt?.streaming?.provider))
        setTerminalFontFamilyFromConfig(config.terminal?.font_family)
        applyAutoSpeakFromConfig(config)
        applyVoiceStopPhraseFromConfig(config)
        applyThinkingSoundFromConfig(config)
      } catch {
        // Config is nice-to-have; chat still works without it.
      }
    },
    [activeSessionIdRef, refreshProjectBranch]
  )

  return {
    refreshHermesConfig,
    streamingSttEnabled,
    streamingSttProvider,
    sttEnabled,
    sttProvider,
    ttsProvider,
    voiceBargeInEnabled,
    voiceMaxRecordingSeconds,
    voiceSemanticTurnEnabled,
    wakeWordConfig
  }
}
