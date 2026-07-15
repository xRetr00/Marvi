import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

// Radix Select calls scrollIntoView on its items when the content opens; jsdom
// doesn't implement it (nor hasPointerCapture / releasePointerCapture), so stub
// them to let the dropdown open in tests.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const getGlobalModelInfo = vi.fn()
const getGlobalModelOptions = vi.fn()
const getAuxiliaryModels = vi.fn()
const getMoaModels = vi.fn()
const setModelAssignment = vi.fn()
const getRecommendedDefaultModel = vi.fn()
const saveMoaModels = vi.fn()
const setEnvVar = vi.fn()
const getHermesConfigRecord = vi.fn()
const saveHermesConfig = vi.fn()
const setApiRequestProfile = vi.fn()
const startManualProviderOAuth = vi.fn()
const getVoiceInstantStatus = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: () => getGlobalModelInfo(),
  getGlobalModelOptions: () => getGlobalModelOptions(),
  getAuxiliaryModels: () => getAuxiliaryModels(),
  getMoaModels: () => getMoaModels(),
  setModelAssignment: (body: unknown) => setModelAssignment(body),
  getRecommendedDefaultModel: (slug: string) => getRecommendedDefaultModel(slug),
  saveMoaModels: (body: unknown) => saveMoaModels(body),
  setEnvVar: (key: string, value: string) => setEnvVar(key, value),
  getHermesConfigRecord: () => getHermesConfigRecord(),
  saveHermesConfig: (config: unknown) => saveHermesConfig(config),
  setApiRequestProfile: (profile: unknown) => setApiRequestProfile(profile),
  getVoiceInstantStatus: () => getVoiceInstantStatus()
}))

vi.mock('@/store/onboarding', () => ({
  startManualProviderOAuth: (slug: string) => startManualProviderOAuth(slug)
}))

