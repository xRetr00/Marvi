export type IslandCardKind = 'info' | 'result' | 'approval' | 'weather' | 'time'

export interface IslandCardAction {
  id: string
  label: string
  /** Free-text sent back as a user turn, or a choice token resolved by the caller. */
  value?: string
}

export interface IslandCard {
  id: string
  kind: IslandCardKind
  title?: string
  body?: string
  /** Large primary value for glanceable cards, e.g. "24°" or "4:42 PM". */
  value?: string
  duration?: number
  autoDismiss?: boolean
  actions?: IslandCardAction[]
}

export interface IslandQueueSnapshot {
  active: IslandCard | null
  queued: IslandCard[]
}

export interface IslandQueueOptions {
  maxQueue?: number
  onChange?: (snapshot: IslandQueueSnapshot) => void
}

export function createIslandQueue(options: IslandQueueOptions = {}) {
  let active: IslandCard | null = null
  let queued: IslandCard[] = []
  let timer: ReturnType<typeof setTimeout> | null = null

  const snapshot = (): IslandQueueSnapshot => ({ active, queued: [...queued] })
  const emit = () => options.onChange?.(snapshot())

  const clearTimer = () => {
    if (timer) {clearTimeout(timer)}
    timer = null
  }

  const trimQueue = () => {
    if (typeof options.maxQueue !== 'number' || options.maxQueue < 0) {return}

    while (queued.length > options.maxQueue) {queued.shift()}
  }

  const armTimer = () => {
    clearTimer()

    if (!active) {return}
    const autoDismiss = active.autoDismiss ?? false
    const duration = active.duration ?? 0

    if (!autoDismiss || duration <= 0) {return}
    timer = setTimeout(() => {
      if (active) {dismiss(active.id)}
    }, duration)
  }

  const promote = () => {
    active = queued.shift() ?? null
    armTimer()
    emit()
  }

  const show = (card: IslandCard, opts: { force?: boolean } = {}): string => {
    if (opts.force || !active) {
      active = card
      trimQueue()
      armTimer()
      emit()

      return card.id
    }

    queued.push(card)
    trimQueue()
    emit()

    return card.id
  }

  const dismiss = (id?: string) => {
    if (id && active?.id !== id) {
      queued = queued.filter(card => card.id !== id)
      emit()

      return
    }

    clearTimer()
    promote()
  }

  const dismissAll = () => {
    clearTimer()
    active = null
    queued = []
    emit()
  }

  return { show, dismiss, dismissAll, snapshot }
}
