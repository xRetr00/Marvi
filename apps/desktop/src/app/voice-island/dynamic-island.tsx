import { useEffect, useRef, useState } from 'react'

import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

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

const SEED_HEIGHT = 26
const SEED_RADIUS = 13
const SEED_MIN_WIDTH = 56

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

const SEED_PAD_Y = 6
const SEED_PAD_X = 10

const PILL_SHADOW = [
  'inset 0 1px 0 rgba(255,255,255,0.08)',
  'inset 0 0 0 1px rgba(255,255,255,0.05)',
  'inset 0 -1px 0 rgba(255,255,255,0.02)'
].join(', ')

const SPRING = { type: 'spring', stiffness: 400, damping: 30 } as const

const CONTENT_TRANSITION_MOTION = { duration: 0.22, ease: 'easeOut' } as const
const CONTENT_TRANSITION_INSTANT = { duration: 0 } as const

function phaseLabel(phase: VoicePhase): string {
  switch (phase) {
    case 'wake':
      return 'Listening'
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

function resolveView(state: VoiceState, card: IslandCard | null, summoned: boolean, caption: ActiveCaption | null): IslandView {
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
    // Nothing happening — rest as a tiny ambient seed rather than the fuller
    // idle pill, so Marvi reads as present-but-quiet between turns.
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
  const caption = resolveCaption(state)
  const view = resolveView(state, card, summoned, caption)
  const active = state.phase === 'listening' || state.phase === 'speaking'
  const color = phaseColor(state.phase)
  // While thinking, narrate the agent's current tool action instead of the
  // static "Thinking" label — falls back to it once activity clears (between
  // tools) or for phases that don't carry an activity.
  const narrating = (state.phase === 'thinking' || state.phase === 'transcribing') && Boolean(activity)
  const label = narrating ? activity! : phaseLabel(state.phase)
  // Thinking with a live user caption: the caption becomes the primary line
  // and the activity narration steps aside rather than stacking a third row.
  const showActivityLabel = !(state.phase === 'thinking' && caption)

  const contentTransition = reducedMotion ? CONTENT_TRANSITION_INSTANT : CONTENT_TRANSITION_MOTION
  const springTransition = reducedMotion ? CONTENT_TRANSITION_INSTANT : SPRING

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
    view === 'seed' ? SEED_RADIUS : view === 'idle' ? IDLE_RADIUS : view === 'summon' ? SUMMON_RADIUS : EXPANDED_RADIUS
  const padY = view === 'seed' ? SEED_PAD_Y : PAD_Y
  const padX = view === 'seed' ? SEED_PAD_X : PAD_X

  return (
    <motion.div
      layout
      transition={springTransition}
      style={{
        transformOrigin: 'center top',
        marginTop: 10,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'stretch',
        justifyContent: 'center',
        minWidth,
        maxWidth: view === 'expanded' ? EXPANDED_MAX_WIDTH : view === 'summon' ? SUMMON_WIDTH : undefined,
        minHeight,
        borderRadius: radius,
        background: '#060606',
        boxShadow: PILL_SHADOW,
        padding: `${padY}px ${padX}px`,
        overflow: 'hidden',
        color: '#f2f2f7',
        fontFamily: 'system-ui, -apple-system, sans-serif'
      }}
    >
      <AnimatePresence mode="wait">
        {view === 'summon' ? (
          <motion.div
            key={contentKey}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            transition={contentTransition}
            style={{ display: 'flex', alignItems: 'center', width: '100%' }}
          >
            <SummonBar onSubmit={onSummonSubmit} onCancel={onSummonCancel} />
          </motion.div>
        ) : view === 'seed' ? (
          <motion.div
            key={contentKey}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            transition={contentTransition}
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <SeedDot reducedMotion={Boolean(reducedMotion)} />
          </motion.div>
        ) : view === 'idle' ? (
          <motion.div
            key={contentKey}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            transition={contentTransition}
            style={{ display: 'flex', alignItems: 'center', gap: 10 }}
          >
            <StateDot color={color} active={active} reducedMotion={Boolean(reducedMotion)} />
            <IslandWaveform level={state.level} active={active} width={64} height={24} />
            <span style={{ fontSize: 12, fontWeight: 500, color: 'rgba(255,255,255,0.72)', whiteSpace: 'nowrap' }}>
              {label}
            </span>
          </motion.div>
        ) : (
          <motion.div
            key={contentKey}
            initial={reducedMotion ? false : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={reducedMotion ? undefined : { scale: 0.9, opacity: 0, filter: 'blur(6px)' }}
            transition={contentTransition}
          >
            {card ? (
              <CardContent card={card} onCardAction={onCardAction} />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
                <IslandWaveform level={state.level} active={active} width={300} height={72} />
                {showActivityLabel ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <StateDot color={color} active={active} reducedMotion={Boolean(reducedMotion)} />
                    <span style={{ fontSize: 13, fontWeight: 500, color: 'rgba(255,255,255,0.78)' }}>{label}</span>
                  </div>
                ) : null}
                {caption ? <Caption text={caption.text} who={caption.who} reducedMotion={Boolean(reducedMotion)} /> : null}
                {state.phase === 'speaking' && state.bargeable ? (
                  <InterruptHint reducedMotion={Boolean(reducedMotion)} />
                ) : null}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// Resting-seed indicator: a single dim dot with a slow, soft pulse — no
// waveform, no rAF. This is the "Marvi is here, quietly present" mark shown
// whenever there's nothing active to report.
function SeedDot({ reducedMotion }: { reducedMotion: boolean }) {
  return (
    <motion.span
      animate={reducedMotion ? { opacity: 1, scale: 1 } : { opacity: [0.35, 0.75, 0.35], scale: [0.9, 1, 0.9] }}
      transition={reducedMotion ? undefined : { duration: 2.6, repeat: Infinity, ease: 'easeInOut' }}
      style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        borderRadius: '50%',
        background: '#6b6b78',
        flexShrink: 0
      }}
    />
  )
}

function StateDot({ color, active, reducedMotion }: { color: string; active: boolean; reducedMotion: boolean }) {
  return (
    <motion.span
      animate={
        active && !reducedMotion
          ? { opacity: [0.5, 1, 0.5], scale: [0.9, 1.05, 0.9] }
          : { opacity: 1, scale: 1 }
      }
      transition={active && !reducedMotion ? { duration: 1.6, repeat: Infinity, ease: 'easeInOut' } : undefined}
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: color,
        flexShrink: 0
      }}
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
function Caption({ text, who, reducedMotion }: { text: string; who: 'you' | 'marvi'; reducedMotion: boolean }) {
  const isUser = who === 'you'
  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={`${who}:${text}`}
        initial={reducedMotion ? false : { opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={reducedMotion ? undefined : { opacity: 0 }}
        transition={reducedMotion ? CONTENT_TRANSITION_INSTANT : CONTENT_TRANSITION_MOTION}
        style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, maxWidth: 280 }}
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
            color: isUser ? '#b9b9c9' : '#e6e6f0',
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
        transition={reducedMotion ? undefined : { duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
        style={{ display: 'inline-block', width: 5, height: 5, borderRadius: '50%', background: '#5cd97e', flexShrink: 0 }}
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

// Card content sizes to its text instead of a fixed layout: short bodies get
// a bigger font and hug their width, long bodies shrink and clamp to a few
// lines with an ellipsis so the pill never blows past the expanded max width.
const CARD_MIN_WIDTH = 220
const CARD_LONG_WIDTH = 300

function bodyFontSize(length: number): number {
  if (length <= 44) return 16
  if (length <= 120) return 14
  return 13
}

function bodyLineClamp(length: number): number {
  if (length <= 44) return 2
  if (length <= 120) return 3
  return 4
}

function titleColor(kind: IslandCardKind): string {
  switch (kind) {
    case 'result':
      return 'rgba(140,224,168,0.85)'
    case 'approval':
      return 'rgba(255,255,255,0.5)'
    default:
      return 'rgba(255,255,255,0.5)'
  }
}

function dotColor(kind: IslandCardKind): string | null {
  switch (kind) {
    case 'result':
      return '#5cd97e'
    case 'approval':
      return '#f5b95c'
    default:
      return null
  }
}

function CardContent({ card, onCardAction }: { card: IslandCard; onCardAction: (payload: CardAction) => void }) {
  const dismiss = () => onCardAction({ type: 'dismiss', id: card.id })
  const bodyLength = (card.body ?? '').length
  const long = bodyLength > 120
  const accentDot = dotColor(card.kind)

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        minWidth: CARD_MIN_WIDTH,
        width: long ? CARD_LONG_WIDTH : undefined
      }}
    >
      {card.title && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {accentDot && (
            <span
              style={{
                display: 'inline-block',
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: accentDot,
                flexShrink: 0
              }}
            />
          )}
          <div
            style={{
              fontSize: 11,
              letterSpacing: '0.12em',
              textTransform: 'uppercase',
              color: titleColor(card.kind)
            }}
          >
            {card.title}
          </div>
        </div>
      )}
      {card.body && (
        <div
          style={{
            fontSize: bodyFontSize(bodyLength),
            lineHeight: 1.5,
            color: 'rgba(255,255,255,0.92)',
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
        <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
          {card.actions.map(action => (
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
                background: action.id === 'primary' ? '#2b5bd0' : 'transparent',
                border: '0.5px solid rgba(255,255,255,0.2)',
                color: '#fff',
                borderRadius: 10,
                padding: '8px 0',
                fontSize: 13,
                cursor: 'pointer'
              }}
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
        ref={inputRef}
        value={value}
        onChange={e => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask Marvi…"
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
      />
    </div>
  )
}
