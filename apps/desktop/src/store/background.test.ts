import { describe, expect, it } from 'vitest'

import { backgroundFor, BACKGROUNDS } from './background'

describe('backgroundFor', () => {
  it('cycles every available background in auto mode', () => {
    expect(backgroundFor('auto')).toBe(BACKGROUNDS.electricGaze)
    expect(backgroundFor('auto', 1)).toBe(BACKGROUNDS.personalWebsite)
    expect(backgroundFor('auto', 2)).toBe(BACKGROUNDS.asciiFlower)
    expect(backgroundFor('auto', 3)).toBe(BACKGROUNDS.electricGaze)
  })
})
