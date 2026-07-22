import { useStore } from '@nanostores/react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { CSSProperties, PropsWithChildren } from 'react'

import { requestVoiceToggle } from '@/app/chat/composer/focus'
import { VoiceSpeakerBadge } from '@/components/voice-speaker-badge'
import { cn } from '@/lib/utils'
import { $voiceModeActive, $voiceState, type VoicePhase } from '@/store/voice-presence'

import { VoiceOrb } from './voice-orb'

const PRESENTATION: Record<VoicePhase, { label: string }> = {
  off: { label: 'Ready' },
  wake: { label: "I'm here" },
  listening: { label: 'Listening' },
  transcribing: { label: 'Understanding' },
  thinking: { label: 'Thinking it through' },
  speaking: { label: 'Speaking' }
}

export function voiceModePresentation(phase: VoicePhase) {
  return PRESENTATION[phase]
}

export function voiceModeCaption(voice: Pick<ReturnType<typeof $voiceState.get>, 'caption' | 'phase' | 'userCaption'>) {
  return voice.phase === 'speaking' || voice.phase === 'thinking' ? voice.caption : voice.userCaption
}

/** Replaces the transcript only; the app shell, header, and composer never unmount. */
export function VoiceModeViewport({ children }: PropsWithChildren) {
  const active = useStore($voiceModeActive)
  const reducedMotion = useReducedMotion()
  const duration = reducedMotion ? 0 : 0.18

  return (
    <AnimatePresence initial={false} mode="wait">
      {active ? (
        <VoiceModeStage key="voice-stage" />
      ) : (
        <motion.div
          animate={{ opacity: 1 }}
          className="h-full min-h-0"
          exit={{ opacity: 0 }}
          initial={{ opacity: 0 }}
          key="voice-transcript"
          transition={{ duration }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

export function VoiceModeStage() {
  const voice = useStore($voiceState)
  const reducedMotion = useReducedMotion()
  const presentation = voiceModePresentation(voice.phase)
  const caption = voiceModeCaption(voice)
  const level = Math.max(0, Math.min(1, voice.level))
  const transition = reducedMotion ? { duration: 0 } : { duration: 0.22, ease: 'easeOut' as const }

  return (
    <motion.section
      animate={{ opacity: 1 }}
      aria-label="Voice conversation"
      className="relative grid h-full min-h-0 w-full grid-rows-[minmax(5.5rem,0.65fr)_minmax(18rem,1.35fr)] overflow-hidden px-6 pb-[calc(var(--composer-measured-height)+1.5rem)] pt-5 text-center"
      data-phase={voice.phase}
      data-slot="voice-mode-stage"
      exit={{ opacity: 0, scale: reducedMotion ? 1 : 0.99 }}
      initial={{ opacity: 0 }}
      role="region"
      style={{ '--voice-stage-level': level } as CSSProperties}
      transition={{ duration: reducedMotion ? 0 : 0.24 }}
    >
      <div aria-hidden className="marvi-voice-stage__field" />

      <motion.header
        animate={{ opacity: 1, y: 0 }}
        className="relative z-10 flex min-h-0 flex-col items-center justify-end"
        initial={{ opacity: 0, y: reducedMotion ? 0 : -12 }}
        transition={{ delay: reducedMotion ? 0 : 0.12, duration: reducedMotion ? 0 : 0.5, ease: 'easeOut' }}
      >
        <div className="font-['Collapse'] text-[clamp(4rem,10vw,8.75rem)] font-bold uppercase leading-[0.78] tracking-[0.08em] text-foreground/90 mix-blend-plus-lighter">
          MARVI
        </div>
        <div className="mt-3 text-[0.58rem] font-semibold uppercase tracking-[0.24em] text-muted-foreground/50">
          by NeuRetro Labs
        </div>
      </motion.header>

      <div className="relative z-10 flex min-h-0 flex-col items-center justify-center">
        <motion.div
          animate={{
            opacity: 1,
            scale: voice.phase === 'speaking' ? 1.025 : voice.phase === 'thinking' ? 0.975 : 1,
            y: 0
          }}
          className="relative size-[clamp(13.5rem,25vw,20rem)] shrink-0"
          initial={{ opacity: reducedMotion ? 1 : 0.15, scale: reducedMotion ? 1 : 0.12, y: reducedMotion ? 0 : 180 }}
          style={{ transformOrigin: '50% 140%' }}
          transition={
            reducedMotion ? { duration: 0 } : { damping: 21, delay: 0.05, mass: 0.85, stiffness: 155, type: 'spring' }
          }
        >
          <div aria-hidden className="marvi-voice-stage__tether" />
          <VoiceOrb className="size-full" level={level} phase={voice.phase} size="100%" />
          <div aria-hidden className="marvi-voice-stage__horizon">
            {Array.from({ length: 9 }, (_, index) => (
              <span key={index} style={{ '--voice-wave-index': index } as CSSProperties} />
            ))}
          </div>
        </motion.div>

        <motion.div
          animate={{ opacity: 1, y: 0 }}
          aria-live="polite"
          className="marvi-voice-stage__readout relative mt-4 min-h-20 w-full max-w-xl"
          initial={{ opacity: 0, y: reducedMotion ? 0 : 14 }}
          role="status"
          transition={{ delay: reducedMotion ? 0 : 0.36, duration: reducedMotion ? 0 : 0.35 }}
        >
          <div className="flex min-h-7 flex-wrap items-center justify-center gap-2">
            <AnimatePresence initial={false} mode="popLayout">
              <motion.div
                animate={{ filter: 'blur(0px)', opacity: 1, y: 0 }}
                className="flex items-center gap-2 text-sm font-medium tracking-wide text-foreground/78"
                exit={{ filter: reducedMotion ? 'blur(0px)' : 'blur(4px)', opacity: 0, y: reducedMotion ? 0 : -5 }}
                initial={{ filter: reducedMotion ? 'blur(0px)' : 'blur(4px)', opacity: 0, y: reducedMotion ? 0 : 5 }}
                key={`${voice.phase}:${voice.label ?? presentation.label}`}
                transition={transition}
              >
                <span className="marvi-voice-stage__status-dot" data-phase={voice.phase} />
                {voice.label ?? presentation.label}
              </motion.div>
            </AnimatePresence>
            {voice.speakerBadge ? <VoiceSpeakerBadge name={voice.speakerName} speaker={voice.speakerBadge} /> : null}
            {voice.activity ? (
              <span className="rounded-full border border-white/8 bg-white/5 px-2.5 py-1 text-[0.68rem] font-medium text-foreground/55">
                {voice.activity.label}
              </span>
            ) : null}
            {voice.deepWorking ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-300/15 bg-amber-300/8 px-2.5 py-1 text-[0.68rem] font-medium text-amber-100/65">
                <span className="size-1.5 animate-pulse rounded-full bg-amber-300/80" />
                {voice.deepMode === 'delegating' ? 'Background agent active' : 'Background task active'}
              </span>
            ) : null}
          </div>

          <AnimatePresence initial={false} mode="wait">
            <motion.div
              animate={{ opacity: 1, y: 0 }}
              className={cn(
                'mx-auto mt-2 line-clamp-2 min-h-6 max-w-lg text-balance leading-relaxed',
                caption ? 'text-base text-foreground/80' : 'text-xs text-muted-foreground/42',
                voice.phase === 'listening' && voice.captionIgnored && 'text-foreground/40 line-through'
              )}
              exit={{ opacity: 0, y: reducedMotion ? 0 : -4 }}
              initial={{ opacity: 0, y: reducedMotion ? 0 : 4 }}
              key={`${voice.phase}:${caption ? 'caption' : voice.bargeable}`}
              transition={transition}
            >
              {caption ??
                (voice.phase === 'speaking' && voice.bargeable
                  ? 'Speak whenever you want to interrupt'
                  : 'Voice mode is live')}
            </motion.div>
          </AnimatePresence>

          <button
            className="mt-3 rounded-full border border-white/8 bg-white/4 px-3.5 py-1.5 text-[0.68rem] font-medium text-foreground/45 transition-colors hover:bg-white/8 hover:text-foreground/80 focus-visible:outline-2 focus-visible:outline-offset-3 focus-visible:outline-foreground/60"
            onClick={() => requestVoiceToggle()}
            type="button"
          >
            End voice mode
          </button>
        </motion.div>
      </div>
    </motion.section>
  )
}
