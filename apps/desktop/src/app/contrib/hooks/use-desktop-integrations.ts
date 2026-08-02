import { useEffect, useRef } from 'react'

import { closeActiveTab } from '@/app/chat/close-tab'
import { getSmartRoomClapDataset, reviewSmartRoomClap } from '@/hermes'
import { openSession } from '@/app/open-session'
import { storedSessionIdForNotification } from '@/lib/session-ids'
import { respondToApprovalAction } from '@/store/native-notifications'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { openFolderAsProject } from '@/store/projects'
import {
  $sessions,
  getRememberedRoute,
  getRememberedSessionId,
  rememberedSessionProfile,
  setRememberedRoute,
  setRememberedSessionId
} from '@/store/session'
import { onSessionsChanged } from '@/store/session-sync'
import { openUpdatesWindow, startUpdatePoller, stopUpdatePoller } from '@/store/updates'
import { startProactiveDeliveryPolling, stopProactiveDeliveryPolling } from '@/store/proactive-delivery'
import { initVoiceIslandBridge } from '@/store/voice-island'
import { startVoiceWarmupPolling } from '@/store/voice-warmup'
import { initWindowPresence } from '@/store/window-presence'
import { isSecondaryWindow } from '@/store/windows'

import { requestComposerFocus, requestComposerInsert } from '../../chat/composer/focus'
import { appViewForPath, isOverlayView, NEW_CHAT_ROUTE, sessionRoute } from '../../routes'

interface DesktopIntegrationsParams {
  chatOpen: boolean
  hasPreview: boolean
  locationPathname: string
  navigate: (to: string, options?: { replace?: boolean }) => void
  refreshSessions: () => Promise<unknown> | unknown
  resumeExhaustedSessionId: null | string
  routedSessionId: null | string
  runtimeIdByStoredSessionId: { readonly current: Map<string, string> }
}

/**
 * All the Electron-main / OS / cross-window integrations the shell listens for:
 * update polling, the ⌘W close shortcut, deep links, native-notification
 * navigation, preview-shortcut enablement, remembered-session restore, and
 * cross-window session-list sync. Kept out of the wiring controller so the
 * "talks to the desktop shell" surface reads as one unit.
 */
