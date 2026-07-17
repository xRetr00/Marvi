import { AnimatePresence, motion, useReducedMotion } from 'motion/react'

import type { IslandCard } from '@/lib/island-queue'
import type { IslandWorkItem, IslandWorkState } from '@/lib/island-work'

interface IslandWorkPanelProps {
  card: IslandCard | null
  work: IslandWorkState | null
}

const MAX_ROWS = 3

export function IslandWorkPanel({ card, work }: IslandWorkPanelProps) {
  const reducedMotion = useReducedMotion()
  const rows = work?.items.slice(0, MAX_ROWS) ?? []
  const hidden = Math.max(0, (work?.items.length ?? 0) - rows.length)
  const done = work?.items.filter(item => item.state === 'done').length ?? 0
  const total = work?.items.length ?? 0
  const progress = total ? Math.max(0.08, done / total) : work?.active ? 0.12 : 1

  return (
    <AnimatePresence mode="wait">
      {card || work ? (
        <motion.aside
          animate={{ opacity: 1, x: 0, filter: 'blur(0px)' }}
          aria-live="polite"
          initial={reducedMotion ? false : { opacity: 0, x: -22, filter: 'blur(8px)' }}
          key={card ? `card:${card.id}` : 'work'}
          style={{
            position: 'absolute',
            top: 42,
            left: 12,
            width: 338,
            overflow: 'hidden',
            border: '1px solid rgba(255,255,255,0.1)',
            borderRadius: 20,
            color: '#f5f5f7',
            background:
              'radial-gradient(circle at 0% 0%, rgba(110,168,255,0.13), transparent 42%), linear-gradient(155deg, rgba(14,14,18,0.98), rgba(2,2,4,0.97))',
            boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.08), 0 20px 54px rgba(0,0,0,0.48)',
            fontFamily: 'system-ui, -apple-system, sans-serif',
            pointerEvents: 'none'
          }}
          transition={reducedMotion ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 34, mass: 0.75 }}
        >
          {card ? (
            <SideCard card={card} />
          ) : work ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '11px 13px 8px' }}>
                <motion.span
                  animate={work.active && !reducedMotion ? { rotate: 360 } : undefined}
                  style={{
                    width: 18,
                    height: 18,
                    borderRadius: '50%',
                    background: 'conic-gradient(from 210deg, #6ea8ff, #b57eff, #56d9ff, #6ea8ff)',
                    boxShadow: '0 0 16px rgba(110,168,255,0.42)'
                  }}
                  transition={{ duration: 5, ease: 'linear', repeat: Infinity }}
                />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div
                    style={{ fontSize: 9, fontWeight: 700, letterSpacing: '0.14em', color: 'rgba(255,255,255,0.42)' }}
                  >
                    MARVI · LIVE WORK
                  </div>
                  <div
                    style={{
                      marginTop: 1,
                      overflow: 'hidden',
                      fontSize: 13,
                      fontWeight: 650,
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap'
                    }}
                  >
                    {work.title}
                  </div>
                </div>
                <span style={{ fontSize: 10, fontVariantNumeric: 'tabular-nums', color: 'rgba(255,255,255,0.48)' }}>
                  {done}/{total}
                </span>
              </div>
              <div
                style={{
                  height: 2,
                  margin: '0 13px 6px',
                  overflow: 'hidden',
                  borderRadius: 999,
                  background: 'rgba(255,255,255,0.07)'
                }}
              >
                <motion.div
                  animate={{ width: `${progress * 100}%` }}
                  style={{
                    height: '100%',
                    borderRadius: 999,
                    background: 'linear-gradient(90deg, #6ea8ff, #b57eff, #56d9ff)'
                  }}
                  transition={reducedMotion ? { duration: 0 } : { type: 'spring', stiffness: 240, damping: 28 }}
                />
              </div>
              <div style={{ padding: '2px 9px 8px' }}>
                {rows.map((item, index) => (
                  <WorkRow
                    index={index}
                    item={item}
                    key={item.id}
                    last={index === rows.length - 1 && hidden === 0}
                    reducedMotion={Boolean(reducedMotion)}
                  />
                ))}
                {hidden > 0 ? (
                  <div style={{ padding: '4px 7px 2px 29px', fontSize: 10, color: 'rgba(255,255,255,0.38)' }}>
                    +{hidden} more in the Desktop task view
                  </div>
                ) : null}
              </div>
            </>
          ) : null}
        </motion.aside>
      ) : null}
    </AnimatePresence>
  )
}