beforeEach(() => {
  getGlobalModelInfo.mockResolvedValue({ provider: 'openrouter', model: 'openai/gpt-5.4-mini' })
  getGlobalModelOptions.mockResolvedValue({
    providers: [
      {
        name: 'Nous',
        slug: 'nous',
        models: ['hermes-4', 'hermes-4-mini'],
        authenticated: true,
        capabilities: { 'hermes-4': { reasoning: true, fast: true } }
      },
      {
        name: 'OpenRouter',
        slug: 'openrouter',
        models: ['openai/gpt-5.4-mini'],
        authenticated: true,
        capabilities: { 'openai/gpt-5.4-mini': { reasoning: true, fast: true } }
      },
      // An unconfigured api_key provider — surfaced by the full-universe payload.
      {
        name: 'DeepSeek',
        slug: 'deepseek',
        models: [],
        authenticated: false,
        auth_type: 'api_key',
        key_env: 'DEEPSEEK_API_KEY'
      }
    ]
  })
  getAuxiliaryModels.mockResolvedValue({
    main: { provider: 'openrouter', model: 'openai/gpt-5.4-mini' },
    tasks: [{ task: 'vision', provider: 'auto', model: '', base_url: '' }]
  })
  getMoaModels.mockResolvedValue(null)
  setModelAssignment.mockResolvedValue({
    ok: true,
    provider: 'openrouter',
    model: 'openai/gpt-5.4-mini',
    gateway_tools: []
  })
  getRecommendedDefaultModel.mockResolvedValue({ provider: 'deepseek', model: 'deepseek-chat', free_tier: null })
  setEnvVar.mockResolvedValue({ ok: true })
  getHermesConfigRecord.mockResolvedValue({ agent: { reasoning_effort: 'medium', service_tier: 'normal' } })
  saveHermesConfig.mockResolvedValue({ ok: true })
  getVoiceInstantStatus.mockResolvedValue({
    resolved: true,
    provider: 'openrouter',
    model: 'openai/gpt-5.4-mini',
    configured_provider: 'openrouter',
    configured_model: 'openai/gpt-5.4-mini',
    is_fallback: false
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderModelSettings() {
  const { ModelSettings } = await import('./model-settings')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={queryClient}>
      <ModelSettings />
    </QueryClientProvider>
  )
}

describe('ModelSettings', () => {
  it('loads the current main model and lists configured providers only', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())
    await waitFor(() => expect(getGlobalModelOptions).toHaveBeenCalled())

    // Open the provider Select — only configured providers should be listed.
    const triggers = await screen.findAllByRole('combobox')
    fireEvent.click(triggers[0])

    expect(screen.queryByText('Nous')).toBeNull()
    expect((await screen.findAllByText(/OpenRouter/)).length).toBeGreaterThan(0)
    expect(await screen.findByText(/DeepSeek/)).toBeTruthy()
  })

  it('activates an unconfigured api_key provider inline by saving its key', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelOptions).toHaveBeenCalled())

    // Open the provider Select and pick the unconfigured provider.
    const triggers = screen.getAllByRole('combobox')
    fireEvent.click(triggers[0])
    const deepseekOption = await screen.findByText(/DeepSeek/)
    fireEvent.click(deepseekOption)

    // The inline key input appears for an api_key provider that needs setup.
    const keyInput = await screen.findByPlaceholderText(/Paste DEEPSEEK_API_KEY/)
    fireEvent.change(keyInput, { target: { value: 'sk-test-123' } })

    const activate = await screen.findByRole('button', { name: /Activate/ })
    fireEvent.click(activate)

    await waitFor(() => expect(setEnvVar).toHaveBeenCalledWith('DEEPSEEK_API_KEY', 'sk-test-123'))
  })

  it('writes the profile default speed (service_tier) when the fast switch is toggled', async () => {
    await renderModelSettings()
    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())

    const fastSwitch = await screen.findByRole('switch')
    fireEvent.click(fastSwitch)

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({ agent: expect.objectContaining({ service_tier: 'fast' }) })
      )
    )
  })

  it('hides the reasoning/speed defaults when the main model reports no capabilities', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4'],
          authenticated: true,
          capabilities: { 'hermes-4': { reasoning: false, fast: false } }
        }
      ]
    })

    await renderModelSettings()
    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())

    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('renders the auxiliary task rows', async () => {
    await renderModelSettings()

    expect(await screen.findByText('Vision')).toBeTruthy()
    expect(screen.getAllByText('auto · use main model').length).toBeGreaterThan(0)
    expect(screen.getByRole('combobox', { name: 'Instant voice reasoning' }).textContent).toContain('Off')
  })

  it('saves a separate instant voice reasoning level', async () => {
    await renderModelSettings()
    const reasoning = await screen.findByRole('combobox', { name: 'Instant voice reasoning' })
    fireEvent.click(reasoning)
    fireEvent.click(await screen.findByText('Low'))

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          auxiliary: expect.objectContaining({
            voice_instant: expect.objectContaining({ reasoning_effort: 'low' })
          })
        })
      )
    )
  })

  it('assigns an auxiliary task to the main model via setModelAssignment', async () => {
    await renderModelSettings()

    // One "Set to main" button per task slot; the first is Vision.
    const setToMainButtons = await screen.findAllByRole('button', { name: 'Set to main' })
    fireEvent.click(setToMainButtons[0])

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'openai/gpt-5.4-mini',
        provider: 'openrouter',
        scope: 'auxiliary',
        task: 'vision',
        // Already the user's chosen main model -- re-confirming here would
        // just be friction for a pick they already made once.
        confirm_expensive_model: true
      })
    )
  })

  it('warns when a main switch leaves auxiliary tasks pinned to another provider', async () => {
    setModelAssignment.mockResolvedValueOnce({
      ok: true,
      provider: 'openrouter',
      model: 'anthropic/claude-opus-4.7',
      gateway_tools: [],
      stale_aux: [{ task: 'compression', provider: 'nous', model: 'hermes-4' }]
    })

    await renderModelSettings()
    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())

    const applyButton = await screen.findByRole('button', { name: 'Apply' })
    fireEvent.click(applyButton)

    // The switch-time notice names the pinned provider and offers a reset.
    expect(await screen.findByText(/still run on/)).toBeTruthy()
    expect(screen.getByText('nous')).toBeTruthy()
  })

  it('shows a persistent banner when a loaded aux slot mismatches the main provider', async () => {
    getAuxiliaryModels.mockResolvedValueOnce({
      main: { provider: 'openrouter', model: 'openai/gpt-5.4-mini' },
      tasks: [{ task: 'curator', provider: 'deepseek', model: 'deepseek-chat', base_url: '' }]
    })

    await renderModelSettings()

    // Banner present on load, no switch required.
    expect(await screen.findByText(/still run on/)).toBeTruthy()
  })

  // -------------------------------------------------------------------------
  // Instant voice model Apply — spec Part 2 regression coverage. The bug: the
  // Apply button never checked the response's ok/confirm_required fields, so
  // a backend cost-guard rejection (ok:false, confirm_required:true, nothing
  // persisted) looked identical to success — the editor closed and refresh()
  // ran as if auxiliary.voice_instant.{provider,model} had actually been
  // written, when it hadn't.
  // -------------------------------------------------------------------------

  it('applies the instant voice model and persists provider+model via setModelAssignment', async () => {
    await renderModelSettings()

    const changeButtons = await screen.findAllByRole('button', { name: 'Change' })
    fireEvent.click(changeButtons[changeButtons.length - 1]) // voice_instant is the last AUX_TASKS row

    // Two "Apply" buttons now exist -- the always-present main-model one and
    // this row's draft editor; the draft's is appended last in DOM order.
    // The draft is pre-seeded from the current main model (openrouter /
    // openai/gpt-5.4-mini per the beforeEach mocks) -- no need to reselect.
    const applyButtons = await screen.findAllByRole('button', { name: 'Apply' })
    fireEvent.click(applyButtons[applyButtons.length - 1])

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'openai/gpt-5.4-mini',
        provider: 'openrouter',
        scope: 'auxiliary',
        task: 'voice_instant',
        confirm_expensive_model: false
      })
    )

    // Editor closes and the panel re-fetches (including the resolved-status
    // probe) once the backend confirms the write actually happened.
    await waitFor(() => expect(getVoiceInstantStatus).toHaveBeenCalledTimes(2))
  })

  it('does NOT silently succeed when the backend requires cost confirmation — shows a prompt instead of closing', async () => {
    setModelAssignment.mockResolvedValueOnce({
      ok: false,
      confirm_required: true,
      confirm_message: 'This model looks expensive. Apply anyway?'
    })

    await renderModelSettings()

    const changeButtons = await screen.findAllByRole('button', { name: 'Change' })
    fireEvent.click(changeButtons[changeButtons.length - 1])

    const applyButtons = await screen.findAllByRole('button', { name: 'Apply' })
    fireEvent.click(applyButtons[applyButtons.length - 1])

    // The confirmation prompt appears -- NOT a silently-closed editor.
    expect(await screen.findByText('This model looks expensive. Apply anyway?')).toBeTruthy()
    // The editor is still open (the "Apply" button is still present) --
    // this is the crux of the regression: before the fix, this click would
    // have closed the editor as if the model had been saved.
    expect(screen.getAllByRole('button', { name: 'Apply' }).length).toBe(2)

    // Confirming resends with confirm_expensive_model: true.
    fireEvent.click(await screen.findByRole('button', { name: 'Apply anyway' }))

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenLastCalledWith({
        model: 'openai/gpt-5.4-mini',
        provider: 'openrouter',
        scope: 'auxiliary',
        task: 'voice_instant',
        confirm_expensive_model: true
      })
    )
  })

  it('shows the resolved "currently using" model, and flags a silent fallback', async () => {
    getVoiceInstantStatus.mockResolvedValue({
      resolved: true,
      provider: 'openai',
      model: 'gpt-5.4-mini',
      configured_provider: 'openrouter',
      configured_model: 'deepseek/deepseek-v4-flash',
      is_fallback: true
    })

    await renderModelSettings()

    expect(await screen.findByText(/currently using: openai · gpt-5.4-mini/)).toBeTruthy()
    expect(await screen.findByText(/fallback/)).toBeTruthy()
  })
})
