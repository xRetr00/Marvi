import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

import type { IslandCard } from '@/lib/island-queue'
import type { VoicePhase, VoiceState } from '@/store/voice-presence'

import { IslandWaveform } from './island-waveform'

type IslandView = 'idle' | 'expanded'

type CardAction = { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }

interface DynamicIslandProps {
  state: VoiceState
  card: IslandCard | null
  onCardAction: (payload: CardAction) => void
}

const IDLE_HEIGHT = 44
const IDLE_RADIUS = 22
const IDLE_MIN_WIDTH = 128

const EXPANDED_MAX_WIDTH = 360
const EXPANDED_RADIUS = 28

const PAD_Y = 10
const PAD_X = 18

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
      return 'Listening'
    case 'transcribing':
      return 'Transcribing'
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
      return '#6ea8ff'
    case 'thinking':
      return '#f5b95c'
    case 'speaking':
      return '#5cd97e'
    default:
      return '#8a8a8e'
  }
}

function resolveView(state: VoiceState, card: IslandCard | null): IslandView {
  if (card) {
    return 'expanded'
  }
  if (state.phase === 'listening' || state.phase === 'speaking') {
    return 'expanded'
  }
  return 'idle'
}

export function DynamicIsland({ state, card, onCardAction }: DynamicIslandProps) {
  const reducedMotion = useReducedMotion()
  const view = resolveView(state, card)
  const active = state.phase === 'listening' || state.phase === 'speaking'
  const color = phaseColor(state.phase)
  const label = phaseLabel(state.phase)

  const contentTransition = reducedMotion ? CONTENT_TRANSITION_INSTANT : CONTENT_TRANSITION_MOTION
  const springTransition = reducedMotion ? CONTENT_TRANSITION_INSTANT : SPRING

  const contentKey = card ? `card:${card.id}` : `state:${view}:${state.phase}`

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
        minWidth: view === 'idle' ? IDLE_MIN_WIDTH : undefined,
        maxWidth: view === 'expanded' ? EXPANDED_MAX_WIDTH : undefined,
        minHeight: IDLE_HEIGHT,
        borderRadius: view === 'idle' ? IDLE_RADIUS : EXPANDED_RADIUS,
        background: '#060606',
        boxShadow: PILL_SHADOW,
        padding: `${PAD_Y}px ${PAD_X}px`,
        overflow: 'hidden',
        color: '#f2f2f7',
        fontFamily: 'system-ui, -apple-system, sans-serif'
      }}
    >
      <AnimatePresence mode="wait">
        {view === 'idle' ? (
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
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <StateDot color={color} active={active} reducedMotion={Boolean(reducedMotion)} />
                  <span style={{ fontSize: 13, fontWeight: 500, color: 'rgba(255,255,255,0.78)' }}>{label}</span>
                </div>
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

function CardContent({ card, onCardAction }: { card: IslandCard; onCardAction: (payload: CardAction) => void }) {
  const dismiss = () => onCardAction({ type: 'dismiss', id: card.id })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 220 }}>
      {card.title && (
        <div
          style={{
            fontSize: 11,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            color: 'rgba(255,255,255,0.5)'
          }}
        >
          {card.title}
        </div>
      )}
      {card.body && <div style={{ fontSize: 14, lineHeight: 1.5, color: 'rgba(255,255,255,0.92)' }}>{card.body}</div>}
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
