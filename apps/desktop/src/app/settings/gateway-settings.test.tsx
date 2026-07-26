import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionConfig, DesktopConnectionProbeResult } from '@/global'

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/profile', () => {
  return {
    $profiles: atom([]),
    refreshActiveProfile: vi.fn(async () => undefined)
  }
})

const MERCURY_URL = 'http://mercury2:9119'

const getConnectionConfig = vi.fn<() => Promise<DesktopConnectionConfig>>()
const oauthLoginConnectionConfig = vi.fn<() => Promise<{ baseUrl: string; connected: boolean; ok: boolean }>>()
const probeConnectionConfig = vi.fn<() => Promise<DesktopConnectionProbeResult>>()
const saveConnectionConfig = vi.fn<(payload: unknown) => Promise<DesktopConnectionConfig>>()
const testConnectionConfig = vi.fn()

function connectionConfig(overrides: Partial<DesktopConnectionConfig> = {}): DesktopConnectionConfig {
  return {
    envOverride: true,
    mode: 'remote',
    profile: null,
    remoteAuthMode: 'oauth',
    remoteOauthConnected: false,
    remoteTokenPreview: null,
    remoteTokenSet: false,
    remoteUrl: MERCURY_URL,
    ...overrides
  }
}

function probeResult(overrides: Partial<DesktopConnectionProbeResult> = {}): DesktopConnectionProbeResult {
  return {
    authMode: 'oauth',
    baseUrl: MERCURY_URL,
    error: null,
    providers: [{ displayName: 'Basic', name: 'basic', supportsPassword: true }],
    reachable: true,
    version: null,
    ...overrides
  }
}

beforeEach(() => {
  getConnectionConfig.mockResolvedValue(connectionConfig())
  oauthLoginConnectionConfig.mockResolvedValue({ baseUrl: MERCURY_URL, connected: true, ok: true })
  probeConnectionConfig.mockResolvedValue(probeResult())
  saveConnectionConfig.mockResolvedValue(connectionConfig())
  testConnectionConfig.mockResolvedValue({ baseUrl: MERCURY_URL, ok: true, version: '0.19.0' })

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      getConnectionConfig,
      oauthLoginConnectionConfig,
      probeConnectionConfig,
      saveConnectionConfig,
      testConnectionConfig
    }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  delete (window as Partial<Window>).hermesDesktop
})

async function renderGatewaySettings() {
  const { GatewaySettings } = await import('./gateway-settings')

  return render(<GatewaySettings />)
}

describe('GatewaySettings', () => {
  it('typing a clean first-run URL does not probe, save, or authenticate', async () => {
    getConnectionConfig.mockResolvedValue(
      connectionConfig({
        envOverride: false,
        remoteUrl: ''
      })
    )

    await renderGatewaySettings()

    const input = await screen.findByPlaceholderText('https://gateway.example.com/hermes')
    fireEvent.change(input, { target: { value: MERCURY_URL } })

    expect(probeConnectionConfig).not.toHaveBeenCalled()
    expect(saveConnectionConfig).not.toHaveBeenCalled()
    expect(oauthLoginConnectionConfig).not.toHaveBeenCalled()
  })

  it('Sign In explicitly probes, then opens login without committing the candidate', async () => {
    getConnectionConfig.mockResolvedValue(connectionConfig({ envOverride: false, remoteUrl: '' }))
    await renderGatewaySettings()
    fireEvent.change(await screen.findByPlaceholderText('https://gateway.example.com/hermes'), { target: { value: MERCURY_URL } })
    fireEvent.click(await screen.findByRole('button', { name: /sign in/i }))

    await waitFor(() => expect(probeConnectionConfig).toHaveBeenCalledWith(MERCURY_URL))
    expect(oauthLoginConnectionConfig).toHaveBeenCalledWith(MERCURY_URL)
    expect(saveConnectionConfig).not.toHaveBeenCalled()
  })

  it('does not auto-probe an env-provided URL', async () => {
    await renderGatewaySettings()

    expect(await screen.findByDisplayValue(MERCURY_URL)).toBeTruthy()
    expect(probeConnectionConfig).not.toHaveBeenCalled()
  })

  it('allows saving an env-provided OAuth gateway for future app launches', async () => {
    getConnectionConfig.mockResolvedValue(connectionConfig({ remoteOauthConnected: true }))
    saveConnectionConfig.mockResolvedValue(connectionConfig({ remoteOauthConnected: true }))

    await renderGatewaySettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Save for next restart' }))

    await waitFor(() =>
      expect(saveConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        profile: undefined,
        remoteAuthMode: 'oauth',
        remoteToken: undefined,
        remoteUrl: MERCURY_URL
      })
    )
    expect(testConnectionConfig).toHaveBeenCalled()
  })
})
