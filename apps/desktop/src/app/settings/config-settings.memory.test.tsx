import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfigSettings } from './config-settings'

const getHermesConfigRecord = vi.fn()
const getHermesConfigSchema = vi.fn()
const saveHermesConfig = vi.fn()
const getElevenLabsVoices = vi.fn()
const warmTextToSpeech = vi.fn()

vi.mock('@/hermes', () => ({
  getHermesConfigRecord: () => getHermesConfigRecord(),
  getHermesConfigSchema: () => getHermesConfigSchema(),
  saveHermesConfig: (c: unknown) => saveHermesConfig(c),
  getElevenLabsVoices: () => getElevenLabsVoices(),
  warmTextToSpeech: () => warmTextToSpeech(),
  setApiRequestProfile: vi.fn(),
  getMemoryProviderOAuthStatus: () => Promise.reject(new Error('no')),
  startMemoryProviderOAuth: () => Promise.resolve()
}))

vi.mock('../hooks/use-on-profile-switch', () => ({
  useOnProfileSwitch: () => undefined
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// Schema shape mirrors what CONFIG_SCHEMA actually produces for these two
// keys server-side (hermes_cli/web_server.py::_build_schema_from_config) —
// see tests/hermes_cli/test_config_schema.py::test_memory_toggles_in_schema
// for the Python-side half of this round trip.
const MEMORY_SCHEMA = {
  fields: {
    'memory.memory_enabled': { type: 'boolean', description: 'x', category: 'memory' },
    'memory.user_profile_enabled': { type: 'boolean', description: 'x', category: 'memory' },
    'memory.memory_char_limit': { type: 'number', description: 'x', category: 'memory' },
    'memory.user_char_limit': { type: 'number', description: 'x', category: 'memory' },
    'memory.provider': { type: 'select', description: 'x', category: 'memory', options: ['', 'honcho'] }
  },
  category_order: ['memory']
}

function renderMemorySection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ConfigSettings activeSectionId="memory" importInputRef={{ current: null }} />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('Memory & Context settings toggle', () => {
  it('shows the live effective state — both switches checked when the on-disk config has them enabled', async () => {
    getElevenLabsVoices.mockResolvedValue({ available: false, voices: [] })
    getHermesConfigRecord.mockResolvedValue({
      memory: {
        memory_enabled: true,
        user_profile_enabled: true,
        write_approval: false,
        memory_char_limit: 5000,
        user_char_limit: 1500,
        provider: 'honcho'
      }
    })
    getHermesConfigSchema.mockResolvedValue(MEMORY_SCHEMA)

    renderMemorySection()

    const switches = await screen.findAllByRole('switch')
    expect(switches).toHaveLength(2)
    expect(switches[0].getAttribute('aria-checked')).toBe('true')
    expect(switches[1].getAttribute('aria-checked')).toBe('true')
  })

  it('shows both switches unchecked when the on-disk config has memory disabled', async () => {
    getElevenLabsVoices.mockResolvedValue({ available: false, voices: [] })
    getHermesConfigRecord.mockResolvedValue({
      memory: {
        memory_enabled: false,
        user_profile_enabled: false,
        write_approval: false,
        memory_char_limit: 2200,
        user_char_limit: 1375,
        provider: ''
      }
    })
    getHermesConfigSchema.mockResolvedValue(MEMORY_SCHEMA)

    renderMemorySection()

    const switches = await screen.findAllByRole('switch')
    expect(switches[0].getAttribute('aria-checked')).toBe('false')
    expect(switches[1].getAttribute('aria-checked')).toBe('false')
  })

  it('round-trips a toggle: flipping Persistent Memory on autosaves memory_enabled: true through the save API', async () => {
    getElevenLabsVoices.mockResolvedValue({ available: false, voices: [] })
    getHermesConfigRecord.mockResolvedValue({
      memory: {
        memory_enabled: false,
        user_profile_enabled: true,
        write_approval: false,
        memory_char_limit: 2200,
        user_char_limit: 1375,
        provider: ''
      }
    })
    getHermesConfigSchema.mockResolvedValue(MEMORY_SCHEMA)
    saveHermesConfig.mockResolvedValue({ ok: true })

    renderMemorySection()

    const switches = await screen.findAllByRole('switch')
    expect(switches[0].getAttribute('aria-checked')).toBe('false')

    fireEvent.click(switches[0])

    await waitFor(
      () => {
        expect(saveHermesConfig).toHaveBeenCalledTimes(1)
      },
      { timeout: 2000 }
    )

    const saved = saveHermesConfig.mock.calls[0][0] as { memory: { memory_enabled: boolean } }
    expect(saved.memory.memory_enabled).toBe(true)
  })
})
