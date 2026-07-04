interface BargeInGateOptions {
  graceMs: number
  level: number
  sustainedMs: number
}

export function createBargeInGate({ graceMs, level, sustainedMs }: BargeInGateOptions) {
  let speechStartedAt: number | null = null

  return {
    update(currentLevel: number, elapsedMs: number): boolean {
      if (elapsedMs < graceMs || currentLevel < level) {
        speechStartedAt = null
        return false
      }

      speechStartedAt ??= elapsedMs
      return elapsedMs - speechStartedAt >= sustainedMs
    }
  }
}
