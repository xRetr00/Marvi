import { motion, useReducedMotion } from 'motion/react'

import type { IslandWorkItem, IslandWorkState } from '@/lib/island-work'

interface IslandWorkContentProps {
  onCollapse: () => void
  work: IslandWorkState
}

const MAX_ROWS = 3

export function IslandWorkContent({ onCollapse, work }: IslandWorkContentProps) {
  const reducedMotion = Boolean(useReducedMotion())
  const rows = work.items.slice(0, MAX_ROWS)
  const hidden = Math.max(0, work.items.length - rows.length)
  const done = work.items.filter(item => item.state === 'done').length
  const progress = work.items.length ? Math.max(0.08, done / work.items.length) : work.active ? 0.12 : 1

  return (
    <div style={{ width: 324 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, paddingBottom: 8 }}>
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
          <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: '0.14em', color: 'rgba(255,255,255,0.42)' }}>
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
          {done}/{work.items.length}
        </span>
        <button aria-label="Collapse work card" onClick={onCollapse} style={quietButtonStyle} type="button">
          ⌃
        </button>
      </div>
      <div
        style={{
          height: 2,
          marginBottom: 6,
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
      {rows.map((item, index) => (
        <WorkRow
          index={index}
          item={item}
          key={item.id}
          last={index === rows.length - 1 && hidden === 0}
          reducedMotion={reducedMotion}
        />
      ))}
      {hidden > 0 ? (
        <div style={{ padding: '4px 7px 0 29px', fontSize: 10, color: 'rgba(255,255,255,0.38)' }}>
          +{hidden} more in the Desktop task view
        </div>
      ) : null}
    </div>
  )
}

const quietButtonStyle = {
  display: 'grid',
  width: 24,
  height: 24,
  padding: 0,
  border: '1px solid rgba(255,255,255,0.08)',
  borderRadius: 9,
  placeItems: 'center',
  color: 'rgba(255,255,255,0.48)',
  background: 'rgba(255,255,255,0.035)',
  cursor: 'pointer',
  fontSize: 13
} as const

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
