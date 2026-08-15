/**
 * Wiring surfaces — each pane is its own memoized component. Every surface
 * reads the reactive state it renders from at the leaf (its own atom
 * subscriptions) and reaches the controller's callbacks through the stable
 * `actions` bag, so a state change scoped to one surface (or a bare
 * wiring-controller tick) never re-renders another. This is what keeps the
 * layout tree's zones independently rendered — the whole point of the shell.
 */

import { useStore } from '@nanostores/react'
import { type ComponentProps, lazy, memo, type ReactNode, Suspense, useMemo } from 'react'
import { Navigate, Route, Routes, useParams } from 'react-router'

import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { cn } from '@/lib/utils'
import type { WakeWordConfig } from '@/lib/wake-word'
import { $activeGatewayProfile } from '@/store/profile'
import { $freshDraftReady, $gatewayState } from '@/store/session'
import { $voicePlayback } from '@/store/voice-playback'
import { $wakeStatus } from '@/store/voice-presence'
import { $presenceEnabled, setPresenceEnabled } from '@/store/voice-presence-settings'
import { $voiceWarmup } from '@/store/voice-warmup'

import { ChatView } from '../chat'
import { ChatSidebar } from '../chat/sidebar'
import { TerminalPaneChrome } from '../right-sidebar/terminal/chrome'
import { contributedRoutes, NEW_CHAT_ROUTE, OVERLAY_ROUTES, ROUTES_AREA, sessionRoute } from '../routes'
import { useStatusSnapshot } from '../shell/hooks/use-status-snapshot'
import { useStatusbarItems } from '../shell/hooks/use-statusbar-items'
import { ModelMenuPanel } from '../shell/model-menu-panel'
import { StatusbarControls } from '../shell/statusbar-controls'
import type { StatusbarItem } from '../shell/statusbar-controls'

import { latestChatActions, latestSidebarActions } from './latest-actions'
import { setStatusbarItemGroup, useStatusbarContributions } from './panes'
import type { SidebarActions, WiringActions } from './types'

// Same lazy-view split as DesktopController — pages load on demand. The
// full-page views the workspace route table mounts live here; overlay views
// (agents/settings/…) are the controller's and stay in wiring.tsx.
const ArtifactsView = lazy(async () => ({ default: (await import('../artifacts')).ArtifactsView }))
const MessagingView = lazy(async () => ({ default: (await import('../messaging')).MessagingView }))
const MindView = lazy(async () => ({ default: (await import('../mind')).MindView }))
const SkillsView = lazy(async () => ({ default: (await import('../skills')).SkillsView }))

export function LegacySessionRedirect() {
  const { sessionId } = useParams()

  return <Navigate replace to={sessionId ? sessionRoute(sessionId) : NEW_CHAT_ROUTE} />
}

export const SidebarSurface = memo(function SidebarSurface({
  actions,
  currentView
}: {
  actions: SidebarActions
  currentView: ComponentProps<typeof ChatSidebar>['currentView']
}) {
  const latestActions = useMemo(() => latestSidebarActions(actions), [actions])

  return <ChatSidebar currentView={currentView} {...latestActions} />
})

export const TerminalSurface = memo(function TerminalSurface() {
  return (
    <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-terminal-surface-background)">
      <TerminalPaneChrome />
    </div>
  )
})

function VoicePipelineDots({ sttActive, ttsActive }: { sttActive: boolean; ttsActive: boolean }) {
  const dot = (active: boolean) => (
    <span
      aria-hidden="true"
      className={cn('size-1.5 rounded-full', active ? 'bg-emerald-400' : 'bg-muted-foreground/45')}
    />
  )

  return (
    <span className="inline-flex items-center gap-1">
      {dot(sttActive)}
      {dot(ttsActive)}
    </span>
  )
}

