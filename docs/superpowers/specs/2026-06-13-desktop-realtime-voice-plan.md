# Desktop Realtime Voice Plan

## Scope

All realtime voice upgrades target the Electron desktop app first. The classic
CLI and TUI voice paths stay unchanged unless a later task explicitly asks to
port the desktop work back.

The existing desktop playback path now uses a Markdown AST speech renderer
before calling TTS. That is the foundation for making AI Markdown responses read
naturally in voice conversation mode and read-aloud mode.

## Current Voice Baseline

- Desktop records audio in the renderer and uploads it to `/api/audio/transcribe`.
- The backend uses the existing `tools.transcription_tools.transcribe_audio`
  provider chain.
- Desktop asks `/api/audio/speak` for a complete synthesized audio data URL.
- Playback happens in the browser with `HTMLAudioElement`.
- Existing speech cleanup was regex based; it is now AST based for desktop.

## Locked TTS Direction

PocketTTS is the preferred new local CPU TTS option beside the existing TTS
providers.

Reasons:

- Kyutai describes PocketTTS as CPU-focused, installable with Python, and not
  requiring GPU PyTorch.
- Kyutai's technical report positions it as high-quality CPU TTS with voice
  cloning.
- It fits the desktop-first requirement because the Python backend can host it
  behind the same `/api/audio/speak` shape or a new streaming speak endpoint.

Existing providers remain available. PocketTTS is additive, not a replacement.

Sources:

- https://github.com/kyutai-labs/pocket-tts
- https://kyutai.org/pocket-tts-technical-report
- https://kyutai.org/tts

## STT Direction

Primary recommendation: sherpa-onnx.

Reasons:

- It supports local streaming and non-streaming ASR.
- It also includes VAD, keyword spotting, TTS, punctuation, and speech
  enhancement in the same ecosystem.
- It supports Windows, Linux, macOS, Android, iOS, and embedded targets, which
  is a better long-term fit for a desktop app than an STT-only integration.
- The Python backend can integrate it beside the current `transcribe_audio`
  chain without changing current providers.

Moonshine should be kept as an evaluation candidate, not the first integration.
It is promising for low-latency English STT and voice agents, but sherpa-onnx
has the broader voice stack Marvi needs: STT plus VAD plus keyword spotting.

Sources:

- https://github.com/k2-fsa/sherpa-onnx
- https://k2-fsa.github.io/sherpa/onnx/index.html
- https://github.com/moonshine-ai/moonshine
- https://github.com/moonshine-ai/moonshine-js

## Deferred Advanced Duplex Work

Barge-in and acoustic echo cancellation are explicitly future updates.

Not in this phase:

- User speech interrupting active TTS.
- Cancelling the active LLM generation from microphone input.
- Acoustic echo cancellation for speaker-to-microphone feedback.
- Full phone-call duplex behavior.

This keeps the near-term work lower risk. The next voice phase should still be
real-time in the practical sense: faster endpointing, streaming STT partials,
and faster TTS start. It should not attempt full duplex.

## Update Order

1. Desktop Markdown AST speech renderer.
2. PocketTTS provider behind existing desktop speak flow.
3. sherpa-onnx streaming STT proof behind a new opt-in desktop voice setting.
4. Desktop realtime voice session state: listening, partial transcript, final
   transcript, thinking, speaking.
5. Optional wake word for desktop voice activation.
6. Moonshine benchmark spike if sherpa-onnx latency or accuracy is not good
   enough on CPU.
7. Future only: barge-in and AEC.

## Risk Controls

- Keep current CLI/TUI code paths untouched.
- Keep current desktop `/api/audio/transcribe` and `/api/audio/speak` behavior
  working while adding new providers.
- Add provider-specific tests before implementation.
- Gate realtime voice behind config/UI settings so normal desktop voice remains
  stable.
- Treat PocketTTS and sherpa-onnx as additive provider integrations, not core
  model tools.
