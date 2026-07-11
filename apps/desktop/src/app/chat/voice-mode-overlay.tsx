import { useStore } from '@nanostores/react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

import { requestVoiceToggle } from '@/app/chat/composer/focus'
import { $conversation, $voiceState, type VoicePhase } from '@/store/voice-presence'

const PRESENTATION: Record<VoicePhase, { color: string; label: string }> = {
  off: { color: '#8b7cff', label: 'Ready' },
  wake: { color: '#68b7ff', label: 'Listening' },
  listening: { color: '#68b7ff', label: 'Listening' },
  transcribing: { color: '#a78bfa', label: 'Understanding' },
  thinking: { color: '#ff9c72', label: 'Thinking deeper' },
  speaking: { color: '#76e6b5', label: 'Speaking' }
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
            className="relative size-[min(34vw,17rem)] min-h-48 min-w-48"
            transition={{ duration: voice.phase === 'listening' ? 0.1 : 3.2, ease: 'easeInOut', repeat: voice.phase === 'listening' ? 0 : Infinity }}
          >
            <motion.div
              animate={reducedMotion ? undefined : { rotate: 360 }}
              className="absolute inset-0 rounded-full blur-[2px]"
              style={{
                background: `conic-gradient(from 35deg, ${presentation.color}, #8b5cf6 24%, #ef74c8 48%, #4ecdc4 72%, ${presentation.color})`,
                boxShadow: `0 0 72px color-mix(in srgb, ${presentation.color} 38%, transparent)`
              }}
              transition={{ duration: 9, ease: 'linear', repeat: Infinity }}
            />
            <motion.div
              animate={reducedMotion ? undefined : { borderRadius: ['48% 52% 44% 56%', '56% 44% 54% 46%', '48% 52% 44% 56%'], rotate: [0, -18, 0] }}
              className="absolute inset-[7%] bg-[radial-gradient(circle_at_34%_28%,rgba(255,255,255,0.88),rgba(255,255,255,0.08)_22%,rgba(13,16,26,0.82)_64%)] shadow-[inset_-28px_-24px_58px_rgba(4,5,12,0.62),inset_18px_14px_42px_rgba(255,255,255,0.13)]"
              transition={{ duration: 5.5, ease: 'easeInOut', repeat: Infinity }}
            />
            <div className="absolute inset-[19%] rounded-full bg-[radial-gradient(circle_at_42%_35%,rgba(255,255,255,0.18),rgba(4,6,14,0.76)_58%,rgba(0,0,0,0.94))] blur-[1px]" />
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