function useMarviVoiceStatusItems({
  sttEnabled,
  streamingSttEnabled,
  streamingSttProvider,
  sttProvider,
  ttsProvider,
  voiceBargeInEnabled,
  voiceSemanticTurnEnabled
}: {
  sttEnabled: boolean
  streamingSttEnabled: boolean
  streamingSttProvider: string
  sttProvider: string
  ttsProvider: string
  voiceBargeInEnabled: boolean
  voiceSemanticTurnEnabled: boolean
}): readonly StatusbarItem[] {
  const presenceEnabled = useStore($presenceEnabled)
  const playback = useStore($voicePlayback)
  const wakeStatus = useStore($wakeStatus)
  const warmup = useStore($voiceWarmup)
  const listening = wakeStatus === 'woken' || wakeStatus === 'listening'
  const transcribing = wakeStatus === 'transcribing'
  const warming = warmup.started && !warmup.done

  return useMemo(() => {
    const sttActive = sttEnabled && (streamingSttEnabled || listening || transcribing)
    const ttsActive = ttsProvider === 'pockettts' && playback.status !== 'idle'
    const streamingLabel = streamingSttEnabled ? streamingSttProvider || 'on' : 'off'
    const pipelineTitle = `STT ${sttProvider || 'off'} · streaming ${streamingLabel} · TTS ${ttsProvider || 'off'} · smart turn ${voiceSemanticTurnEnabled ? 'on' : 'off'} · barge-in ${voiceBargeInEnabled ? 'on' : 'off'}`
    const engines = [warmup.tts, warmup.stt, warmup.wake]
    const counted = engines.filter(status => status !== 'skipped')
    const ready = counted.filter(status => status === 'ready').length
    const failed = engines.filter(status => status === 'failed')

    const presence = {
      className: !presenceEnabled ? 'opacity-45' : listening || transcribing ? 'text-(--ui-text-accent)' : undefined,
      detail: !presenceEnabled
        ? 'Presence off'
        : listening
          ? 'Listening'
          : transcribing
            ? 'Finalizing speech'
            : undefined,
      id: 'voice-presence',
      label: 'Presence',
      onSelect: () => setPresenceEnabled(!presenceEnabled),
      title: presenceEnabled ? 'Voice presence is on; click to turn off' : 'Voice presence is off; click to turn on',
      variant: 'action' as const
    }

    const pipeline = warming
      ? {
          className: 'justify-center gap-1.5 px-2',
          icon: <VoicePipelineDots sttActive={sttActive} ttsActive={ttsActive} />,
          id: 'voice-pipeline',
          label: `Warming voice ${ready}/${counted.length || 3}`,
          title: `Warming voice models · TTS ${warmup.tts} · STT ${warmup.stt} · wake ${warmup.wake}`,
          variant: 'text' as const
        }
      : {
          className: 'w-8 justify-center px-0',
          icon: <VoicePipelineDots sttActive={sttActive} ttsActive={ttsActive} />,
          id: 'voice-pipeline',
          title: failed.length ? `${pipelineTitle} · warmup failed: ${failed.join(', ')}` : pipelineTitle,
          variant: 'text' as const
        }

    return [presence, pipeline]
  }, [
    listening,
    playback.status,
    presenceEnabled,
    sttEnabled,
    streamingSttEnabled,
    streamingSttProvider,
    sttProvider,
    transcribing,
    ttsProvider,
    voiceBargeInEnabled,
    voiceSemanticTurnEnabled,
    warmup,
    warming
  ])
}

/** Owns the statusbar's own data hooks (status snapshot poll, contributed
 *  items) so its 15s refresh — and any statusbar-only churn — re-renders the
 *  bar alone, never the chat/sidebar/terminal. */
export const StatusbarSurface = memo(function StatusbarSurface({
  actions,
  agentsOpen,
  chatOpen,
  commandCenterOpen,
  sttEnabled,
  streamingSttEnabled,
  streamingSttProvider,
  sttProvider,
  ttsProvider,
  voiceBargeInEnabled,
  voiceSemanticTurnEnabled
}: {
  actions: WiringActions
  agentsOpen: boolean
  chatOpen: boolean
  commandCenterOpen: boolean
  sttEnabled: boolean
  streamingSttEnabled: boolean
  streamingSttProvider: string
  sttProvider: string
  ttsProvider: string
  voiceBargeInEnabled: boolean
  voiceSemanticTurnEnabled: boolean
}) {
  const gatewayState = useStore($gatewayState)
  const freshDraftReady = useStore($freshDraftReady)

  const marviVoiceStatusItems = useMarviVoiceStatusItems({
    sttEnabled,
    streamingSttEnabled,
    streamingSttProvider,
    sttProvider,
    ttsProvider,
    voiceBargeInEnabled,
    voiceSemanticTurnEnabled
  })

  const { inferenceStatus, statusSnapshot } = useStatusSnapshot(gatewayState, actions.requestGateway)
  const extraLeftItems = useStatusbarContributions('left')
  const extraRightItems = useStatusbarContributions('right')

  const { leftStatusbarItems, statusbarItems } = useStatusbarItems({
    agentsOpen,
    chatOpen,
    commandCenterOpen,
    extraLeftItems: [...marviVoiceStatusItems, ...extraLeftItems],
    extraRightItems,
    freshDraftReady,
    gatewayState,
    inferenceStatus,
    openAgents: actions.openAgents,
    openCommandCenterSection: actions.openCommandCenterSection,
    requestGateway: actions.requestGateway,
    statusSnapshot,
    toggleCommandCenter: actions.toggleCommandCenter
  })

  return <StatusbarControls items={statusbarItems} leftItems={leftStatusbarItems} />
})

