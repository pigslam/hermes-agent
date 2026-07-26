import { atom } from 'nanostores'

import type { DesktopConnectionConfigInput } from '@/global'

export interface GatewayRecoveryState {
  attemptId: number
  candidate: DesktopConnectionConfigInput | null
  error: string | null
  phase: 'idle' | 'connecting_gateway' | 'authenticating' | 'opening_websocket' | 'resolving_backend_configuration'
  stage: 'idle' | 'editing' | 'attempting'
}

let nextAttemptId = 0

export const $gatewayRecovery = atom<GatewayRecoveryState>({
  attemptId: 0,
  candidate: null,
  error: null,
  phase: 'idle',
  stage: 'idle'
})

export function openGatewayRecoveryEditor(candidate: DesktopConnectionConfigInput | null = null) {
  const current = $gatewayRecovery.get()
  $gatewayRecovery.set({
    attemptId: current.attemptId + 1,
    candidate: candidate ?? current.candidate,
    error: null,
    phase: 'idle',
    stage: 'editing'
  })
}

export function startGatewayRecoveryAttempt(candidate: DesktopConnectionConfigInput) {
  const attemptId = Math.max(nextAttemptId + 1, $gatewayRecovery.get().attemptId + 1)
  nextAttemptId = attemptId
  $gatewayRecovery.set({ attemptId, candidate, error: null, phase: 'connecting_gateway', stage: 'attempting' })

  return attemptId
}

export function updateGatewayRecoveryAttempt(attemptId: number, update: Pick<GatewayRecoveryState, 'candidate' | 'phase'>) {
  if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return false}
  $gatewayRecovery.set({ ...$gatewayRecovery.get(), ...update })

  return true
}

export function failGatewayRecoveryAttempt(attemptId: number, error: string) {
  if (!isGatewayRecoveryAttemptCurrent(attemptId)) {return false}
  const current = $gatewayRecovery.get()
  $gatewayRecovery.set({ ...current, attemptId: current.attemptId + 1, error, phase: 'idle', stage: 'editing' })

  return true
}

export function isGatewayRecoveryAttemptCurrent(attemptId: number) {
  const current = $gatewayRecovery.get()

  return current.stage === 'attempting' && current.attemptId === attemptId
}

export function cancelGatewayRecoveryAttempt() {
  const current = $gatewayRecovery.get()
  $gatewayRecovery.set({ ...current, attemptId: current.attemptId + 1, error: null, phase: 'idle', stage: 'editing' })

  return current.attemptId
}

export function clearGatewayRecovery() {
  $gatewayRecovery.set({
    attemptId: $gatewayRecovery.get().attemptId + 1,
    candidate: null,
    error: null,
    phase: 'idle',
    stage: 'idle'
  })
}
