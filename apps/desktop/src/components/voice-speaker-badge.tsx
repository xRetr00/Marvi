import { cn } from '@/lib/utils'

export type VoiceSpeakerBadgeSpeaker = 'guest' | 'unknown'

/**
 * Small, unobtrusive pill shown while duplex speaker ID (see
 * docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md
 * section 4) attributes the live utterance to someone other than the
 * enrolled owner. Deliberately tiny/muted — this is a hint, not an alert —
 * and deliberately silent for the owner: you don't need a badge telling you
 * that you are you.
 *
 * Shared across every voice surface (the wake-word island, the hands-free
 * voice-mode overlay/orb, and the composer's inline voice status) so the
 * indicator reads identically everywhere duplex speaker ID can surface it.
 */
export function VoiceSpeakerBadge({
  speaker,
  variant = 'default',
  className
}: {
  speaker: VoiceSpeakerBadgeSpeaker
  /** `dark` matches the island/orb's always-dark chrome; `default` follows the app's light/dark theme tokens. */
  variant?: 'dark' | 'default'
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[9px] font-semibold tracking-wider uppercase',
        variant === 'dark' ? 'border-white/18 text-white/45' : 'border-border/60 text-muted-foreground',
        className
      )}
    >
      {speaker === 'guest' ? 'guest' : 'unknown voice'}
    </span>
  )
}
