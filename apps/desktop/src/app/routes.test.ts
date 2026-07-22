import { describe, expect, it } from 'vitest'

import { APP_ROUTES, isOverlayView, OVERLAY_ROUTES, SETTINGS_ROUTE } from './routes'

describe('overlay workspace lifetime', () => {
  it('keeps the chat mounted beneath every route overlay, including Settings', () => {
    const overlayPaths = new Set(OVERLAY_ROUTES.map(route => route.path))

    for (const route of APP_ROUTES) {
      expect(overlayPaths.has(route.path)).toBe(isOverlayView(route.view))
    }

    expect(OVERLAY_ROUTES.some(route => route.path === SETTINGS_ROUTE)).toBe(true)
  })
})
