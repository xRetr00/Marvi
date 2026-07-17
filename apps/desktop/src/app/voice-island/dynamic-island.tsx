import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { useEffect, useRef, useState } from 'react'

import { VoiceSpeakerBadge } from '@/components/voice-speaker-badge'
import type { IslandCard, IslandCardKind } from '@/lib/island-queue'
import type { VoicePhase, VoiceState } from '@/store/voice-presence'

import { IslandWaveform } from './island-waveform'

type IslandView = 'seed' | 'idle' | 'expanded' | 'summon'

type CardAction = { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }

interface DynamicIslandProps {
  state: VoiceState
  card: IslandCard | null
  // Short label for the agent's current tool action (e.g. "Searching the
  // web"), shown in place of the static phase label while thinking.
  activity?: string | null
  onCardAction: (payload: CardAction) => void
  // Command bar: summoned via the global hotkey, lets the user type to Marvi
  // from any app.
  summoned?: boolean
  onSummonSubmit?: (text: string) => void
  onSummonCancel?: () => void
}

const SEED_HEIGHT = 34
const SEED_MIN_WIDTH = 112

const IDLE_HEIGHT = 44
const IDLE_RADIUS = 22
const IDLE_MIN_WIDTH = 128

const EXPANDED_MAX_WIDTH = 360
const EXPANDED_RADIUS = 28

const SUMMON_WIDTH = 340
const SUMMON_RADIUS = 22
const SUMMON_HEIGHT = 44

const PAD_Y = 10
const PAD_X = 18

const PILL_SHADOW = [
  'inset 0 1px 0 rgba(255,255,255,0.08)',
  'inset 0 0 0 1px rgba(255,255,255,0.05)',
  'inset 0 -1px 0 rgba(255,255,255,0.02)'
].join(', ')

const APPEAR_SPRING = { type: 'spring', stiffness: 420, damping: 32, mass: 0.78 } as const
const RECESS_SPRING = { type: 'spring', stiffness: 520, damping: 42, mass: 0.72 } as const

const CONTENT_TRANSITION_MOTION = { duration: 0.16, ease: 'easeOut' } as const
const CONTENT_TRANSITION_INSTANT = { duration: 0 } as const

function phaseLabel(phase: VoicePhase): string {
  switch (phase) {
    case 'wake':
      return 'Waking…'

    case 'listening':

    case 'transcribing':
      return 'Listening'

    case 'thinking':
      return 'Thinking'

    case 'speaking':
      return 'Speaking'

    default:
      return 'Ready'
  }
}

function phaseColor(phase: VoicePhase): string {
  switch (phase) {
    case 'wake':

    case 'listening':

    case 'transcribing':
      return '#6ea8ff'

    case 'thinking':
      return '#f5b95c'

    case 'speaking':
      return '#5cd97e'

    default:
      return '#8a8a8e'
  }
}

// Which speaker's words are currently active for the caption line, and the
// text to show. Marvi's spoken caption (TTS) takes priority while she's
// actually speaking; otherwise the user's live/final transcript fills the
// line across the wake/listening/transcribing/thinking phases.
interface ActiveCaption {
  text: string
  who: 'you' | 'marvi'
}

function resolveCaption(state: VoiceState): ActiveCaption | null {
  if (state.phase === 'speaking' && state.caption) {
    return { text: state.caption, who: 'marvi' }
  }

  if (state.userCaption) {
    return { text: state.userCaption, who: 'you' }
  }

  return null
}

function resolveView(
  state: VoiceState,
  card: IslandCard | null,
  summoned: boolean,
  caption: ActiveCaption | null
): IslandView {
  if (summoned) {
    return 'summon'
  }

  if (card) {
    return 'expanded'
  }

  if (state.phase === 'listening' || state.phase === 'speaking') {
    return 'expanded'
  }

  if (caption) {
    // A caption ready to show (e.g. user speech during transcribing/thinking)
    // earns the roomier expanded pill so the words aren't clipped.
    return 'expanded'
  }

  if (state.phase === 'off') {
    return 'seed'
  }

  return 'idle'
}

