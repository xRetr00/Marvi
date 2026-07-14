import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

export const BACKGROUNDS = {
  electricGaze: {
    kind: 'video',
    poster: 'https://assets.21st.dev/ascii-recipes/thumbnails/user_2nElBLvklOKlAURm6W1PTu6yYFh/ae758991-0c3f-4c6a-9296-33784c65d43b.webp',
    src: 'https://assets.21st.dev/ascii-recipes/videos/user_2nElBLvklOKlAURm6W1PTu6yYFh/c458eb38-7f4e-4272-8711-59a86e20d624.mp4'
  },
  personalWebsite: {
    kind: 'video',
    poster: 'https://assets.21st.dev/ascii-recipes/thumbnails/user_3DGegI7DSMBe4TOO9tgylJbWneq/fc8eb20d-1b76-4d65-b19f-feff221dc91f.webp',
    src: 'https://assets.21st.dev/ascii-recipes/videos/user_3DGegI7DSMBe4TOO9tgylJbWneq/375a2230-64b1-4100-8581-e364f782bbe7.mp4'
  },
  asciiFlower: { kind: 'canvas' }
} as const

export type BackgroundId = keyof typeof BACKGROUNDS
export type BackgroundMode = BackgroundId | 'auto'

const KEY = 'hermes.desktop.background.v1'
const BACKGROUND_IDS = Object.keys(BACKGROUNDS) as BackgroundId[]

const storedMode = () => storedString(KEY)

const isBackgroundMode = (value: string | null): value is BackgroundMode =>
  value !== null && (value === 'auto' || value in BACKGROUNDS)

const initialMode = typeof window === 'undefined' ? null : storedMode()

export const $backgroundMode = atom<BackgroundMode>(isBackgroundMode(initialMode) ? initialMode : 'electricGaze')

export function setBackgroundMode(mode: BackgroundMode): void {
  $backgroundMode.set(mode)
}

export function backgroundFor(mode: BackgroundMode, index = 0) {
  return BACKGROUNDS[mode === 'auto' ? BACKGROUND_IDS[index % BACKGROUND_IDS.length] : mode]
}

if (typeof window !== 'undefined') {
  $backgroundMode.subscribe(mode => persistString(KEY, mode))
}
