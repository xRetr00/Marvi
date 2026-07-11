import { useStore } from '@nanostores/react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

import { requestVoiceToggle } from '@/app/chat/composer/focus'
import { $conversation, $voiceState, type VoicePhase } from '@/store/voice-presence'

import { VoiceOrb } from './voice-orb'

const PRESENTATION: Record<VoicePhase, { label: string }> = {
  off: { label: 'Ready' },
  wake: { label: 'Listening' },
  listening: { label: 'Listening' },
  transcribing: { label: 'Understanding' },
  thinking: { label: 'Thinking deeper' },
  speaking: { label: 'Speaking' }
}

export function voiceModePresentation(phase: VoicePhase) {
  return PRESENTATION[phase]
}

export function VoiceModeOverlay() {
  const conversation = useStore($conversation)
  const voice = useStore($voiceState)
  const reducedMotion = useReducedMotion()
  const presentation = voiceModePresentation(voice.phase)
  const caption = voice.phase === 'speaking' ? voice.caption : voice.userCaption
  const level = Math.max(0, Math.min(1, voice.level))

  return (
    <AnimatePresence>
      {conversation.active ? (
        <motion.div
          animate={{ opacity: 1 }}
          aria-label="Voice conversation"
          className="absolute inset-0 z-40 flex flex-col items-center justify-center overflow-hidden bg-[rgba(8,9,13,0.94)] px-8 text-center backdrop-blur-2xl"
          exit={{ opacity: 0 }}
          initial={{ opacity: 0 }}
          role="dialog"
          transition={{ duration: reducedMotion ? 0 : 0.28 }}
        >
          <div aria-hidden className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_48%,rgba(72,75,130,0.16),transparent_42%)]" />

          <motion.div
            animate={
              reducedMotion
                ? undefined
                : {
                    scale: voice.phase === 'listening' ? 1 + level * 0.09 : [0.98, 1.03, 0.98],
                    y: voice.phase === 'thinking' ? [0, -5, 0] : 0
                  }
            }
            className="relative size-[min(38vw,19rem)] min-h-48 min-w-48"
            transition={{ duration: voice.phase === 'listening' ? 0.1 : 3.2, ease: 'easeInOut', repeat: voice.phase === 'listening' ? 0 : Infinity }}
          >
            <VoiceOrb className="size-full" level={level} phase={voice.phase} size="100%" />
          </motion.div>

          <div aria-live="polite" className="relative mt-8 min-h-24 max-w-xl" role="status">
            <div className="text-sm font-medium tracking-wide text-white/72">{presentation.label}</div>
            {caption ? <div className="mt-3 line-clamp-3 text-balance text-lg leading-relaxed text-white/92">{caption}</div> : null}
            {voice.phase === 'speaking' && voice.bargeable ? <div className="mt-3 text-xs text-white/40">Speak to interrupt</div> : null}
          </div>

          <button
            className="relative mt-7 rounded-full border border-white/12 bg-white/7 px-5 py-2 text-xs font-medium text-white/64 transition hover:bg-white/12 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-white/70"
            onClick={requestVoiceToggle}
            type="button"
          >
            End voice mode
          </button>
        </motion.div>
      ) : null}
    </AnimatePresence>
  )
}
