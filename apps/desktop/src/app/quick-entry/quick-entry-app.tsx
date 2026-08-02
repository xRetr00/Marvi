import { useEffect, useReducer, useRef } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { iconSize, MessageSquareText, Send } from '@/lib/icons'
import {
  initialQuickComposerState,
  QUICK_TARGET_CURRENT,
  QUICK_TARGET_NEW,
  type QuickComposerEvent,
  quickComposerReducer,
  type QuickComposerState
} from '@/store/quick-entry'

/** Small capture surface for sending a prompt through the primary renderer. */
export function QuickEntryApp() {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const { t } = useI18n()

  const [state, dispatch] = useReducer((current: QuickComposerState, event: QuickComposerEvent) => {
    const { send, state: next } = quickComposerReducer(current, event)
    const api = window.hermesDesktop?.quickEntry

    if (send) {
      api?.submit(send)
    } else if (!next.visible && current.visible) {
      api?.dismiss()
    }

    return next
  }, initialQuickComposerState)

  useEffect(() => {
    const api = window.hermesDesktop?.quickEntry

    const offShown = api?.onShown(() => {
      dispatch({ type: 'shown' })
      requestAnimationFrame(() => inputRef.current?.focus())
    })

    const offState = api?.onState(payload => {
      dispatch({
        connected: payload?.connected === true,
        sessions: Array.isArray(payload?.sessions) ? payload.sessions : [],
        type: 'state'
      })
    })

    inputRef.current?.focus()

    return () => {
      offShown?.()
      offState?.()
    }
  }, [])

  const submit = () => dispatch({ type: 'submit' })
  const canSubmit = state.connected && state.draft.trim().length > 0 && !state.submitting

  return (
    <main className="flex h-screen w-screen items-center justify-center bg-transparent p-3">
      <form
        className="flex w-full flex-col gap-2.5 rounded-xl border border-(--stroke-nous) bg-(--ui-chat-bubble-background) px-3.5 py-3 shadow-nous"
        onSubmit={event => {
          event.preventDefault()
          submit()
        }}
      >
        <header className="flex items-center gap-2 text-xs font-medium text-(--ui-text-secondary)">
          <MessageSquareText aria-hidden className={iconSize.sm} />
          <span>{t.settings.quickEntry.enabledTitle}</span>
          <span
            aria-hidden
            className={`ml-auto size-1.5 rounded-full ${state.connected ? 'bg-primary' : 'bg-(--ui-text-quaternary)'}`}
          />
        </header>

        <textarea
          aria-label={t.settings.quickEntry.enabledTitle}
          autoCapitalize="off"
          autoComplete="off"
          autoCorrect="off"
          className="min-h-14 w-full resize-none bg-transparent text-[15px] leading-5 text-(--ui-text-primary) outline-none placeholder:text-(--ui-text-tertiary) disabled:cursor-not-allowed disabled:opacity-55"
          disabled={!state.connected}
          onBlur={event => {
            if (!event.relatedTarget) {
              dispatch({ type: 'blur' })
            }
          }}
          onChange={event => dispatch({ draft: event.target.value, type: 'edit' })}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            } else if (event.key === 'Escape') {
              event.preventDefault()
              dispatch({ type: 'dismiss' })
            }
          }}
          placeholder={state.connected ? t.settings.quickEntry.placeholder : t.settings.quickEntry.disconnected}
          ref={inputRef}
          rows={2}
          spellCheck={false}
          value={state.draft}
        />

        <footer className="flex items-center gap-2 border-t border-(--ui-stroke-tertiary) pt-2.5">
          <label className="sr-only" htmlFor="quick-entry-target">
            {t.settings.quickEntry.sendTo}
          </label>
          <select
            className="min-w-0 max-w-72 cursor-pointer appearance-none bg-transparent pr-4 text-xs text-(--ui-text-secondary) outline-none disabled:cursor-not-allowed disabled:opacity-50"
            disabled={!state.connected}
            id="quick-entry-target"
            onChange={event => dispatch({ target: event.target.value, type: 'target' })}
            onKeyDown={event => {
              if (event.key === 'Escape') {
                event.preventDefault()
                dispatch({ type: 'dismiss' })
              }
            }}
            value={state.target}
          >
            <option value={QUICK_TARGET_CURRENT}>{t.settings.quickEntry.currentChat}</option>
            <option value={QUICK_TARGET_NEW}>{t.settings.quickEntry.newSession}</option>
            {state.sessions.map(session => (
              <option key={session.id} value={session.id}>
                {session.title}
              </option>
            ))}
          </select>

          <div className="ml-auto hidden items-center gap-1.5 text-[10px] text-(--ui-text-quaternary) sm:flex">
            <kbd className="font-[inherit]">Enter</kbd>
            <span>{t.common.send}</span>
            <span aria-hidden>·</span>
            <kbd className="font-[inherit]">Esc</kbd>
            <span>{t.common.close}</span>
          </div>

          <Button aria-label={t.common.send} disabled={!canSubmit} size="icon-xs" type="submit">
            <Send aria-hidden />
          </Button>
        </footer>
      </form>
    </main>
  )
}
