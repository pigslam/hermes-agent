import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { DesktopAuthProvider, DesktopConnectionProbeResult } from '@/global'
import { useI18n } from '@/i18n'
import { AlertCircle, Check, FileText, Globe, Loader2, LogIn } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'
import { $profiles, refreshActiveProfile } from '@/store/profile'

import { CONTROL_TEXT } from './constants'
import { EmptyState, ListRow, LoadingState, Pill, SettingsContent } from './primitives'

type Mode = 'local' | 'remote'
type AuthMode = 'oauth' | 'token'
type ProbeStatus = 'idle' | 'probing' | 'done' | 'error'

interface GatewaySettingsState {
  envOverride: boolean
  mode: Mode
  remoteAuthMode: AuthMode
  remoteOauthConnected: boolean
  remoteTokenPreview: string | null
  remoteTokenSet: boolean
  remoteUrl: string
}

const EMPTY_STATE: GatewaySettingsState = {
  envOverride: false,
  mode: 'remote',
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: ''
}

function ScopeChip({ active, label, onSelect }: { active: boolean; label: string; onSelect: () => void }) {
  return (
    <button
      className={cn(
        'rounded-full border px-3 py-1 text-[length:var(--conversation-caption-font-size)] transition',
        active
          ? 'border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) text-(--ui-text-primary)'
          : 'border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)'
      )}
      onClick={onSelect}
      type="button"
    >
      {label}
    </button>
  )
}