export function useDesktopIntegrations({
  locationPathname,
  navigate,
  refreshSessions,
  resumeExhaustedSessionId,
  routedSessionId,
  runtimeIdByStoredSessionId
}: DesktopIntegrationsParams): void {
  // Update polling — populates $desktopVersion/$updateStatus, which feed the
  // statusbar version pill and the update toasts. Also honors the main
  // process's "open updates" menu request.
  useEffect(() => {
    startUpdatePoller()
    startProactiveDeliveryPolling()
    startVoiceWarmupPolling()
    initWindowPresence()
    const stopVoiceIslandBridge = isSecondaryWindow() ? undefined : initVoiceIslandBridge()
    const unsubscribe = window.hermesDesktop?.onOpenUpdatesRequested?.(() => openUpdatesWindow())

    return () => {
      unsubscribe?.()
      stopVoiceIslandBridge?.()
      stopProactiveDeliveryPolling()
      stopUpdatePoller()
    }
  }, [])

  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    let stopped = false
    const shown = new Set<string>()

    const poll = async () => {
      try {
        const { dataset } = await getSmartRoomClapDataset()
        const sample = dataset.next_pending

        if (stopped || !sample || shown.has(sample.id)) {
          return
        }

        shown.add(sample.id)
        const captured = new Date(sample.captured_at)
        const time = captured.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        const today = new Date().toDateString() === captured.toDateString()
        const when = today ? `${time} today` : captured.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
        const answer = (confirmed: boolean) => {
          void reviewSmartRoomClap(sample.id, confirmed)
            .then(() => window.setTimeout(() => void poll(), 250))
            .catch(error => notifyError(error, 'Could not save clap feedback'))
        }

        notify({
          id: `smart-room-clap-${sample.id}`,
          kind: 'info',
          title: 'Clap detected',
          message: `Did you clap at ${when}?`,
          detail: `${dataset.confirmed} of ${dataset.target} confirmed claps collected locally.`,
          durationMs: 0,
          placement: 'bottom-right',
          actions: [
            { label: 'Yes', onClick: () => answer(true) },
            { label: 'No', onClick: () => answer(false) }
          ]
        })
      } catch {
        // Smart Room may be disabled or still starting; the next poll retries.
      }
    }

    void poll()
    const timer = window.setInterval(() => void poll(), 8_000)

    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [])

  // The renderer OWNS ⌘W: on macOS the native menu accelerator would else
  // close the window, so claim it unconditionally — the menu then routes ⌘W
  // to us (close-preview-requested IPC) and we decide tab-vs-window.
  useEffect(() => {
    window.hermesDesktop?.setPreviewShortcutActive?.(true)
  }, [])

  // Remember the open chat (session id for notifications/resume) AND the last
  // non-overlay route (a page like /skills, or a session route) so a relaunch
  // lands where you were. Overlays (settings/command-center/…) aren't stored —
  // you don't want to boot into a modal.
  useEffect(() => {
    const routeProfile = rememberedSessionProfile($sessions.get(), routedSessionId, $activeGatewayProfile.get())

    if (routedSessionId) {
      setRememberedSessionId(routedSessionId, routeProfile)
    }

    if (!isOverlayView(appViewForPath(locationPathname))) {
      // Keyed by the same owner as the id above: a session route embeds a
      // session id, so remembering it globally would restore another profile's
      // conversation on cold start.
      setRememberedRoute(locationPathname, routeProfile)
    }
  }, [locationPathname, routedSessionId])

  const restoredRef = useRef(false)

  // Restore once on cold start — only when the renderer booted at the default
  // route (a hidden-then-shown window keeps its own route). Prefer the full
  // remembered route (covers pages); fall back to the last session id.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (restoredRef.current || locationPathname !== NEW_CHAT_ROUTE) {
      restoredRef.current = true

      return
    }

    restoredRef.current = true
    const activeProfile = $activeGatewayProfile.get()
    const route = getRememberedRoute(activeProfile)

    if (route && route !== NEW_CHAT_ROUTE && !isOverlayView(appViewForPath(route))) {
      navigate(route, { replace: true })

      return
    }

    const last = getRememberedSessionId(activeProfile)

    if (last) {
      navigate(sessionRoute(last), { replace: true })
    }
  }, [locationPathname, navigate])

  useEffect(() => {
    if (!resumeExhaustedSessionId) {
      return
    }

    const owner = rememberedSessionProfile($sessions.get(), resumeExhaustedSessionId, $activeGatewayProfile.get())

    if (getRememberedSessionId(owner) === resumeExhaustedSessionId) {
      setRememberedSessionId(null, owner)
    }
  }, [resumeExhaustedSessionId])

  // Native-notification click -> jump to the session WHERE IT ALREADY IS (open
  // tile / main), else beside what's loaded rather than over it — the click
  // came from outside the app and shouldn't cost the user the chat they left
  // on screen. Runtime id is translated to the stored id the chat route is
  // keyed by; action buttons resolve in place.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onFocusSession?.(sessionId => {
      if (sessionId) {
        openSession(storedSessionIdForNotification(sessionId, runtimeIdByStoredSessionId.current), navigate, 'stack')
      }
    })

    return () => unsubscribe?.()
  }, [navigate, runtimeIdByStoredSessionId])

  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onNotificationAction?.(({ actionId, sessionId }) => {
      void respondToApprovalAction(sessionId ?? null, actionId)
    })

    return () => unsubscribe?.()
  }, [])

  // hermes:// deep links -> a reviewable /blueprint command in the composer.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onDeepLink?.(payload => {
      if (!payload || payload.kind !== 'blueprint' || !payload.name) {
        return
      }

      const slots = Object.entries(payload.params || {})
        .map(([k, v]) => {
          const sval = /\s/.test(v) ? `"${v.replace(/"/g, '\\"')}"` : v

          return `${k}=${sval}`
        })
        .join(' ')

      const command = `/blueprint ${payload.name}${slots ? ' ' + slots : ''}`
      requestComposerInsert(command, { mode: 'block', target: 'main' })
      requestComposerFocus('main')
    })

    void window.hermesDesktop?.signalDeepLinkReady?.()

    return () => unsubscribe?.()
  }, [])

  // ⌘W via the macOS menu accelerator → close the focused tab; if nothing is
  // closeable, fall back to closing the window (so ⌘W still works as the
  // OS-standard window close, esp. secondary windows). The Win/Linux keyboard
  // path is the `view.closeTab` keybind (use-keybinds), sharing closeActiveTab.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onClosePreviewRequested?.(
      () => void closeActiveTab(id => navigate(sessionRoute(id)))
    )

    return () => unsubscribe?.()
  }, [navigate])

  // File > Open Folder… — same open-folder-as-project upsert as the ⌘O keybind.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onOpenFolderRequested?.(() => void openFolderAsProject())

    return () => unsubscribe?.()
  }, [])

  // Another window mutated the shared session list -> re-pull the sidebar.
  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    return onSessionsChanged(() => void refreshSessions())
  }, [refreshSessions])
}