function WorkRow({
  item,
  index,
  last,
  reducedMotion
}: {
  index: number
  item: IslandWorkItem
  last: boolean
  reducedMotion: boolean
}) {
  const active = item.state === 'running'

  return (
    <motion.div
      animate={{ opacity: 1, x: 0 }}
      initial={reducedMotion ? false : { opacity: 0, x: -8 }}
      style={{
        position: 'relative',
        display: 'flex',
        minHeight: 29,
        alignItems: 'center',
        gap: 9,
        borderRadius: 10,
        padding: '5px 7px',
        background: active ? 'rgba(110,168,255,0.075)' : 'transparent'
      }}
      transition={reducedMotion ? { duration: 0 } : { delay: index * 0.035, duration: 0.18 }}
    >
      {!last ? (
        <span
          style={{
            position: 'absolute',
            top: 20,
            bottom: -10,
            left: 13,
            width: 1,
            background: 'rgba(255,255,255,0.09)'
          }}
        />
      ) : null}
      <StatusMark reducedMotion={reducedMotion} state={item.state} />
      <span
        style={{
          minWidth: 0,
          flex: 1,
          overflow: 'hidden',
          color: item.state === 'done' ? 'rgba(255,255,255,0.48)' : 'rgba(255,255,255,0.86)',
          fontSize: 12,
          textDecoration: item.state === 'done' ? 'line-through' : 'none',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap'
        }}
      >
        {item.title}
      </span>
      {item.meta ? (
        <span
          style={{
            maxWidth: 84,
            overflow: 'hidden',
            fontSize: 9,
            color: 'rgba(255,255,255,0.34)',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap'
          }}
        >
          {item.meta}
        </span>
      ) : null}
    </motion.div>
  )
}

function StatusMark({ reducedMotion, state }: { reducedMotion: boolean; state: IslandWorkItem['state'] }) {
  const color =
    state === 'done' ? '#5cd97e' : state === 'failed' ? '#ff6b78' : state === 'running' ? '#6ea8ff' : '#73737d'

  return (
    <motion.span
      animate={
        state === 'running' && !reducedMotion ? { opacity: [0.45, 1, 0.45], scale: [0.88, 1.08, 0.88] } : undefined
      }
      style={{
        position: 'relative',
        zIndex: 1,
        display: 'grid',
        width: 13,
        height: 13,
        flexShrink: 0,
        placeItems: 'center',
        border: `1.5px solid ${color}`,
        borderRadius: '50%',
        background: '#08080b',
        color,
        fontSize: 8,
        lineHeight: 1
      }}
      transition={{ duration: 1.3, ease: 'easeInOut', repeat: Infinity }}
    >
      {state === 'done' ? '✓' : state === 'failed' ? '×' : state === 'running' ? '•' : ''}
    </motion.span>
  )
}

function SideCard({ card }: { card: IslandCard }) {
  const accent = card.kind === 'result' ? '#5cd97e' : '#6ea8ff'

  return (
    <div style={{ padding: '13px 15px 15px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ color: accent, fontSize: 14 }}>✦</span>
        <span
          style={{ fontSize: 9, fontWeight: 750, letterSpacing: '0.14em', color: accent, textTransform: 'uppercase' }}
        >
          {card.title || (card.kind === 'result' ? 'Result' : 'Marvi')}
        </span>
      </div>
      {card.body ? (
        <p style={{ margin: '8px 0 0', color: 'rgba(255,255,255,0.9)', fontSize: 13, lineHeight: 1.48 }}>{card.body}</p>
      ) : null}
    </div>
  )
}
