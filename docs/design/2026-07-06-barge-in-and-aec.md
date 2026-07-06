# Barge-in + Echo Cancellation — current design & the tighter-AEC upgrade

**Date:** 2026-07-06
**Status:** Two-stage barge-in shipped. AEC upgrade = future, not started.
**Related code:** `apps/desktop/src/lib/barge-in-detector.ts`,
`use-voice-conversation.ts` (`armBargeIn`/`disarmBargeIn`),
`use-read-aloud-barge-in.ts`, `voice-echo-guard.ts`, the gapless player in
`voice-playback.ts`.

---

## Why energy barge-in failed (the finding that drove this)

WebRTC's `echoCancellation` (`getUserMedia({ audio: { echoCancellation: true }})`)
**may not register Web Audio `AudioContext` playback as its reference signal** —
and our gapless TTS plays through exactly that. On speakers this gives one of two
bad outcomes:

- Marvi's echo leaks into the mic uncancelled, **or**
- AEC + noiseSuppression + autoGainControl duck *everything* (including the
  user's voice) down to ~**0.02** RMS.

We measured ~0.006–0.02 peaks in `logs/voice-presence.log`. A raw energy
threshold can't survive that — it's architectural, not a tuning problem. AEC is
also browser-wide and needs a few seconds of "training."

## What we shipped: two-stage barge-in (content, not energy)

`startBargeInDetector()` runs for the duration of each playback:

1. **Energy PRE-GATE (free, CPU).** Only feed the STT while the mic rises above
   the echo floor (`ECHO_FLOOR = 0.025`). During Marvi-only playback (~0.02) the
   model stays idle → no GPU contention with TTS.
2. **STT WORD confirmation (Parakeet EOU, GPU only when gated).** The interrupt
   fires on transcribed **words that are not Marvi's own echo**, rejected via
   `isLikelySelfEchoTranscript` (compares against her known TTS text stored by
   `rememberSpokenText`). This keys on *content*, so it works even at 0.02.
3. **Fallback:** energy gate (`BARGE_IN_DEFAULTS`) only when streaming STT is off.

This sidesteps the broken Web-Audio-AEC entirely: we don't need AEC to *remove*
Marvi's voice, because we already *know her words* and ignore them.

### Tunables (current barge-in)
| Knob | File | Default | Effect |
| --- | --- | --- | --- |
| `ECHO_FLOOR` | `barge-in-detector.ts` | 0.025 | Raise if pure echo keeps waking the STT; lower if your voice doesn't feed it |
| `MIN_CONFIRM_CHARS` | `barge-in-detector.ts` | 4 | Higher = fewer false interrupts, misses very short "stop" |
| `BARGE_GRACE_MS` | `barge-in-detector.ts` | 300 | Ignore interrupts in the first N ms of playback (TTS onset) |
| echo-guard `MIN_WORDS` | `voice-echo-guard.ts` | 3 | How many words before a transcript can be judged as self-echo |

Watch `[voice] barge-in partial {chars, echo}` and `barge-in accepted {reason}`
in `logs/voice-presence.log` to tune.

---

## FUTURE UPGRADE: tighter acoustic echo cancellation

Goal: reduce Marvi's echo *in the mic signal itself*, so the STT sees cleaner
user audio and the echo-guard has less to reject. Not required for barge-in to
work (the two-stage design already handles it), but it makes everything crisper
and lets you drop `ECHO_FLOOR`. **Do these in order; stop when it's good enough.**

### Option 1 — Play TTS through an `<audio>` element (most promising, moderate effort)
The core issue is that Chromium's AEC references audio it knows about, and Web
Audio `AudioContext` output often isn't registered as that reference.
`HTMLMediaElement` (`<audio>`) output is more likely to be seen by the AEC loop.

**Steps:**
1. In `voice-playback.ts`, add an alternate sink: instead of scheduling PCM on an
   `AudioContext`, feed the streamed PCM into a `MediaSource`/`SourceBuffer`
   attached to an `<audio>` element, or decode to a Blob URL and play via `<audio>`.
2. Keep the gapless queue/timeline logic; only the *output node* changes.
3. Verify AEC now references it: with `echoCancellation: true`, play a long
   utterance and watch `barge-in partial {echo:true}` — if Marvi's words stop
   appearing in the mic transcript, AEC is cancelling at the source.
4. Trade-off: `<audio>`/MediaSource scheduling is less precise than Web Audio, so
   re-check gaplessness (the `STREAM_*_BUFFER_SECONDS` knobs may need a nudge).

**Risk:** touches the gapless player; keep it behind a flag
(`voice.aec.audio_element_sink`) so it's easy to revert if prosody regresses.

### Option 2 — Route output to a device the AEC monitors
Ensure TTS plays through the **default system output** that WebRTC's AEC probes
(not a separate WASAPI/exclusive device). On Windows, exclusive-mode or a second
output device can bypass the AEC reference. Verify the Electron audio output
device matches the mic's echo-cancellation reference.

### Option 3 — Native / server-side AEC (heaviest, best quality)
If browser AEC stays unreliable, run a real AEC (e.g. **speexdsp** /
**WebRTC APM** / RNNoise) server-side or in a native Electron module: feed it
(mic, TTS-reference) and get a cleaned mic stream. This is what phone systems do.
Highest quality, but you must pipe the TTS PCM as the reference signal and add a
native dep — only worth it if Options 1–2 aren't enough.

### Option 4 — Headphones escape hatch (free)
No echo path at all → barge-in is trivially clean. Add a "headphones mode"
toggle that relaxes `ECHO_FLOOR` and skips the echo-guard when the user confirms
they're on headphones.

### Recommended path
Try **Option 1** first (biggest win for the effort). If prosody suffers, fall
back to the current Web Audio player + the two-stage detector (which already
works), and consider **Option 3** only if you need studio-clean duplex.

## Out of scope / deliberately not done
- Silero VAD: adds a dependency and can't tell the user from Marvi's echo (no
  text knowledge), so it would still need the echo-guard on top. The STT +
  echo-guard combo is strictly better for our stack.
