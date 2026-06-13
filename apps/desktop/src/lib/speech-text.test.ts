import { describe, expect, it } from 'vitest'

import { renderMarkdownForSpeech, sanitizeTextForSpeech } from './speech-text'

describe('renderMarkdownForSpeech', () => {
  it('renders structured markdown as natural speech', () => {
    const text = renderMarkdownForSpeech(`# Deploy notes

Use **PocketTTS** with [docs](https://example.com).

- Install the package
- Run \`marvi voice\`

\`\`\`bash
npm test
\`\`\``)

    expect(text).toBe(
      'Deploy notes. Use PocketTTS with docs. Install the package. Run marvi voice. Code block skipped.'
    )
  })

  it('summarizes tables and skips decorative markdown', () => {
    const text = renderMarkdownForSpeech(`Thinking...

| Provider | Role |
| --- | --- |
| PocketTTS | TTS |
| sherpa-onnx | STT |

![diagram](voice.png)

> Keep the desktop path isolated.`)

    expect(text).toBe(
      'Table with columns Provider, Role, and 2 rows. Image skipped: diagram. Quote: Keep the desktop path isolated.'
    )
  })
})

describe('sanitizeTextForSpeech', () => {
  it('uses markdown AST rendering before final text cleanup', () => {
    expect(sanitizeTextForSpeech('## Result\n\nUse `PocketTTS` for **voice**.')).toBe(
      'Result. Use PocketTTS for voice.'
    )
  })
})
