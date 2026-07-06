import { afterEach, describe, expect, it } from 'vitest'

import { clearRecentSpokenText, isLikelyHallucination, isLikelySelfEchoTranscript, rememberSpokenText } from './voice-echo-guard'

describe('isLikelyHallucination', () => {
  it('rejects the common silence hallucinations', () => {
    for (const junk of ['', '   ', 'you', 'okay', 'hmm', 'thank you', 'thanks for watching', 'mmhmm', '.', 'you you you']) {
      expect(isLikelyHallucination(junk)).toBe(true)
    }
  })

  it('keeps real commands', () => {
    for (const real of ['what time is it', 'open the terminal', 'summarize this file', 'thank you for opening the file']) {
      expect(isLikelyHallucination(real)).toBe(false)
    }
  })
})

describe('voice echo guard', () => {
  afterEach(() => clearRecentSpokenText())

  it('rejects transcripts that mostly repeat recently spoken TTS', () => {
    rememberSpokenText('The deployment succeeded and the logs are green.', 1000)

    expect(isLikelySelfEchoTranscript('deployment succeeded and logs are green', 2000)).toBe(true)
  })

  it('keeps unrelated user speech and stale echoes', () => {
    rememberSpokenText('The deployment succeeded and the logs are green.', 1000)

    expect(isLikelySelfEchoTranscript('open the settings page', 2000)).toBe(false)
    expect(isLikelySelfEchoTranscript('deployment succeeded and logs are green', 40_000)).toBe(false)
  })

  it('does not reject very short transcripts', () => {
    rememberSpokenText('yes', 1000)

    expect(isLikelySelfEchoTranscript('yes', 2000)).toBe(false)
  })
})
