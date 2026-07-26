import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { LogView } from '@/components/ui/log-view'
import type { DesktopConnectionConfigInput, DesktopConnectionProbeResult } from '@/global'
import { AlertCircle, Check, FileText, Globe, Loader2, LogIn, RefreshCw, Settings2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import {
  failGatewayRecoveryAttempt,
  isGatewayRecoveryAttemptCurrent,
  startGatewayRecoveryAttempt,
  updateGatewayRecoveryAttempt
} from '@/store/gateway-recovery'
import { notify, notifyError } from '@/store/notifications'

import type { GatewayStartupKind } from './gateway-launcher-state'

const COMMON_PORTS = ['9119', '8000', '3000']

function assetPath(path: string) {
  return `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`
}

function normalizePathPrefix(value: string) {
  const trimmed = value.trim()

  if (!trimmed) {return ''}

  return `/${trimmed.replace(/^\/+/, '').replace(/\/+$/, '')}`
}

function buildRemoteUrl({ host, pathPrefix, port, scheme }: { host: string; pathPrefix: string; port: string; scheme: string }) {
  const cleanHost = host
    .trim()
    .replace(/^https?:\/\//i, '')
    .replace(/\/.*$/, '')

  const cleanPort = port.trim()
  const portSuffix = cleanPort ? `:${cleanPort.replace(/^:+/, '')}` : ''

  if (!cleanHost) {
    return ''
  }

  return `${scheme}://${cleanHost}${portSuffix}${normalizePathPrefix(pathPrefix)}`
}

function isPlausibleUrl(value: string) {
  return /^https?:\/\/[^/\s:]+(?::\d+)?(?:\/\S*)?$/i.test(value.trim())
}

interface GatewaySetupPanelProps {
  candidate?: DesktopConnectionConfigInput | null
  initialError?: string | null
  onBack?: () => void
  onConfigured: () => void
}

export function GatewaySetupPanel({ candidate, initialError = null, onBack, onConfigured }: GatewaySetupPanelProps) {
  const [scheme, setScheme] = useState('http')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('9119')
  const [pathPrefix, setPathPrefix] = useState('')
  const [remoteUrl, setRemoteUrl] = useState(candidate?.remoteUrl ?? '')
  const [remoteToken, setRemoteToken] = useState(candidate?.remoteToken ?? '')
  const [selectedAuthMode, setSelectedAuthMode] = useState<'oauth' | 'token'>(candidate?.remoteAuthMode ?? 'oauth')
  const [probe, setProbe] = useState<DesktopConnectionProbeResult | null>(null)
  const [probing, setProbing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(initialError)

  const constructedUrl = useMemo(
    () => buildRemoteUrl({ host, pathPrefix, port, scheme }),
    [host, pathPrefix, port, scheme]
  )

  const trimmedUrl = remoteUrl.trim()
  const authMode = probe?.reachable && probe.authMode !== 'unknown' ? probe.authMode : selectedAuthMode
  const needsToken = authMode === 'token'

  const payload = (mode = authMode): DesktopConnectionConfigInput => ({
    mode: 'remote',
    remoteAuthMode: mode,
    remoteToken: mode === 'token' ? remoteToken.trim() || undefined : undefined,
    remoteUrl: trimmedUrl
  })

  const requireUrl = () => {
    if (isPlausibleUrl(trimmedUrl)) {return true}
    setError('Enter a full gateway URL, for example http://mercury2:9119.')

    return false
  }

  // Deliberately explicit: editing a URL is inert. This is the only probe path.
  const probeGateway = async () => {
    const desktop = window.hermesDesktop

    if (!desktop?.probeConnectionConfig) {
      setError('Desktop gateway settings are unavailable.')

      return null
    }

    if (!requireUrl()) {return null}
    setProbing(true)
    setError(null)

    try {
      const result = await desktop.probeConnectionConfig(trimmedUrl)
      setProbe(result)

      if (result.authMode !== 'unknown') {setSelectedAuthMode(result.authMode)}

      if (!result.reachable) {setError(result.error || 'Could not reach this gateway.')}

      return result
    } catch (err) {
      setProbe(null)
      setError(err instanceof Error ? err.message : String(err))

      return null
    } finally {
      setProbing(false)
    }
  }

  const signIn = async () => {
    const result = await probeGateway()

    if (!result?.reachable || result.authMode !== 'oauth') {return}
    setSaving(true)

    try {
      const login = await window.hermesDesktop?.oauthLoginConnectionConfig(trimmedUrl)

      if (!login?.connected) {setError('Sign-in did not complete. You can retry or edit the gateway URL.')}
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const connect = async () => {
    const desktop = window.hermesDesktop

    if (!desktop?.applyConnectionConfig || !desktop.testConnectionConfig) {
      setError('Desktop gateway settings are unavailable.')

      return
    }

    if (!requireUrl()) {return}

    const initialCandidate = payload()
    const attemptId = startGatewayRecoveryAttempt(initialCandidate)
    setSaving(true)
    setError(null)

    try {
      const result = await desktop.probeConnectionConfig?.(trimmedUrl)

      if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return}

      if (!result?.reachable || result.authMode === 'unknown') {
        throw new Error(result?.error || 'Could not reach this gateway.')
      }

      const detectedAuthMode = result.authMode
      const nextCandidate = payload(detectedAuthMode)
      setProbe(result)
      setSelectedAuthMode(detectedAuthMode)
      updateGatewayRecoveryAttempt(attemptId, { candidate: nextCandidate, phase: 'authenticating' })

      if (detectedAuthMode === 'token' && !remoteToken.trim()) {
        throw new Error('This gateway expects a session token. Paste the token before connecting.')
      }

      if (detectedAuthMode === 'oauth') {
        const session = await desktop.oauthSessionConnectionConfig?.(trimmedUrl)

        if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return}

        if (!session?.connected) {
          const login = await desktop.oauthLoginConnectionConfig(trimmedUrl, attemptId)

          if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return}

          if (!login.connected) {
            throw new Error('Sign-in was cancelled or did not complete. Edit the gateway and try again.')
          }
        }
      }

      updateGatewayRecoveryAttempt(attemptId, { candidate: nextCandidate, phase: 'opening_websocket' })
      // Test validates status, required auth/ticket, and a live /api/ws upgrade.
      // Only after it succeeds do we replace the confirmed configuration.
      await desktop.testConnectionConfig(nextCandidate, attemptId)

      if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return}
      updateGatewayRecoveryAttempt(attemptId, { candidate: nextCandidate, phase: 'resolving_backend_configuration' })
      await desktop.applyConnectionConfig(nextCandidate)

      if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return}
      notify({ kind: 'success', title: 'Gateway connected', message: 'Reconnecting to the confirmed gateway…' })
      onConfigured()
    } catch (err) {
      if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return}
      const message = err instanceof Error ? err.message : String(err)
      failGatewayRecoveryAttempt(attemptId, message)
      notifyError(err, 'Could not connect gateway')
      setError(message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="grid gap-5">
      <div className="flex items-center gap-4">
        <img alt="" className="size-16 rounded-2xl border border-(--ui-stroke-tertiary)" src={assetPath('reuben-icon.png')} />
        <div>
          <div className="text-[0.68rem] font-semibold uppercase tracking-[0.26em] text-primary">Reuben Agent</div>
          <h1 className="mt-1 text-xl font-semibold tracking-tight text-(--ui-text-primary)">Choose a gateway</h1>
          <p className="mt-1 max-w-[34rem] text-sm leading-6 text-(--ui-text-tertiary)">
            Reuben is a remote desktop client. Connect it to an already-running Reuben or Hermes gateway to begin.
          </p>
        </div>
      </div>

      <div className="grid gap-3 rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-bg-tertiary)/45 p-4">
        <div className="grid gap-3 sm:grid-cols-[1fr_7rem_8rem]">
          <label className="grid gap-1.5 text-xs font-medium text-(--ui-text-secondary)">
            Hostname
            <Input
              onChange={event => setHost(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter' && constructedUrl) {
                  setRemoteUrl(constructedUrl)
                }
              }}
              placeholder="mercury2"
              value={host}
            />
          </label>
          <label className="grid gap-1.5 text-xs font-medium text-(--ui-text-secondary)">
            Scheme
            <select
              className="h-8 rounded-[4px] border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) px-2 text-sm text-(--ui-text-primary)"
              onChange={event => setScheme(event.target.value)}
              value={scheme}
            >
              <option value="http">http</option>
              <option value="https">https</option>
            </select>
          </label>
          <label className="grid gap-1.5 text-xs font-medium text-(--ui-text-secondary)">
            Port
            <Input onChange={event => setPort(event.target.value)} placeholder="9119" value={port} />
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {COMMON_PORTS.map(value => (
            <Button key={value} onClick={() => setPort(value)} size="xs" type="button" variant={port === value ? 'secondary' : 'outline'}>
              {value}
            </Button>
          ))}
          <Input
            className="h-7 max-w-[13rem]"
            onChange={event => setPathPrefix(event.target.value)}
            placeholder="/hermes path prefix"
            value={pathPrefix}
          />
          <Button disabled={!constructedUrl} onClick={() => setRemoteUrl(constructedUrl)} size="xs" type="button">
            Use constructed URL
          </Button>
        </div>
      </div>

      <label className="grid gap-1.5 text-xs font-medium text-(--ui-text-secondary)">
        Full gateway URL
        <Input
          autoFocus
          className="font-mono"
              onChange={event => {
                setRemoteUrl(event.target.value)
                setProbe(null)
                setError(null)
              }}
          onKeyDown={event => {
            if (event.key === 'Enter') {void connect()}
          }}
          placeholder="http://mercury2:9119"
          value={remoteUrl}
        />
      </label>

      {probing ? (
        <div className="flex items-center gap-2 text-xs text-(--ui-text-tertiary)">
          <Loader2 className="size-3.5 animate-spin" />
          Checking gateway…
        </div>
      ) : probe?.reachable ? (
        <div className="flex items-center gap-2 text-xs text-primary">
          <Check className="size-3.5" />
          Gateway reachable. Authentication: {authMode === 'oauth' ? 'sign-in session' : 'session token'}.
        </div>
      ) : probe?.error ? (
        <div className="flex items-start gap-2 text-xs text-(--ui-text-tertiary)">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          {probe.error}
        </div>
      ) : null}

      <label className="grid gap-1.5 text-xs font-medium text-(--ui-text-secondary)">
        Authentication
        <select
          className="h-8 rounded-[4px] border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) px-2 text-sm text-(--ui-text-primary)"
          onChange={event => setSelectedAuthMode(event.target.value as 'oauth' | 'token')}
          value={selectedAuthMode}
        >
          <option value="oauth">Sign-in session</option>
          <option value="token">Session token</option>
        </select>
      </label>

      {needsToken ? (
        <label className="grid gap-1.5 text-xs font-medium text-(--ui-text-secondary)">
          Session token
          <Input
            className="font-mono"
            onChange={event => setRemoteToken(event.target.value)}
            placeholder="Paste session token"
            type="password"
            value={remoteToken}
          />
        </label>
      ) : null}

      {error ? <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">{error}</div> : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-[28rem] text-xs leading-5 text-(--ui-text-tertiary)">
          Editing is local to this form. Test or Connect explicitly; a failed candidate never replaces the confirmed gateway.
        </p>
        <div className="flex flex-wrap gap-2">
          {onBack ? <Button onClick={onBack} type="button" variant="secondary">Back</Button> : null}
          <Button disabled={probing || saving || !trimmedUrl} onClick={() => void probeGateway()} type="button" variant="outline">
            {probing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            Test
          </Button>
          {authMode === 'oauth' ? <Button disabled={saving || !trimmedUrl} onClick={() => void signIn()} type="button" variant="secondary"><LogIn />Sign In</Button> : null}
          <Button disabled={saving || !trimmedUrl} onClick={() => void connect()} type="button">
          {saving ? <Loader2 className="animate-spin" /> : <Globe />}
            Connect
          </Button>
        </div>
      </div>
    </div>
  )
}

interface GatewayRecoveryPanelProps {
  busy: 'retry' | 'signin' | null
  diagnostic: string
  kind: Exclude<GatewayStartupKind, 'setup'>
  logs: string[]
  onOpenLogs: () => void
  onRestorePrevious?: () => void
  onRetry: () => void
  onSettings: () => void
  onSignIn: () => void
  remoteUrl: string
  showLogs: boolean
  signInLabel: string
  toggleLogs: () => void
}

export function GatewayRecoveryPanel({
  busy,
  diagnostic,
  kind,
  logs,
  onOpenLogs,
  onRestorePrevious,
  onRetry,
  onSettings,
  onSignIn,
  remoteUrl,
  showLogs,
  signInLabel,
  toggleLogs
}: GatewayRecoveryPanelProps) {
  const copy = {
    signin: {
      title: 'Sign in to configured gateway',
      body: 'The configured gateway is reachable, but Reuben needs a fresh gateway session before it can continue.',
      hint: 'The next step opens the remote gateway sign-in page. Reuben keeps the session in its own desktop cookie jar.'
    },
    unavailable: {
      title: 'Configured gateway unavailable',
      body: 'Reuben has a saved default backend and is trying to reconnect to it.',
      hint: 'Check the host, port, VPN, proxy, or remote service status, then retry.'
    },
    unexpected: {
      title: 'Configured gateway returned an unexpected response',
      body: 'The host responded, but it did not look like a compatible Reuben/Hermes gateway.',
      hint: 'Confirm the URL points at the gateway root and preserves any required path prefix.'
    }
  }[kind]

  return (
    <div className="grid gap-5">
      <div className="flex items-center gap-4">
        <img alt="" className="size-14 rounded-2xl border border-(--ui-stroke-tertiary)" src={assetPath('reuben-icon.png')} />
        <div>
          <div className="text-[0.68rem] font-semibold uppercase tracking-[0.26em] text-primary">Reuben Agent</div>
          <h1 className="mt-1 text-xl font-semibold tracking-tight text-(--ui-text-primary)">{copy.title}</h1>
          <p className="mt-1 max-w-[34rem] text-sm leading-6 text-(--ui-text-tertiary)">{copy.body}</p>
        </div>
      </div>

      <div className="grid gap-3 rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-bg-tertiary)/45 p-4">
        <div className="text-xs font-medium uppercase tracking-[0.18em] text-(--ui-text-tertiary)">Default backend</div>
        <div className="break-all font-mono text-sm text-(--ui-text-primary)">{remoteUrl}</div>
      </div>

      <div
        className={cn(
          'rounded-xl border px-4 py-3 text-xs leading-5',
          kind === 'signin'
            ? 'border-primary/30 bg-primary/10 text-(--ui-text-secondary)'
            : 'border-destructive/30 bg-destructive/10 text-destructive'
        )}
      >
        <div className="font-medium">{kind === 'signin' ? 'Authentication required' : 'Diagnostic'}</div>
        <div className="mt-1">{diagnostic}</div>
      </div>

      <div className="grid gap-2">
        <div className="flex flex-wrap gap-2">
          {kind === 'signin' ? (
            <Button disabled={Boolean(busy)} onClick={onSignIn}>
              {busy === 'signin' ? <Loader2 className="animate-spin" /> : <LogIn />}
              {signInLabel}
            </Button>
          ) : (
            <Button disabled={Boolean(busy)} onClick={onRetry}>
              {busy === 'retry' ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              Retry
            </Button>
          )}
          <Button disabled={Boolean(busy)} onClick={onSettings} variant="secondary">
            <Settings2 />
            {kind === 'signin' ? 'Change gateway' : 'Configure gateway'}
          </Button>
          {onRestorePrevious ? (
            <Button disabled={Boolean(busy)} onClick={onRestorePrevious} variant="outline">
              <RefreshCw />
              Return to previous gateway
            </Button>
          ) : null}
          <Button onClick={onOpenLogs} variant="ghost">
            <FileText />
            Open logs
          </Button>
        </div>
        <p className="text-xs text-(--ui-text-tertiary)">{copy.hint}</p>
      </div>

      {logs.length > 0 ? (
        <div className="grid gap-2">
          <Button className="-ml-2 self-start font-medium" onClick={toggleLogs} size="xs" type="button" variant="text">
            {showLogs ? 'Hide recent logs' : 'Show recent logs'}
          </Button>
          {showLogs ? <LogView className="max-h-48">{logs.slice(-40).join('')}</LogView> : null}
        </div>
      ) : null}
    </div>
  )
}
