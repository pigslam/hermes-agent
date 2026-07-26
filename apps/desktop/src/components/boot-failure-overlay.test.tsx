import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionConfig } from '@/global'
import { $desktopBoot } from '@/store/boot'
import { clearGatewayRecovery } from '@/store/gateway-recovery'
import { $desktopOnboarding } from '@/store/onboarding'

import { BootFailureOverlay } from './boot-failure-overlay'
import { GatewaySetupPanel } from './gateway-launcher-overlay'

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const MERCURY_URL = 'http://mercury2:9119'

const getConnectionConfig = vi.fn<() => Promise<DesktopConnectionConfig>>()
const getRecentLogs = vi.fn<() => Promise<{ lines: string[] }>>()
const oauthLoginConnectionConfig = vi.fn<() => Promise<{ baseUrl: string; connected: boolean; ok: boolean }>>()
const probeConnectionConfig = vi.fn()
const revealLogs = vi.fn()
const applyConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()
const testConnectionConfig = vi.fn()

function connectionConfig(overrides: Partial<DesktopConnectionConfig> = {}): DesktopConnectionConfig {
  return {
    envOverride: false,
    mode: 'remote',
    profile: null,
    remoteAuthMode: 'oauth',
    remoteOauthConnected: true,
    remoteTokenPreview: null,
    remoteTokenSet: false,
    remoteUrl: MERCURY_URL,
    ...overrides
  }
}

function failBoot(error: string) {
  $desktopBoot.set({
    error,
    fakeMode: false,
    message: `Desktop boot failed: ${error}`,
    phase: 'backend.error',
    progress: 24,
    running: false,
    timestamp: Date.now(),
    visible: true
  })
}

function resetStores() {
  clearGatewayRecovery()
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: 'ready',
    phase: 'renderer.ready',
    progress: 100,
    running: false,
    timestamp: Date.now(),
    visible: false
  })
  $desktopOnboarding.set({
    configured: true,
    firstRunSkipped: false,
    flow: { status: 'idle' },
    localEndpoint: false,
    manual: false,
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false
  })
}

