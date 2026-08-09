/** Smart Room's always-visible vision status, backed by its authenticated API. */

import {
  cn,
  Codicon,
  type HermesPlugin,
  host,
  STATUSBAR_AREAS,
  Tip,
  useQuery
} from '@hermes/plugin-sdk'

interface StatusPayload {
  runtime: { alive?: boolean; ready?: boolean }
  state: null | {
    vision?: {
      active_gesture?: null | string
      camera_online?: boolean
      owner_visible?: boolean
      person_count?: number
      sleep_state?: string
    }
  }
}

function VisionStatus({ request }: { request: () => Promise<StatusPayload> }) {
  const { data } = useQuery({
    queryFn: request,
    queryKey: ['smart-room', 'vision-status'],
    refetchInterval: 5_000
  })

  const vision = data?.state?.vision
  const online = !!data?.runtime?.alive && data.runtime.ready !== false && !!vision?.camera_online

  const detail = !data?.runtime?.alive
    ? 'room offline'
    : !vision?.camera_online
      ? 'camera offline'
      : vision.active_gesture
        ? vision.active_gesture
        : vision.owner_visible
          ? `owner · ${vision.sleep_state || 'awake'}`
          : `${vision.person_count ?? 0} people`

  return (
    <Tip label={`Smart Room vision — ${detail}. Click to open camera, face, and gesture settings.`}>
      <button
        className={cn(
          'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] transition-colors',
          'hover:bg-(--chrome-action-hover) hover:text-foreground',
          online ? 'text-emerald-400' : 'text-amber-400'
        )}
        onClick={() => host.navigate('/settings?tab=smart-room')}
        type="button"
      >
        <Codicon name="device-camera" size="0.7rem" />
        <span className="max-w-40 truncate">Vision: {detail}</span>
      </button>
    </Tip>
  )
}

const plugin: HermesPlugin = {
  id: 'smart_room',
  name: 'Smart Room',
  register(ctx) {
    ctx.register({
      id: 'vision-status',
      area: STATUSBAR_AREAS.right,
      order: 75,
      render: () => <VisionStatus request={() => ctx.rest<StatusPayload>('/status')} />
    })
  }
}

export default plugin
