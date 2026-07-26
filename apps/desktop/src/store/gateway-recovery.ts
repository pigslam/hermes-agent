import { atom } from 'nanostores'

import type { DesktopConnectionConfigInput } from '@/global'

export interface GatewayRecoveryState {
  attemptId: number
  candidate: DesktopConnectionConfigInput | null
  stage: 'idle' | 'editing' | 'attempting'
}

let nextAttemptId = 0

export const $gatewayRecovery = atom<GatewayRecoveryState>({ attemptId: 0, candidate: null, stage: 'idle' })

export function openGatewayRecoveryEditor(candidate: DesktopConnectionConfigInput | null = null) {
  const current = $gatewayRecovery.get()
  $gatewayRecovery.set({ attemptId: current.attemptId + 1, candidate: candidate ?? current.candidate, stage: 'editing' })
}

export function startGatewayRecoveryAttempt(candidate: DesktopConnectionConfigInput) {
  const attemptId = Math.max(nextAttemptId + 1, $gatewayRecovery.get().attemptId + 1)
  nextAttemptId = attemptId
  $gatewayRecovery.set({ attemptId, candidate, stage: 'attempting' })

  return attemptId
}

export function isGatewayRecoveryAttemptCurrent(attemptId: number) {
  const current = $gatewayRecovery.get()

  return current.stage === 'attempting' && current.attemptId === attemptId
}

export function cancelGatewayRecoveryAttempt() {
  const current = $gatewayRecovery.get()
  $gatewayRecovery.set({ ...current, attemptId: current.attemptId + 1, stage: 'editing' })

  return current.attemptId
}

export function clearGatewayRecovery() {
  $gatewayRecovery.set({ attemptId: $gatewayRecovery.get().attemptId + 1, candidate: null, stage: 'idle' })
}