beforeEach(() => {
  resetStores()
  getConnectionConfig.mockResolvedValue(connectionConfig())
  getRecentLogs.mockResolvedValue({ lines: [] })
  oauthLoginConnectionConfig.mockResolvedValue({ baseUrl: MERCURY_URL, connected: false, ok: true })
  probeConnectionConfig.mockResolvedValue({
    authMode: 'oauth',
    baseUrl: MERCURY_URL,
    error: null,
    providers: [{ displayName: 'Basic', name: 'basic', supportsPassword: true }],
    reachable: true,
    version: null
  })
  revealLogs.mockResolvedValue(undefined)
  saveConnectionConfig.mockResolvedValue(connectionConfig())
  applyConnectionConfig.mockResolvedValue(connectionConfig())
  testConnectionConfig.mockResolvedValue({ baseUrl: MERCURY_URL, ok: true, version: '0.19.0' })

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      getConnectionConfig,
      getRecentLogs,
      applyConnectionConfig,
      oauthLoginConnectionConfig,
      probeConnectionConfig,
      revealLogs,
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

describe('BootFailureOverlay gateway launcher states', () => {
  it('shows first-run gateway setup when no saved remote URL exists', async () => {
    getConnectionConfig.mockResolvedValue(connectionConfig({ remoteUrl: '' }))
    failBoot('Reuben requires a remote gateway URL. Open Settings -> Gateway and configure one.')

    render(<BootFailureOverlay />)

    expect(await screen.findByText('Choose a gateway')).toBeTruthy()
    expect(screen.getByText(/remote desktop client/i)).toBeTruthy()
    expect(screen.queryByText(/Configured gateway unavailable/i)).toBeNull()
  })

  it('shows configured-gateway unavailable state for a saved URL network failure', async () => {
    failBoot(`Remote Reuben gateway did not become reachable at ${MERCURY_URL}: connect ECONNREFUSED`)

    render(<BootFailureOverlay />)

    expect(await screen.findByText('Configured gateway unavailable')).toBeTruthy()
    expect(screen.getByText(MERCURY_URL)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Configure gateway' })).toBeTruthy()
    expect(screen.getByText(/Connection refused/)).toBeTruthy()
  })

  it('shows configured-gateway sign-in state for missing OAuth session', async () => {
    getConnectionConfig.mockResolvedValue(connectionConfig({ remoteOauthConnected: false }))
    failBoot('Remote Reuben gateway uses login auth, but you are not signed in.')

    render(<BootFailureOverlay />)

    expect(await screen.findByText('Sign in to configured gateway')).toBeTruthy()
    expect(screen.getByText(MERCURY_URL)).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in to remote gateway' }))
    await waitFor(() => expect(oauthLoginConnectionConfig).toHaveBeenCalledWith(MERCURY_URL))
  })

  it('shows unexpected response state for incompatible gateway responses', async () => {
    failBoot(`Expected JSON from ${MERCURY_URL}/api/status but got HTML`)

    render(<BootFailureOverlay />)

    expect(await screen.findByText('Configured gateway returned an unexpected response')).toBeTruthy()
    expect(screen.getByText(MERCURY_URL)).toBeTruthy()
    expect(screen.getAllByText(/did not look like a compatible/).length).toBeGreaterThan(0)
  })

  it('offers and applies the previous confirmed gateway after a failed candidate', async () => {
    getConnectionConfig.mockResolvedValue(connectionConfig({ previousRemoteAuthMode: 'oauth', previousRemoteUrl: 'http://known-good:9119' }))
    failBoot(`Remote Reuben gateway did not become reachable at ${MERCURY_URL}: connect ECONNREFUSED`)
    render(<BootFailureOverlay />)

    fireEvent.click(await screen.findByRole('button', { name: 'Return to previous gateway' }))

    await waitFor(() =>
      expect(applyConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        remoteAuthMode: 'oauth',
        remoteUrl: 'http://known-good:9119'
      })
    )
  })

  it('Change gateway opens an editable form without retrying or probing', async () => {
    failBoot(`Remote Reuben gateway did not become reachable at ${MERCURY_URL}: connect ECONNREFUSED`)
    render(<BootFailureOverlay />)

    fireEvent.click(await screen.findByRole('button', { name: 'Configure gateway' }))

    expect(await screen.findByText('Choose a gateway')).toBeTruthy()
    expect(screen.getByDisplayValue(MERCURY_URL)).toBeTruthy()
    expect(probeConnectionConfig).not.toHaveBeenCalled()
  })
})

describe('GatewaySetupPanel', () => {
  it('typing a gateway URL performs no probe', async () => {
    const onConfigured = vi.fn()

    render(<GatewaySetupPanel onConfigured={onConfigured} />)

    fireEvent.change(screen.getByPlaceholderText('http://mercury2:9119'), { target: { value: MERCURY_URL } })

    expect(probeConnectionConfig).not.toHaveBeenCalled()
    expect(testConnectionConfig).not.toHaveBeenCalled()
    expect(saveConnectionConfig).not.toHaveBeenCalled()
    expect(onConfigured).not.toHaveBeenCalled()
  })

  it('commits a candidate only after the explicit live connection test succeeds', async () => {
    const onConfigured = vi.fn()
    render(<GatewaySetupPanel candidate={{ mode: 'remote', remoteAuthMode: 'oauth', remoteUrl: MERCURY_URL }} onConfigured={onConfigured} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))

    await waitFor(() => expect(testConnectionConfig).toHaveBeenCalledWith(expect.objectContaining({ remoteUrl: MERCURY_URL }), expect.any(Number)))
    await waitFor(() => expect(applyConnectionConfig).toHaveBeenCalledWith(expect.objectContaining({ remoteUrl: MERCURY_URL })))
    expect(onConfigured).toHaveBeenCalled()
  })

  it('does not overwrite the confirmed gateway when candidate validation fails', async () => {
    testConnectionConfig.mockRejectedValueOnce(new Error('WebSocket connection failed'))
    render(<GatewaySetupPanel candidate={{ mode: 'remote', remoteAuthMode: 'oauth', remoteUrl: MERCURY_URL }} onConfigured={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))

    await waitFor(() => expect(testConnectionConfig).toHaveBeenCalled())
    expect(applyConnectionConfig).not.toHaveBeenCalled()
  })
})
