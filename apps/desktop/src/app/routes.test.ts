import { describe, expect, it } from 'vitest'

import {
  APP_ROUTES,
  isOverlayView,
  NEW_CHAT_ROUTE,
  OVERLAY_ROUTES,
  primaryRouteSelectedSessionId,
  sessionRoute,
  SETTINGS_ROUTE
} from './routes'

describe('overlay workspace lifetime', () => {
  it('keeps the chat mounted beneath every route overlay, including Settings', () => {
    const overlayPaths = new Set(OVERLAY_ROUTES.map(route => route.path))

    for (const route of APP_ROUTES) {
      expect(overlayPaths.has(route.path)).toBe(isOverlayView(route.view))
    }

    expect(OVERLAY_ROUTES.some(route => route.path === SETTINGS_ROUTE)).toBe(true)
  })
})

const SESS_A = 'sess-a'
const SESS_B = 'sess-b'

describe('primaryRouteSelectedSessionId', () => {
  it('prefers the routed session id over a stale/different store selection (#59305)', () => {
    // The route already committed to B while the store selection hasn't
    // caught up yet (still reads A) — the route wins.
    expect(primaryRouteSelectedSessionId(sessionRoute(SESS_B), SESS_A)).toBe(SESS_B)
  })

  it('returns null on the new-chat route even with a leftover selection from the previous chat', () => {
    expect(primaryRouteSelectedSessionId(NEW_CHAT_ROUTE, SESS_A)).toBeNull()
  })

  it('falls back to the store selection on a non-chat route (settings, overlays)', () => {
    expect(primaryRouteSelectedSessionId(SETTINGS_ROUTE, SESS_A)).toBe(SESS_A)
  })

  it('falls back to the store selection when the route matches the same session', () => {
    expect(primaryRouteSelectedSessionId(sessionRoute(SESS_A), SESS_A)).toBe(SESS_A)
  })

  it('returns null on a non-chat route with no store selection', () => {
    expect(primaryRouteSelectedSessionId(SETTINGS_ROUTE, null)).toBeNull()
  })
})