export function DynamicIsland({
  state,
  card,
  activity,
  onCardAction,
  summoned = false,
  onSummonSubmit,
  onSummonCancel
}: DynamicIslandProps) {
  const reducedMotion = useReducedMotion()

  // `state` already carries whichever session is authoritative — a duplex
  // session (composer hands-free or ambient wake-word, see voice-presence.ts's
  // $voiceState) or the legacy wake-word/conversation derivation — so the
  // island never needs to know which one produced it. `state.label` is only
  // ever set by a duplex session (see DuplexExtras), so its presence doubles
  // as "duplex is driving this frame" without a separate prop.
  const duplexDriven = state.label !== null
  const caption = resolveCaption(state)
  const view = resolveView(state, card, summoned, caption)

  const active =
    state.phase === 'wake' ||
    state.phase === 'listening' ||
    state.phase === 'speaking' ||
    (duplexDriven && state.phase === 'thinking')

  const color = phaseColor(state.phase)
  const level = state.level
  const displayLevel = state.phase === 'wake' ? 0.65 : level
  // While thinking, narrate the agent's current tool action instead of the
  // static "Thinking" label — falls back to it once activity clears (between
  // tools) or for phases that don't carry an activity. Duplex's own labels
  // ("Replying" / "Answering" / "Speaking") always win over tool narration.
  const narrating = !duplexDriven && (state.phase === 'thinking' || state.phase === 'transcribing') && Boolean(activity)
  const label = state.label ?? (narrating ? activity! : phaseLabel(state.phase))
  // Thinking with a live user caption: the caption becomes the primary line
  // and the activity narration steps aside rather than stacking a third row.
  const showActivityLabel = duplexDriven ? true : !(state.phase === 'thinking' && caption)

  const contentTransition = reducedMotion ? CONTENT_TRANSITION_INSTANT : CONTENT_TRANSITION_MOTION
  const springTransition = reducedMotion ? CONTENT_TRANSITION_INSTANT : view === 'seed' ? RECESS_SPRING : APPEAR_SPRING

  // Note: the key intentionally excludes caption text — captions update a
  // few times/sec on streaming partials, and re-keying here would replay the
  // whole pill's enter/exit blur animation on every partial. Only the
  // Caption component's own AnimatePresence (keyed on who+text) should react
  // to text changes.
  const contentKey =
    view === 'summon' ? 'summon' : card ? `card:${card.id}` : `state:${view}:${state.phase}:${narrating ? label : ''}`

  const minWidth =
    view === 'seed' ? SEED_MIN_WIDTH : view === 'idle' ? IDLE_MIN_WIDTH : view === 'summon' ? SUMMON_WIDTH : undefined

  const minHeight = view === 'seed' ? SEED_HEIGHT : view === 'summon' ? SUMMON_HEIGHT : IDLE_HEIGHT

  const radius =
    view === 'seed' ? 18 : view === 'idle' ? IDLE_RADIUS : view === 'summon' ? SUMMON_RADIUS : EXPANDED_RADIUS

  const padY = view === 'seed' ? 0 : PAD_Y
  const padX = view === 'seed' ? 0 : PAD_X

  return (
    <motion.div
      animate={
        view === 'seed'
          ? { y: -6, opacity: 0.88, scaleX: 0.72, scaleY: 0.38 }
          : { y: 8, opacity: 1, scaleX: 1, scaleY: 1 }
      }
      aria-live="polite"
      data-island-view={view}
      initial={reducedMotion ? false : { y: -10, opacity: 0, scaleX: 0.58, scaleY: 0.3 }}
      layout
      role="status"
      style={{
        transformOrigin: 'center top',
        marginTop: 0,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'stretch',
        justifyContent: 'center',
        minWidth,
        maxWidth: view === 'expanded' ? EXPANDED_MAX_WIDTH : view === 'summon' ? SUMMON_WIDTH : undefined,
        minHeight,
        borderRadius: radius,
        background: '#000',
        boxShadow: PILL_SHADOW,
        padding: `${padY}px ${padX}px`,
        overflow: 'hidden',
        color: '#f2f2f7',
        fontFamily: 'system-ui, -apple-system, sans-serif',
        willChange: 'transform, width, height'
      }}
      transition={springTransition}
    >
      <AnimatePresence mode="sync">
        {view === 'summon' ? (
          <motion.div
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            key={contentKey}
            style={{ display: 'flex', alignItems: 'center', width: '100%' }}
            transition={contentTransition}
          >
            <SummonBar onCancel={onSummonCancel} onSubmit={onSummonSubmit} />
          </motion.div>
        ) : view === 'seed' ? (
          <motion.div
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            key={contentKey}
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            transition={contentTransition}
          >
            <motion.span
              animate={
                reducedMotion ? { opacity: 0.55, scaleX: 1 } : { opacity: [0.32, 0.72, 0.32], scaleX: [0.78, 1, 0.78] }
              }
              style={{
                width: 48,
                height: 2,
                borderRadius: 999,
                background:
                  'linear-gradient(90deg, transparent, rgba(110,168,255,0.28), rgba(181,126,255,0.22), transparent)'
              }}
              transition={reducedMotion ? undefined : { duration: 3.2, ease: 'easeInOut', repeat: Infinity }}
            />
          </motion.div>
        ) : view === 'idle' ? (
          <motion.div
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            key={contentKey}
            style={{ display: 'flex', alignItems: 'center', gap: 10 }}
            transition={contentTransition}
          >
            <StateDot active={active} color={color} reducedMotion={Boolean(reducedMotion)} />
            <IslandWaveform active={active} height={24} level={displayLevel} width={64} />
            <span style={{ fontSize: 12, fontWeight: 500, color: 'rgba(255,255,255,0.72)', whiteSpace: 'nowrap' }}>
              {label}
            </span>
          </motion.div>
        ) : (
          <motion.div
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            key={contentKey}
            transition={contentTransition}
          >
            {card ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 7 }}>
                <CardContent card={card} onCardAction={onCardAction} />
                {state.phase === 'speaking' ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                    <IslandWaveform active height={14} level={displayLevel} width={112} />
                    <span style={{ color: 'rgba(255,255,255,0.5)', fontSize: 10, fontWeight: 550 }}>Speaking</span>
                  </div>
                ) : null}
                {state.deepWorking ? (
                  <DeepWorkBadge mode={state.deepMode} reducedMotion={Boolean(reducedMotion)} />
                ) : null}
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
                <IslandWaveform active={active} height={72} level={displayLevel} width={300} />
                {showActivityLabel ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <StateDot active={active} color={color} reducedMotion={Boolean(reducedMotion)} />
                    <span style={{ fontSize: 13, fontWeight: 500, color: 'rgba(255,255,255,0.78)' }}>{label}</span>
                    {state.speakerBadge ? (
                      <VoiceSpeakerBadge name={state.speakerName} speaker={state.speakerBadge} variant="dark" />
                    ) : null}
                  </div>
                ) : null}
                {caption ? (
                  <Caption
                    ignored={caption.who === 'you' && state.captionIgnored}
                    reducedMotion={Boolean(reducedMotion)}
                    text={caption.text}
                    who={caption.who}
                  />
                ) : null}
                {state.phase === 'speaking' && state.bargeable ? (
                  <InterruptHint reducedMotion={Boolean(reducedMotion)} />
                ) : null}
                {state.deepWorking ? (
                  <DeepWorkBadge mode={state.deepMode} reducedMotion={Boolean(reducedMotion)} />
                ) : null}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function StateDot({ color, active, reducedMotion }: { color: string; active: boolean; reducedMotion: boolean }) {
  return (
    <motion.span
      animate={
        active && !reducedMotion ? { opacity: [0.5, 1, 0.5], scale: [0.9, 1.05, 0.9] } : { opacity: 1, scale: 1 }
      }
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: `radial-gradient(circle at 32% 28%, #fff, ${color} 40%, #34255f 78%)`,
        boxShadow: `0 0 12px color-mix(in srgb, ${color} 65%, transparent)`,
        flexShrink: 0
      }}
      transition={active && !reducedMotion ? { duration: 1.6, repeat: Infinity, ease: 'easeInOut' } : undefined}
    />
  )
}

