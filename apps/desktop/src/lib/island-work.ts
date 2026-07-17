export type IslandWorkItemState = 'done' | 'failed' | 'pending' | 'running'

export interface IslandWorkItem {
  id: string
  meta?: string
  state: IslandWorkItemState
  title: string
}

export interface IslandWorkState {
  active: boolean
  items: IslandWorkItem[]
  title: string
}
