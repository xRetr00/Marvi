# Voice: Full-Duplex "Phone Call" Refactor — Design Spec

**Date:** 2026-07-05
**Status:** Approved, implementation starting
**Owner:** @xRetr00
**Related:** `tools/parakeet_streaming_stt.py`, `hermes_cli/web_server.py` (`/api/audio/transcribe/stream`), `tools/tts_tool.py` (PocketTTS), `apps/desktop` voice-island + voice-conversation + wake-word.

> **How to read this doc (for future-you):** every phase lists **Where** (files),
> **Why** (the reason it exists), and **What to do** (the change + where the tunable
> knobs live). When you come back to tune latency or barge-in, jump straight to the
> "Tunables" table at the bottom.

---

## 1. Goal

Make Marvi's voice modes feel like a **real phone call**: you can talk *over* her,
she stops instantly and responds, and turn-taking is sub-second. This must work in
**both** desktop voice modes — **hands-free** (always listening) and **wake-word**
(say the wake word, then converse).

Decisions locked with the user (2026-07-05):
- **True full-duplex barge-in** — listen while speaking. *This explicitly lifts the
  standing "no barge-in without approval" rule, for this work only.*
- **Keep Parakeet EOU (STT) + PocketTTS (TTS)** — make them genuinely stream; do
  **not** swap to the Kyutai moshi-server stack.
- **Speakers must work** — so we need acoustic echo cancellation (AEC), not just a
  headphones assumption.

## 2. Why — what we learned from unmute.sh (Kyutai)

unmute.sh feels like a phone call because of **Delayed Streams Modeling**: audio and
text are parallel, frame-synchronous streams (~12.5 Hz). Consequences we are copying
in spirit (not adopting their models):

| Kyutai technique | What we do instead (keeping our models) |
| --- | --- |
| Frame-synchronous STT, O(n) | Parakeet **cache-aware streaming** (Phase 1) — replaces our O(n²) re-transcribe |
| Semantic VAD inside STT | We already have this: Parakeet **EOU** ≈ semantic VAD. Read it per-frame instead of every 0.5 s |
| "Flush trick" (~125 ms final) | Race the buffered tail at max speed on EOU (Phase 1) |
| Streaming-**in** TTS (~220 ms) | PocketTTS is one-shot → **clause-level** synth is our floor (Phase 1). Documented ceiling. |
| Overlapped STT+LLM+TTS | Client duplex session runs them concurrently (Phase 1) |
| Full-duplex (Moshi) | Mic-open-during-playback + AEC + barge-in detector (Phase 2) |

**Honest ceilings** (do not delete — future-you will ask "why isn't it as fast as unmute?"):
- PocketTTS `generate_audio()` is one-shot; we cannot stream LLM tokens into it
  token-by-token. Clause granularity is the latency floor for the "keep PocketTTS" choice.
- Parakeet cache-aware streaming depends on the NeMo API exposing it for
  `nvidia/parakeet_realtime_eou_120m-v1`. If it doesn't, STT degrades to the improved
  re-transcribe (works, higher latency). Must be validated on GPU.
- Speaker AEC (WebRTC) is good, not perfect; barge-in is empirically tuned.

## 3. Architecture — client-orchestrated duplex

We chose **client orchestration** (not a backend moshi-style single socket) because it
reuses the existing WS STT stream, the `/speak/stream` TTS path, and the Dynamic Island
plumbing, and contains the one risky ML change to a single backend helper.

**New core:** a client-side **duplex session** state machine
(`apps/desktop/src/store/duplex-session.ts`) that, once active, runs mic → STT → turn →
LLM → TTS **concurrently** instead of the current sequential
`listen → final → agent → speak`.

```
idle → listening ⇄ thinking → speaking(mic-armed) → [barge-in | eou] → listening → …
```

Both hands-free and wake-word modes feed the same machine (Phase 3).

## 4. Phases