/** The workspace pane: the real route table (chat + full-page views + plugin
 *  routes). Subscribes to `$gatewayState` and ROUTES_AREA itself; the gateway
 *  instance + voice cap arrive as props so a reconnect/config load re-renders
 *  only this surface. ChatView subscribes to its own session atoms, so
 *  streaming never round-trips through the controller. */
export const ChatRoutesSurface = memo(function ChatRoutesSurface({
  actions,
  bargeInEnabled,
  maxVoiceRecordingSeconds,
  semanticTurnEnabled,
  streamingSttEnabled,
  wakeWordConfig
}: {
  actions: WiringActions
  bargeInEnabled: boolean
  maxVoiceRecordingSeconds?: number
  semanticTurnEnabled: boolean
  streamingSttEnabled: boolean
  wakeWordConfig: WakeWordConfig
}) {
  const activeGatewayProfile = useStore($activeGatewayProfile)
  const gatewayState = useStore($gatewayState)
  useContributions(ROUTES_AREA)
  const routeContributions = contributedRoutes()

  // Recapture the live gateway instance whenever the connection state flips.
  // getGateway reads a controller ref, so gatewayState is the intentional
  // re-eval trigger (not a value the computation itself reads).
  const gateway = useMemo(
    () => actions.getGateway(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actions, gatewayState]
  )

  const modelMenuContent = useMemo(
    () =>
      gatewayState === 'open' ? (
        <ModelMenuPanel
          gateway={gateway || undefined}
          onSelectModel={actions.selectModel}
          profile={activeGatewayProfile}
          requestGateway={actions.requestGateway}
        />
      ) : null,
    [actions, activeGatewayProfile, gateway, gatewayState]
  )

  const chatActions = useMemo(() => latestChatActions(actions), [actions])

  const chatView = (
    <ChatView
      bargeInEnabled={bargeInEnabled}
      gateway={gateway}
      maxVoiceRecordingSeconds={maxVoiceRecordingSeconds}
      modelMenuContent={modelMenuContent}
      semanticTurnEnabled={semanticTurnEnabled}
      streamingSttEnabled={streamingSttEnabled}
      wakeWordConfig={wakeWordConfig}
      {...chatActions}
    />
  )

  // FULL-PAGE views (not chat) mark the zone body `data-zone-no-header`: a
  // page is not a tab-able surface, so the zone's double-click header toggle
  // stands down while one is showing (see onZoneDoubleClick).
  const page = (view: ReactNode) => (
    <div className="contents" data-zone-no-header>
      <Suspense fallback={null}>{view}</Suspense>
    </div>
  )

  return (
    <Routes>
      <Route element={chatView} index />
      <Route element={chatView} path=":sessionId" />
      <Route element={page(<SkillsView setStatusbarItemGroup={setStatusbarItemGroup} />)} path="skills" />
      <Route element={page(<MessagingView setStatusbarItemGroup={setStatusbarItemGroup} />)} path="messaging" />
      <Route element={page(<MindView />)} path="mind" />
      <Route element={page(<ArtifactsView setStatusbarItemGroup={setStatusbarItemGroup} />)} path="artifacts" />
      {/* Route overlays paint above this zone in wiring.tsx. Keep the same
          chat tree alive underneath so opening Settings cannot tear down an
          active duplex mic/socket session. */}
      {OVERLAY_ROUTES.map(route => (
        <Route element={chatView} key={route.id} path={route.path.slice(1)} />
      ))}
      {/* Registry-contributed pages (core features + plugins) render in the
          workspace pane like any built-in view — behind the same blast wall
          as every other contribution mount. */}
      {routeContributions.map(route => (
        <Route
          element={page(
            <ContribBoundary id={route.key}>
              <ContribRender render={route.render} />
            </ContribBoundary>
          )}
          key={route.key}
          path={route.path.slice(1)}
        />
      ))}
      <Route element={<Navigate replace to={NEW_CHAT_ROUTE} />} path="new" />
      <Route element={<LegacySessionRedirect />} path="sessions/:sessionId" />
      <Route element={<Navigate replace to={NEW_CHAT_ROUTE} />} path="*" />
    </Routes>
  )
})
