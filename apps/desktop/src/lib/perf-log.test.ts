// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { currentViewLabel, logPersisted, sessionRelativeTimestamp } from './perf-log'

describe('perf-log', () => {
  describe('sessionRelativeTimestamp', () => {
    it('formats as "+Ns" with one decimal', () => {
      expect(sessionRelativeTimestamp()).toMatch(/^\+\d+\.\d+s$/)
    })

    it('never goes negative even immediately after module load', () => {
      const value = sessionRelativeTimestamp()
      const seconds = Number(value.replace(/^\+/, '').replace(/s$/, ''))

      expect(seconds).toBeGreaterThanOrEqual(0)
    })
  })

  describe('currentViewLabel', () => {
    afterEach(() => {
      window.location.hash = ''
    })

    it('reads the HashRouter route from window.location.hash', () => {
      window.location.hash = '#/chat/abc123'

      expect(currentViewLabel()).toBe('/chat/abc123')
    })

    it('defaults to "/" when there is no hash', () => {
      window.location.hash = ''

      expect(currentViewLabel()).toBe('/')
    })
  })

  describe('logPersisted', () => {
    let consoleErrorSpy: ReturnType<typeof vi.spyOn>

    beforeEach(() => {
      consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    })

    afterEach(() => {
      consoleErrorSpy.mockRestore()
      window.location.hash = ''
    })

    it('emits a single-line entry through console.error (the desktop.log mirror) with prefix, fields, view, and timestamp', () => {
      window.location.hash = '#/settings'

      logPersisted('[UI-PERF]', 'longtask dur=142ms attr=script')

      expect(consoleErrorSpy).toHaveBeenCalledTimes(1)
      const [line] = consoleErrorSpy.mock.calls[0] as [string]

      expect(line).toMatch(/^\[UI-PERF\] longtask dur=142ms attr=script view=\/settings t=\+\d+\.\d+s$/)
    })

    it('never throws even if console.error itself throws', () => {
      consoleErrorSpy.mockImplementation(() => {
        throw new Error('devtools closed mid-write')
      })

      expect(() => logPersisted('[CONN-PERF]', 'connect label=boot durationMs=42 level=INFO ok')).not.toThrow()
    })
  })
})