Each phase is independently shippable and **push-merges to `main`** (user's cadence).

### Phase 1 — Fast half-duplex (streaming foundation)

**Backend**

- **Where:** `tools/parakeet_streaming_stt.py`
  **Why:** today `accept_bytes` buffers and re-transcribes the whole growing buffer
  every 0.5 s (O(n²) + temp-WAV I/O) — that is the main latency source and starves EOU.
  **What to do:** add a cache-aware streaming path — hold the encoder cache, feed fixed
  ~80–160 ms chunks, emit continuous `{type:"partial", text, eou_prob}`. Keep the
  existing re-transcribe as a **fallback** when the streaming API is unavailable (guard
  with a capability check + log which path is active to `logs/parakeet-stt.log`).
  Leave a `# NOTE(duplex-phase1):` marker at the branch point.

- **Where:** `hermes_cli/web_server.py` → `transcribe_audio_stream_ws` (~line 13113) and
  `_ParakeetSubprocessSession` (~line 175).
  **Why:** the endpoint must forward the richer EOU signal and finalize fast.
  **What to do:** forward `eou_prob`; keep the `turn` reply sourced from Parakeet EOU;
  add the **flush trick** on `stop`/EOU (finalize the buffered tail immediately). Comment
  the flush with `# NOTE(duplex-phase1): flush trick — see spec §2`.

**Client**

- **Where:** `apps/desktop/src/lib/voice-playback.ts`
  **Why:** TTS currently streams at *sentence* granularity and can't be stopped mid-word;
  duplex needs frequent stop points and instant cancel.
  **What to do:** split agent text on **clause** boundaries (`,;:.!?…`); synth each clause
  via `/speak/stream`; expose a hard `stopTts()` that cancels the AudioContext scheduler
  **and** aborts the in-flight fetch (`AbortController`). Start TTS on the first clause
  while the LLM still streams.

- **Where:** `apps/desktop/src/lib/streaming-transcription.ts`
  **Why:** it already routes `partial`/`turn`/`final`; extend for `eou_prob`.
  **What to do:** surface `eou_prob` on the partial callback so the session can act on a
  probability, not just a boolean.

*Deliverable: sub-second turn-taking, no talk-over yet.*

### Phase 2 — True duplex (AEC + barge-in)

> **UPDATE 2026-07-05 (found during impl):** most of this already exists in the
> codebase and works. Re-scoped to the true remaining delta below.
>
> **Already present (do NOT rebuild):**
> - Mic **is** open during `speaking` — `use-voice-conversation.ts` `speak()` calls
>   `handle.start({ onLevel })` while playback runs.
> - **Energy barge-in gate** — `apps/desktop/src/lib/voice-barge-in.ts`
>   (`createBargeInGate`, has tests). Wired in `speak()` with
>   `{ graceMs: 700, level: 0.32, sustainedMs: 350 }`; on trigger it
>   `stopVoicePlayback()` + `handle.cancel()` + `onInterrupt()`.
> - **AEC** — `use-mic-recorder.ts` requests `echoCancellation/noiseSuppression/`
>   `autoGainControl`.
> - **Text-level echo rejection** — `apps/desktop/src/lib/voice-echo-guard.ts`
>   (`rememberSpokenText` / `isLikelySelfEchoTranscript`) drops transcripts that
>   match what Marvi just said.

**Remaining delta (the "speakers must work" gap):** the barge-in gate triggers on
**raw mic energy only**, so Marvi's own voice through speakers (residual past AEC)
can self-trigger it. The fix:

  1. **Echo-robust confirmation.** Add a `confirm` signal to the gate so a trigger
     needs sustained energy **AND** confirmation it's the user (not echo). Confirmation
     source options, cheapest first: (a) require the energy onset to persist through a
     short window where `isLikelySelfEchoTranscript` is false; (b) run streaming STT
     during `speaking` and require a non-empty partial. Start with (a).
  2. **Tunable thresholds + rich logs.** Lift `{graceMs, level, sustainedMs}` out of the
     `speak()` literal into a config object; log gate state transitions
     (`idle→rising→triggered`, and *rejected-as-echo*) via `vpLog('voice', ...)` so it's
     tunable from `[voice-presence]` logs. On speakers you'll likely raise `level`.
  3. Config surface: `voice.barge_in.sensitivity` (+ Settings UI later).

*Deliverable: talk over Marvi on speakers without her cutting herself off.*

### Phase 3 — Island duplex UI, both modes

- **Where:** `apps/desktop/src/app/voice-island/dynamic-island.tsx`,
  `apps/desktop/src/store/voice-presence.ts`, `use-voice-conversation.ts`,
  `use-wake-word.ts`, `desktop-controller.tsx`.
  **Why:** the user must *see* they can interrupt, and both modes must drive the same loop.
  **What to do:**
  1. Add a `speaking` island state with a **mic-armed affordance** (live dot / "interrupt"
     hint); a **barge-in transition** (her caption cuts → listening).
  2. Add `armed`/`bargeable` to `$voiceState`; publish through the island bridge.
  3. **Hands-free** → always in the duplex session.
  4. **Wake-word** → wake opens the duplex session and keeps it **live** (continuous
     listen) until end-of-conversation (silence timeout / "goodbye"), then back to
     wake-listen. Barge-in active throughout. Mark the session lifetime with a
     `// NOTE(duplex-phase3):` at the wake→session handoff.

*Deliverable: both modes feel like a phone call; the UI shows it.*

## 5. Testing

- **Python:** parakeet streaming self-check with a fake cache-aware model — partials grow,
  `eou_prob` crosses threshold, flush path returns the final. (Extend
  `tests/tools/test_parakeet_streaming_stt.py`.)
- **Client:** pure unit tests for the **clause splitter** and the **barge-in decision**
  function (`(energy, partial, isSpeaking) → bool`). No DOM/audio needed — keep them pure.
- **Manual:** the phone-call loop on real hardware — this is where AEC + `BARGE_IN`
  thresholds get tuned. Log via the existing `[voice-presence]` `vpLog` channel.

## 6. Tunables (future-you's cheat sheet)

| Knob | File | Default | When to change |
| --- | --- | --- | --- |
| STT chunk size | `parakeet_streaming_stt.py` | ~80–160 ms | Smaller = lower latency, more compute |
| EOU probability threshold | `parakeet_streaming_stt.py` / session | TBD in impl | Raise if she cuts you off; lower if she waits too long |
| Clause split chars | `voice-playback.ts` | `,;:.!?…` | Fewer = faster first audio, choppier prosody |
| `BARGE_IN.ENERGY_THRESHOLD` | `duplex-session.ts` | TBD | Raise if speaker echo self-triggers barge-in |
| `BARGE_IN.ONSET_MS` | `duplex-session.ts` | TBD | Raise to reject coughs/clicks; lower for snappier interrupt |
| Wake-word session idle timeout | `use-wake-word.ts` | TBD | How long a conversation stays "live" after you stop talking |

## 7. Out of scope / deferred

- Backend moshi-style single-socket orchestration (Approach B) — revisit only if we need
  server-side batching for many concurrent users.
- Streaming-**in** TTS (token-level) — blocked by PocketTTS's one-shot API; would require
  a different TTS or its server API.
