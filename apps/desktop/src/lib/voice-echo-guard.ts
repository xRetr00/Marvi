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