export function GatewaySettings() {
  const { t } = useI18n()
  const g = t.settings.gateway
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [state, setState] = useState<GatewaySettingsState>(EMPTY_STATE)
  const [remoteToken, setRemoteToken] = useState('')
  const [lastTest, setLastTest] = useState<null | string>(null)

  // Connection scope: null = the global/default connection (the original
  // behavior); a profile name = that profile's per-profile remote override, so
  // each profile can point at its own backend.
  const [scope, setScope] = useState<null | string>(null)
  const profiles = useStore($profiles)

  useEffect(() => {
    void refreshActiveProfile()
  }, [])

  // Editing is inert. Probe only from an explicit Test or Sign In action.
  const [probeStatus, setProbeStatus] = useState<ProbeStatus>('idle')
  const [probe, setProbe] = useState<DesktopConnectionProbeResult | null>(null)

  useEffect(() => {
    let cancelled = false
    const desktop = window.hermesDesktop

    if (!desktop?.getConnectionConfig) {
      setLoading(false)

      return () => void (cancelled = true)
    }

    setLoading(true)
    // Clear scope-local entry state so a token from one scope can't leak into
    // the next when switching profiles.
    setRemoteToken('')
    setLastTest(null)

    desktop
      .getConnectionConfig(scope)
      .then(config => {
        if (cancelled) {
          return
        }

        setState({ ...config, mode: 'remote' })
      })
      .catch(err => notifyError(err, g.failedLoad))
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => void (cancelled = true)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload on scope change only; copy is stable
  }, [scope])

  const trimmedUrl = state.remoteUrl.trim()

  const probeGateway = async () => {
    if (!trimmedUrl || !/^https?:\/\//i.test(trimmedUrl)) {
      setProbe(null)
      setProbeStatus('error')
      notify({ kind: 'warning', title: g.incompleteTitle, message: g.enterUrlFirst })

      return null
    }

    const desktop = window.hermesDesktop

    if (!desktop?.probeConnectionConfig) {return null}
    setProbeStatus('probing')

    try {
      const result = await desktop.probeConnectionConfig(trimmedUrl)
      setProbe(result)
      setProbeStatus(result.reachable ? 'done' : 'error')
      const detectedAuthMode = result.authMode

      if (detectedAuthMode === 'oauth' || detectedAuthMode === 'token') {
        setState(current => ({ ...current, remoteAuthMode: detectedAuthMode }))
      }

      return result
    } catch (err) {
      setProbe(null)
      setProbeStatus('error')
      notifyError(err, g.testFailed)

      return null
    }
  }

  // Effective auth mode: a reachable probe wins; otherwise fall back to the
  // saved config's mode so a re-open of settings doesn't flicker.
  const authMode: AuthMode = useMemo(() => {
    if (probeStatus === 'done' && probe && probe.authMode !== 'unknown') {
      return probe.authMode
    }

    return state.remoteAuthMode
  }, [probe, probeStatus, state.remoteAuthMode])

  const providerLabel = useMemo(() => {
    const providers: DesktopAuthProvider[] = probe?.providers ?? []

    if (providers.length === 1) {
      return providers[0].displayName || providers[0].name
    }

    if (providers.length > 1) {
      return providers.map(p => p.displayName || p.name).join(' / ')
    }

    return t.boot.failure.identityProvider
  }, [probe, t.boot.failure.identityProvider])

  // A username/password gateway authenticates through a credential form on the
  // gateway's /login page (POST /auth/password-login) rather than an OAuth
  // redirect. Everything downstream — the session cookie, the ws-ticket mint,
  // the persistent partition — is identical, so the desktop drives it through
  // the same sign-in window; only the button copy changes. We treat the
  // gateway as password-style only when EVERY advertised provider supports
  // password, so a mixed deployment keeps the generic OAuth copy.
  const isPasswordProvider = useMemo(() => {
    const providers: DesktopAuthProvider[] = probe?.providers ?? []

    return providers.length > 0 && providers.every(p => p.supportsPassword)
  }, [probe])

  // The 'default' profile uses the global ("All profiles") connection, so the
  // per-profile scopes are the named, non-default profiles.
  const namedProfiles = useMemo(() => profiles.filter(profile => profile.name !== 'default'), [profiles])

  const oauthConnected = state.remoteOauthConnected

  const canSaveRemote = useMemo(() => {
    if (!trimmedUrl) {
      return false
    }

    if (authMode === 'oauth') {
      return true
    }

    return Boolean(remoteToken.trim()) || state.remoteTokenSet
  }, [authMode, remoteToken, state.remoteTokenSet, trimmedUrl])

  const payload = () => ({
    mode: 'remote' as const,
    profile: scope ?? undefined,
    remoteAuthMode: authMode,
    remoteToken: authMode === 'token' ? remoteToken.trim() || undefined : undefined,
    remoteUrl: trimmedUrl
  })

  const save = async (apply: boolean) => {
    if (!canSaveRemote) {
      notify({
        kind: 'warning',
        title: g.incompleteTitle,
        message: authMode === 'oauth' ? g.enterUrlFirst : g.incompleteToken
      })

      return
    }

    setSaving(true)

    try {
      // A candidate remains in component state until the full remote path
      // succeeds: HTTP status, auth/ticket, and a live /api/ws connection.
      await window.hermesDesktop.testConnectionConfig(payload())

      const next = apply
        ? await window.hermesDesktop.applyConnectionConfig(payload())
        : await window.hermesDesktop.saveConnectionConfig(payload())

      setState(next)
      setRemoteToken('')
      notify({
        kind: 'success',
        title: apply ? g.restartingTitle : g.savedTitle,
        message: apply ? g.restartingMessage : g.savedMessage
      })
    } catch (err) {
      notifyError(err, apply ? g.applyFailed : g.saveFailed)
    } finally {
      setSaving(false)
    }
  }

  // OAuth sign-in: persist the URL + oauth mode first (so the saved config has
  // the URL the login window needs), then open the gateway login window and
  // refresh the connection status from the saved config once it completes.
  const signIn = async () => {
    if (!trimmedUrl) {
      notify({ kind: 'warning', title: g.incompleteTitle, message: g.enterUrlFirst })

      return
    }

    setSigningIn(true)

    try {
      const result = await probeGateway()

      if (!result?.reachable || result.authMode !== 'oauth') {return}

      const login = await window.hermesDesktop.oauthLoginConnectionConfig(trimmedUrl)

      if (login.connected) {
        setState(current => ({ ...current, remoteAuthMode: 'oauth', remoteOauthConnected: true }))
        notify({ kind: 'success', title: g.signedIn, message: g.connectedTo(providerLabel) })
      } else {
        notify({
          kind: 'warning',
          title: t.boot.failure.signInIncompleteTitle,
          message: t.boot.failure.signInIncompleteMessage
        })
      }
    } catch (err) {
      notifyError(err, g.signInFailed)
    } finally {
      setSigningIn(false)
    }
  }

  const signOut = async () => {
    setSigningIn(true)

    try {
      await window.hermesDesktop.oauthLogoutConnectionConfig(trimmedUrl || undefined)
      const refreshed = await window.hermesDesktop.getConnectionConfig(scope)
      setState(refreshed)
      notify({ kind: 'success', title: g.signedOutTitle, message: g.signedOutMessage })
    } catch (err) {
      notifyError(err, g.signOutFailed)
    } finally {
      setSigningIn(false)
    }
  }

  const testRemote = async () => {
    setTesting(true)
    setLastTest(null)

    try {
      const probed = await probeGateway()

      if (!probed?.reachable) {return}
      const testedAuthMode = probed.authMode === 'unknown' ? authMode : probed.authMode

      if (testedAuthMode === 'oauth' && !oauthConnected) {
        notify({ kind: 'warning', title: g.incompleteTitle, message: g.incompleteSignInTest })

        return
      }

      if (testedAuthMode === 'token' && !remoteToken.trim() && !state.remoteTokenSet) {
        notify({ kind: 'warning', title: g.incompleteTitle, message: g.incompleteTokenTest })

        return
      }

      const result = await window.hermesDesktop.testConnectionConfig({
        mode: 'remote',
        profile: scope ?? undefined,
        remoteAuthMode: authMode,
        remoteToken: authMode === 'token' ? remoteToken.trim() || undefined : undefined,
        remoteUrl: trimmedUrl
      })

      const message = g.connectedTo(result.baseUrl, result.version ?? undefined)
      setLastTest(message)
      notify({ kind: 'success', title: g.reachableTitle, message })
    } catch (err) {
      notifyError(err, g.testFailed)
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return <LoadingState label={g.loading} />
  }

  if (!window.hermesDesktop?.getConnectionConfig) {
    return <EmptyState description={g.unavailableDesc} title={g.unavailableTitle} />
  }

  return (
    <SettingsContent>
      <div className="mb-5">
        <div className="flex items-center gap-2 text-[length:var(--conversation-text-font-size)] font-medium">
          <Globe className="size-4 text-muted-foreground" />
          {g.title}
          {state.envOverride ? <Pill tone="primary">{g.envOverride}</Pill> : null}
        </div>
        <p className="mt-2 max-w-2xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
          {g.intro}
        </p>
      </div>

      {namedProfiles.length > 0 ? (
        <div className="mb-5 grid gap-2">
          <div className="text-[length:var(--conversation-caption-font-size)] font-medium text-(--ui-text-secondary)">
            {g.appliesTo}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <ScopeChip active={scope === null} label={g.allProfiles} onSelect={() => setScope(null)} />
            {namedProfiles.map(profile => (
              <ScopeChip
                active={scope === profile.name}
                key={profile.name}
                label={profile.name}
                onSelect={() => setScope(profile.name)}
              />
            ))}
          </div>
          <p className="text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
            {scope === null ? g.defaultConnection : g.profileConnection(scope)}
          </p>
        </div>
      ) : null}

      {state.envOverride ? (
        <div className="mb-5 flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[length:var(--conversation-caption-font-size)] text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <div>
            <div className="font-medium">{g.envOverrideTitle}</div>
            <div className="mt-1 leading-5">{g.envOverrideDesc}</div>
          </div>
        </div>
      ) : null}

      <div className="mt-5 grid gap-1">
        <ListRow
          action={
            <Input
              className={cn('h-8', CONTROL_TEXT)}
              disabled={state.envOverride}
              onChange={event => {
                setState(current => ({ ...current, remoteUrl: event.target.value }))
                setProbe(null)
                setProbeStatus('idle')
              }}
              placeholder="https://gateway.example.com/hermes"
              value={state.remoteUrl}
            />
          }
          description={g.remoteUrlDesc}
          title={g.remoteUrlTitle}
        />

        <ListRow
          action={
            <select
              className={cn('h-8 rounded-[4px] border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) px-2', CONTROL_TEXT)}
              disabled={state.envOverride}
              onChange={event => setState(current => ({ ...current, remoteAuthMode: event.target.value as AuthMode }))}
              value={state.remoteAuthMode}
            >
              <option value="oauth">Sign-in session</option>
              <option value="token">Session token</option>
            </select>
          }
          description="Choose the expected gateway authentication, or use Test to detect it."
          title="Authentication"
        />

        {probeStatus === 'probing' ? (
          <div className="flex items-center gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
            <Loader2 className="size-4 animate-spin" />
            {g.probing}
          </div>
        ) : null}

        {probeStatus === 'error' ? (
          <div className="flex items-start gap-2 py-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            {g.probeError}
          </div>
        ) : null}

        {/* OAuth / password gateways: present a sign-in button + connection status. */}
        {authMode === 'oauth' ? (
          <ListRow
            action={
              oauthConnected ? (
                <div className="flex items-center gap-2">
                  <Pill tone="primary">
                    <Check className="size-3" /> {g.signedIn}
                  </Pill>
                  <Button disabled={signingIn} onClick={() => void signOut()} variant="outline">
                    {signingIn ? <Loader2 className="animate-spin" /> : null}
                    {g.signOut}
                  </Button>
                </div>
              ) : (
                <Button disabled={signingIn || !trimmedUrl} onClick={() => void signIn()}>
                  {signingIn ? <Loader2 className="animate-spin" /> : <LogIn />}
                  {isPasswordProvider ? g.signIn : g.signInWith(providerLabel)}
                </Button>
              )
            }
            description={
              oauthConnected
                ? isPasswordProvider
                  ? g.authSignedInPassword
                  : g.authSignedInOauth
                : isPasswordProvider
                  ? g.authNeedsPassword
                  : g.authNeedsOauth(providerLabel)
            }
            title={g.authTitle}
          />
        ) : null}

        {/* Session-token gateways: keep the existing token entry box. */}
        {authMode === 'token' ? (
          <ListRow
            action={
              <Input
                autoComplete="off"
                className={cn('h-8 font-mono', CONTROL_TEXT)}
                disabled={state.envOverride}
                onChange={event => setRemoteToken(event.target.value)}
                placeholder={
                  state.remoteTokenSet ? g.existingToken(state.remoteTokenPreview ?? g.savedToken) : g.pasteSessionToken
                }
                type="password"
                value={remoteToken}
              />
            }
            description={g.tokenDesc}
            title={g.tokenTitle}
          />
        ) : null}
      </div>

      {lastTest ? <div className="mt-4 text-xs text-primary">{lastTest}</div> : null}

      <div className="mt-6 flex flex-wrap items-center justify-end gap-4">
        <Button
          className="mr-auto"
          disabled={testing || !trimmedUrl}
          onClick={() => void testRemote()}
          size="sm"
          variant="text"
        >
          {testing ? <Loader2 className="animate-spin" /> : null}
          {g.testRemote}
        </Button>
        <Button disabled={saving || !canSaveRemote} onClick={() => void save(false)} size="sm" variant="textStrong">
          {g.saveForRestart}
        </Button>
        <Button disabled={saving || !canSaveRemote} onClick={() => void save(true)} size="sm">
          {saving ? <Loader2 className="animate-spin" /> : null}
          {g.saveAndReconnect}
        </Button>
      </div>

      <div className="mt-6 grid gap-1">
        <ListRow
          action={
            <Button onClick={() => void window.hermesDesktop?.revealLogs()} size="sm" variant="textStrong">
              <FileText />
              {g.openLogs}
            </Button>
          }
          description={g.diagnosticsDesc}
          title={g.diagnostics}
        />
      </div>
    </SettingsContent>
  )
}