// Live caption of the words being spoken — either Marvi's (TTS, while
// `state.phase === 'speaking'`) or the user's (live streaming partials on
// the Parakeet path, or a final flash on other paths, while listening/
// transcribing/thinking). Styled by speaker so it's obvious who's talking:
// Marvi's line runs brighter, the user's line sits dimmer/muted with a tiny
// "you" affordance. Clamped to two lines so long speech never blows out the
// pill; fades gently on each change, keyed on who+text so a speaker switch
// (user -> Marvi) also gets a clean crossfade rather than a jump-cut.
function Caption({
  text,
  who,
  reducedMotion,
  ignored
}: {
  text: string
  who: 'you' | 'marvi'
  reducedMotion: boolean
  // Voice focus (spec §4): true when this "you" caption was a non-owner
  // utterance filtered out by focus mode -- dim it and strike it through so
  // it reads as "heard but not acted on", not a normal in-flight turn.
  ignored?: boolean
}) {
  const isUser = who === 'you'

  return (
    <AnimatePresence mode="wait">
      <motion.div
        animate={{ opacity: 1 }}
        exit={reducedMotion ? undefined : { opacity: 0 }}
        initial={reducedMotion ? false : { opacity: 0 }}
        key={`${who}:${text}`}
        style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, maxWidth: 280 }}
        transition={reducedMotion ? CONTENT_TRANSITION_INSTANT : CONTENT_TRANSITION_MOTION}
      >
        {isUser && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 500,
              letterSpacing: '0.08em',
              textTransform: 'uppercase',
              color: 'rgba(185,185,201,0.55)'
            }}
          >
            you
          </span>
        )}
        <p
          style={{
            margin: 0,
            fontSize: isUser ? 13 : 14,
            lineHeight: 1.4,
            color: ignored ? 'rgba(185,185,201,0.45)' : isUser ? '#b9b9c9' : '#e6e6f0',
            textDecoration: ignored ? 'line-through' : 'none',
            textAlign: 'center',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden'
          }}
        >
          {text}
        </p>
      </motion.div>
    </AnimatePresence>
  )
}

