const RECENT_MS = 30_000
const MAX_RECENT = 8
const MIN_WORDS = 3

interface SpokenText {
  at: number
  words: string[]
}

let recent: SpokenText[] = []

function words(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter(word => word.length > 1)
}

export function rememberSpokenText(text: string, at = Date.now()): void {
  const spokenWords = words(text)

  if (spokenWords.length < MIN_WORDS) {
    return
  }

  recent.push({ at, words: spokenWords })
  recent = recent.slice(-MAX_RECENT)
}

export function isLikelySelfEchoTranscript(transcript: string, at = Date.now()): boolean {
  const transcriptWords = words(transcript)

  if (transcriptWords.length < MIN_WORDS) {
    return false
  }

  recent = recent.filter(item => at - item.at <= RECENT_MS)

  return recent.some(item => {
    const spoken = new Set(item.words)
    const matches = transcriptWords.filter(word => spoken.has(word)).length

    return matches / transcriptWords.length >= 0.8
  })
}

export function clearRecentSpokenText(): void {
  recent = []
}

// STT models (Parakeet/Whisper family) hallucinate short filler when fed silence
// or non-speech noise — "you", "okay", "hmm", "thank you", "thanks for watching",
// "mmhmm", etc. Left home alone, a false wake + these turned the chat into
// garbage. Reject a transcript that is *entirely* one of these (so real commands
// that merely contain "you" are unaffected). Pair with a heard-speech gate:
// no real mic energy -> reject regardless.
const HALLUCINATION_PHRASES = new Set([
  'you',
  'you you',
  'thank you',
  'thank you very much',
  'thanks',
  'thanks for watching',
  'thank you for watching',
  'please subscribe',
  'subscribe',
  'okay',
  'ok',
  'k',
  'hmm',
  'mhm',
  'mhmm',
  'mmhmm',
  'mm hmm',
  'uh',
  'um',
  'uh huh',
  'yeah',
  'yep',
  'so',
  'oh',
  'ah',
  'bye',
  'bye bye',
  'music',
  'applause',
  'silence',
  'right',
  'well',
  'mm',
  'and',
  'the'
])

export function isLikelyHallucination(transcript: string): boolean {
  const normalized = transcript
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  if (!normalized) {
    return true
  }

  if (HALLUCINATION_PHRASES.has(normalized)) {
    return true
  }

  const tokens = normalized.split(' ')

  // A single 1-2 char token, or the same word repeated ("you you you").
  if (tokens.length === 1 && tokens[0].length <= 2) {
    return true
  }

  if (tokens.length >= 2 && new Set(tokens).size === 1) {
    return true
  }

  return false
}
