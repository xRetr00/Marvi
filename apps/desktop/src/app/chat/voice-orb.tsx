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

export function VoiceOrb({ className, level, phase, size = '17rem' }: { className?: string; level: number; phase: VoicePhase; size?: string }) {
  const [c1, c2, c3] = voiceOrbPalette(phase)
  const amplitude = Math.max(0, Math.min(1, level))

  return (
    <div
      aria-hidden
      className={cn('marvi-voice-orb', className)}
      style={
        {
          '--voice-orb-c1': c1,
          '--voice-orb-c2': c2,
          '--voice-orb-c3': c3,
          '--voice-orb-contrast': 1.85 + amplitude * 0.45,
          '--voice-orb-duration': `${Math.max(6, 15 - amplitude * 7)}s`,
          '--voice-orb-saturation': 1.15 + amplitude * 0.35,
          height: size,
          width: size
        } as CSSProperties
      }
    >
      <style>{`
        @property --marvi-orb-angle {
          syntax: '<angle>';
          inherits: false;
          initial-value: 0deg;
        }

        .marvi-voice-orb {
          display: grid;
          grid-template-areas: 'stack';
          position: relative;
          overflow: hidden;
          border-radius: 50%;
          background: radial-gradient(circle at 46% 52%, rgba(255,255,255,.08), rgba(255,255,255,.02) 30%, transparent 68%);
          box-shadow: 0 24px 90px color-mix(in srgb, var(--voice-orb-c3) 30%, transparent);
          isolation: isolate;
        }

        .marvi-voice-orb::before,
        .marvi-voice-orb::after {
          content: '';
          display: block;
          grid-area: stack;
          width: 100%;
          height: 100%;
          border-radius: 50%;
        }

        .marvi-voice-orb::before {
          background:
            conic-gradient(from calc(var(--marvi-orb-angle) * 1.2) at 30% 65%, var(--voice-orb-c3) 0deg, transparent 45deg 315deg, var(--voice-orb-c3) 360deg),
            conic-gradient(from calc(var(--marvi-orb-angle) * .8) at 70% 35%, var(--voice-orb-c2) 0deg, transparent 60deg 300deg, var(--voice-orb-c2) 360deg),
            conic-gradient(from calc(var(--marvi-orb-angle) * -1.5) at 65% 75%, var(--voice-orb-c1) 0deg, transparent 90deg 270deg, var(--voice-orb-c1) 360deg),
            conic-gradient(from calc(var(--marvi-orb-angle) * 2.1) at 25% 25%, var(--voice-orb-c2) 0deg, transparent 30deg 330deg, var(--voice-orb-c2) 360deg),
            conic-gradient(from calc(var(--marvi-orb-angle) * -.7) at 80% 80%, var(--voice-orb-c1) 0deg, transparent 45deg 315deg, var(--voice-orb-c1) 360deg),
            radial-gradient(ellipse 120% 80% at 40% 60%, var(--voice-orb-c3), transparent 52%);
          filter: blur(20px) contrast(var(--voice-orb-contrast)) saturate(var(--voice-orb-saturation));
          animation: marvi-orb-rotate var(--voice-orb-duration) linear infinite;
          transform: translateZ(0) scale(1.14);
          will-change: transform, filter;
        }

        .marvi-voice-orb::after {
          background:
            radial-gradient(circle at 38% 30%, rgba(255,255,255,.34), transparent 23%),
            radial-gradient(circle at 52% 58%, transparent 24%, rgba(0,0,0,.1) 68%, rgba(0,0,0,.24));
          mix-blend-mode: overlay;
        }

        @keyframes marvi-orb-rotate {
          from { --marvi-orb-angle: 0deg; }
          to { --marvi-orb-angle: 360deg; }
        }

        @media (prefers-reduced-motion: reduce) {
          .marvi-voice-orb::before { animation: none; }
        }
      `}</style>
    </div>
  )
}