// Shown while Marvi is speaking and barge-in is armed (duplex phase 3), in
// EVERY mode — hands-free, wake-word, and plain read-aloud all route their
// speaking through the shared playback state that feeds `state.bargeable`. It's
// a voice affordance: the stage stays click-through, so this tells the user
// they can just talk to cut in (barge-in listens through playback).
function InterruptHint({ reducedMotion }: { reducedMotion: boolean }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }}>
      <motion.span
        animate={reducedMotion ? { opacity: 0.7 } : { opacity: [0.3, 0.9, 0.3] }}
        style={{
          display: 'inline-block',
          width: 5,
          height: 5,
          borderRadius: '50%',
          background: '#5cd97e',
          flexShrink: 0
        }}
        transition={reducedMotion ? undefined : { duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
      />
      <span
        style={{
          fontSize: 10,
          fontWeight: 500,
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          color: 'rgba(255,255,255,0.4)'
        }}
      >
        talk to interrupt
      </span>
    </div>
  )
}

// Small, unobtrusive ongoing-work indicator (duplex escalation, spec section
// 2): shown once the server hands back an `escalated` ack and clears once
// its `deep_result` arrives, while the conversation otherwise keeps flowing
// normally (listening/replying to further turns) — this is deliberately NOT
// a blocking state, just a quiet "still working on that" marker.
function DeepWorkBadge({ mode, reducedMotion }: { mode: VoiceState['deepMode']; reducedMotion: boolean }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        marginTop: 2,
        padding: '4px 8px',
        border: '1px solid rgba(245,185,92,0.16)',
        borderRadius: 999,
        background: 'rgba(245,185,92,0.07)'
      }}
    >
      <motion.span
        animate={reducedMotion ? { opacity: 0.7 } : { opacity: [0.3, 0.9, 0.3] }}
        style={{
          display: 'inline-block',
          width: 5,
          height: 5,
          borderRadius: '50%',
          background: '#f5b95c',
          flexShrink: 0
        }}
        transition={reducedMotion ? undefined : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
      />
      <span
        style={{
          fontSize: 10,
          fontWeight: 500,
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          color: 'rgba(255,255,255,0.4)'
        }}
      >
        {mode === 'delegating' ? 'background agent active' : 'background task active'}
      </span>
    </div>
  )
}

// Card content sizes to its text instead of a fixed layout: short bodies get
// a bigger font and hug their width, long bodies shrink and clamp to a few
// lines with an ellipsis so the pill never blows past the expanded max width.
const CARD_MIN_WIDTH = 260
const CARD_LONG_WIDTH = 316

const CARD_META: Record<IslandCardKind, { accent: string; label: string; mark: string; tint: string }> = {
  info: { accent: '#72a7ff', label: 'Marvi', mark: '✦', tint: 'rgba(114,167,255,0.13)' },
  result: { accent: '#63dfa0', label: 'Result', mark: '✓', tint: 'rgba(99,223,160,0.12)' },
  approval: { accent: '#f5bd64', label: 'Confirm', mark: '?', tint: 'rgba(245,189,100,0.13)' }
}

function bodyFontSize(length: number): number {
  if (length <= 44) {
    return 16
  }

  if (length <= 120) {
    return 14
  }

  return 13
}

function bodyLineClamp(length: number): number {
  if (length <= 44) {
    return 2
  }

  if (length <= 120) {
    return 3
  }

  return 4
}

