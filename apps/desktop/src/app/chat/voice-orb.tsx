import type { CSSProperties } from 'react'

import { cn } from '@/lib/utils'
import type { VoicePhase } from '@/store/voice-presence'

const PALETTES: Record<VoicePhase, readonly [string, string, string]> = {
  off: ['oklch(75% 0.15 350)', 'oklch(80% 0.12 200)', 'oklch(78% 0.14 280)'],
  wake: ['oklch(78% 0.18 342)', 'oklch(82% 0.13 205)', 'oklch(72% 0.2 290)'],
  listening: ['oklch(76% 0.19 345)', 'oklch(82% 0.14 205)', 'oklch(73% 0.21 292)'],
  transcribing: ['oklch(72% 0.21 294)', 'oklch(80% 0.13 220)', 'oklch(77% 0.17 330)'],
  thinking: ['oklch(76% 0.2 35)', 'oklch(70% 0.23 320)', 'oklch(67% 0.22 285)'],
  speaking: ['oklch(81% 0.14 170)', 'oklch(80% 0.14 215)', 'oklch(74% 0.19 285)']
}

export function voiceOrbPalette(phase: VoicePhase) {
  return PALETTES[phase]
}

export function VoiceOrb({
  className,
  level,
  phase,
  size = '17rem'
}: {
  className?: string
  level: number
  phase: VoicePhase
  size?: string
}) {
  const [c1, c2, c3] = voiceOrbPalette(phase)
  const amplitude = Math.max(0, Math.min(1, level))

  const energy =
    phase === 'listening' || phase === 'wake' ? Math.max(0.22, amplitude) : phase === 'speaking' ? 0.68 : 0.38

  return (
    <div
      aria-hidden
      className={cn('marvi-voice-orb', className)}
      data-phase={phase}
      style={
        {
          '--voice-orb-c1': c1,
          '--voice-orb-c2': c2,
          '--voice-orb-c3': c3,
          '--voice-orb-energy': energy,
          '--voice-orb-level': amplitude,
          '--voice-orb-tempo': `${Math.max(5.5, 13 - energy * 6)}s`,
          height: size,
          width: size
        } as CSSProperties
      }
    >
      <span className="marvi-voice-orb__aura" />
      <span className="marvi-voice-orb__ring marvi-voice-orb__ring--one" />
      <span className="marvi-voice-orb__ring marvi-voice-orb__ring--two" />
      <span className="marvi-voice-orb__ring marvi-voice-orb__ring--three" />
      <span className="marvi-voice-orb__shell">
        <span className="marvi-voice-orb__plasma marvi-voice-orb__plasma--one" />
        <span className="marvi-voice-orb__plasma marvi-voice-orb__plasma--two" />
        <span className="marvi-voice-orb__plasma marvi-voice-orb__plasma--three" />
        <span className="marvi-voice-orb__depth" />
        <span className="marvi-voice-orb__glint" />
      </span>
    </div>
  )
}
