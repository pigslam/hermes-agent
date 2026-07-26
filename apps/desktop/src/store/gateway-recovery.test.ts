import { beforeEach, describe, expect, it } from 'vitest'

import {
  $gatewayRecovery,
  cancelGatewayRecoveryAttempt,
  clearGatewayRecovery,
  isGatewayRecoveryAttemptCurrent,
  startGatewayRecoveryAttempt
} from './gateway-recovery'

const candidate = { authMode: 'oauth' as const, baseUrl: 'http://candidate:9119', mode: 'remote' as const }

describe('gateway recovery attempts', () => {
  beforeEach(() => {
    clearGatewayRecovery()
  })

  it('invalidates a late completion after cancellation while retaining the candidate', () => {
    const attemptId = startGatewayRecoveryAttempt(candidate)

    cancelGatewayRecoveryAttempt()

    expect(isGatewayRecoveryAttemptCurrent(attemptId)).toBe(false)
    expect($gatewayRecovery.get()).toMatchObject({ candidate, stage: 'editing' })
  })
})