function CardContent({ card, onCardAction }: { card: IslandCard; onCardAction: (payload: CardAction) => void }) {
  const dismiss = () => onCardAction({ type: 'dismiss', id: card.id })
  const bodyLength = (card.body ?? '').length
  const long = bodyLength > 120
  const meta = CARD_META[card.kind]

  return (
    <div
      style={{
        position: 'relative',
        display: 'flex',
        flexDirection: 'column',
        gap: 11,
        minWidth: CARD_MIN_WIDTH,
        width: long ? CARD_LONG_WIDTH : undefined,
        padding: '12px 13px 13px',
        border: `1px solid color-mix(in srgb, ${meta.accent} 22%, rgba(255,255,255,0.08))`,
        borderRadius: 17,
        background: `radial-gradient(circle at 8% 0%, ${meta.tint}, transparent 44%), linear-gradient(145deg, rgba(255,255,255,0.055), rgba(255,255,255,0.018))`,
        boxShadow: `inset 0 1px 0 rgba(255,255,255,0.07), 0 10px 28px ${meta.tint}`
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <span
          style={{
            display: 'grid',
            placeItems: 'center',
            width: 25,
            height: 25,
            borderRadius: 9,
            flexShrink: 0,
            color: meta.accent,
            background: meta.tint,
            boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${meta.accent} 24%, transparent)`,
            fontSize: 12,
            fontWeight: 700
          }}
        >
          {meta.mark}
        </span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div
            style={{
              fontSize: 9,
              fontWeight: 650,
              letterSpacing: '0.13em',
              textTransform: 'uppercase',
              color: meta.accent,
              opacity: 0.82
            }}
          >
            {meta.label}
          </div>
          {card.title && (
            <div
              style={{
                marginTop: 1,
                overflow: 'hidden',
                color: 'rgba(255,255,255,0.86)',
                fontSize: 12,
                fontWeight: 560,
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap'
              }}
            >
              {card.title}
            </div>
          )}
        </div>
        <button
          aria-label="Dismiss card"
          onClick={dismiss}
          style={{
            display: 'grid',
            placeItems: 'center',
            width: 24,
            height: 24,
            padding: 0,
            border: '1px solid rgba(255,255,255,0.08)',
            borderRadius: 9,
            color: 'rgba(255,255,255,0.45)',
            background: 'rgba(255,255,255,0.035)',
            cursor: 'pointer',
            fontSize: 15,
            lineHeight: 1
          }}
          type="button"
        >
          ×
        </button>
      </div>
      {card.body && (
        <div
          style={{
            fontSize: bodyFontSize(bodyLength),
            lineHeight: 1.45,
            color: 'rgba(255,255,255,0.94)',
            letterSpacing: '-0.008em',
            display: '-webkit-box',
            WebkitLineClamp: bodyLineClamp(bodyLength),
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden'
          }}
        >
          {card.body}
        </div>
      )}
      {card.actions?.length ? (
        <div style={{ display: 'flex', gap: 7, marginTop: 1 }}>
          {card.actions.map((action, index) => (
            <button
              key={action.id}
              onClick={() => {
                if (action.value) {
                  onCardAction({ type: 'submit', text: action.value })
                }

                dismiss()
              }}
              style={{
                flex: 1,
                background: index === 0 ? meta.accent : 'rgba(255,255,255,0.045)',
                border: index === 0 ? '1px solid transparent' : '1px solid rgba(255,255,255,0.1)',
                color: index === 0 ? '#07110d' : 'rgba(255,255,255,0.86)',
                borderRadius: 11,
                padding: '8px 10px',
                fontSize: 12,
                fontWeight: 620,
                cursor: 'pointer'
              }}
              type="button"
            >
              {action.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

// Command bar: the summon hotkey morphs the pill into a single-line input
// so the user can type to Marvi from any app. Enter submits via the shared
// card-action channel (already routed to the active session); Escape closes
// without sending.
function SummonBar({ onSubmit, onCancel }: { onSubmit?: (text: string) => void; onCancel?: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [value, setValue] = useState('')

  useEffect(() => {
    // The OS window has to become key first (setFocusable + focus happen in
    // the main process), so focus the input on the next frame.
    const raf = requestAnimationFrame(() => inputRef.current?.focus())

    return () => cancelAnimationFrame(raf)
  }, [])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSubmit?.(value)
      setValue('')
    } else if (e.key === 'Escape') {
      e.preventDefault()
      onCancel?.()
    }
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, width: '100%' }}>
      <span
        style={{
          display: 'inline-block',
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: '#6ea8ff',
          flexShrink: 0
        }}
      />
      <input
        onChange={e => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask Marvi…"
        ref={inputRef}
        style={{
          flex: 1,
          minWidth: 0,
          background: 'transparent',
          border: 'none',
          outline: 'none',
          color: '#f2f2f7',
          fontSize: 14,
          fontFamily: 'inherit'
        }}
        value={value}
      />
    </div>
  )
}
