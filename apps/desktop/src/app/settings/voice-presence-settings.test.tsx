import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

const getAuxiliaryModels = vi.fn()
const getGlobalModelOptions = vi.fn()
const getVoiceInstantStatus = vi.fn()
const getVoiceSpeakers = vi.fn()
const setModelAssignment = vi.fn()

vi.mock('@/hermes', () => ({
  enrollVoiceSpeaker: vi.fn(),
  getAuxiliaryModels: () => getAuxiliaryModels(),
  getGlobalModelOptions: () => getGlobalModelOptions(),
  getVoiceInstantStatus: () => getVoiceInstantStatus(),
  getVoiceSpeakers: () => getVoiceSpeakers(),
  removeVoiceSpeaker: vi.fn(),
  setModelAssignment: (body: unknown) => setModelAssignment(body)
}))

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  getGlobalModelOptions.mockResolvedValue({
    providers: [
      {
        authenticated: true,
        models: ['deepseek-v4-flash', 'mimo-v2.5'],
        name: 'OpenCode Go',
        slug: 'opencode-go'
      }
    ]
  })
  getAuxiliaryModels.mockResolvedValue({
    main: { model: 'kimi-k3', provider: 'opencode-go' },
    tasks: [{ base_url: '', model: 'mimo-v2.5', provider: 'opencode-go', task: 'voice_instant' }]
  })
  getVoiceInstantStatus.mockResolvedValue({
    configured_model: 'mimo-v2.5',
    configured_provider: 'opencode-go',
    is_fallback: false,
    model: 'mimo-v2.5',
    provider: 'opencode-go',
    resolved: true
  })
  getVoiceSpeakers.mockResolvedValue({ speakers: [] })
  setModelAssignment.mockResolvedValue({
    model: 'deepseek-v4-flash',
    ok: true,
    provider: 'opencode-go'
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('VoicePresenceSettings instant model picker', () => {
  it('changes the persisted voice-instant model instead of presenting a fixed label', async () => {
    const { VoicePresenceSettings } = await import('./voice-presence-settings')

    const marvi = {
      get: <T,>(_key: string, fallback: T) => fallback,
      patch: vi.fn()
    }

    render(<VoicePresenceSettings marvi={marvi as never} onOpenModelConfig={vi.fn()} onOpenVoiceConfig={vi.fn()} />)

    const modelSelect = await screen.findByRole('combobox', { name: 'Instant voice model' })
    expect(modelSelect.textContent).toContain('mimo-v2.5')

    fireEvent.click(modelSelect)
    fireEvent.click(await screen.findByRole('option', { name: 'deepseek-v4-flash' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'deepseek-v4-flash',
        provider: 'opencode-go',
        scope: 'auxiliary',
        task: 'voice_instant'
      })
    )
  })
})
