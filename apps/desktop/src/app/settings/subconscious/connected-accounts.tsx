import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Clipboard } from '@/lib/icons'
import { notify } from '@/store/notifications'

import { ListRow } from '../primitives'

import { StringListEditor } from './string-list-editor'
import type { useMarviConfig } from './use-marvi-config'

const SUGGESTED_SURFACES = ['gmail', 'github', 'calendar', 'slack']

async function copyConnectCommand(app: string) {
  const command = `hermes composio connect ${app}`

  if (window.hermesDesktop?.writeClipboard) {
    await window.hermesDesktop.writeClipboard(command)
    notify({ kind: 'success', message: 'Copied to clipboard', placement: 'bottom-right' })
  }
}

/** Composio surfaces (config `composio.*`) + connect hints — no live OAuth status endpoint exists yet, so this shows what's configured and how to finish connecting from a terminal. */
export function ConnectedAccounts({ marvi }: { marvi: ReturnType<typeof useMarviConfig> }) {
  const surfaces = marvi.get<string[]>('composio.surfaces', [])
  const apiKeySet = Boolean(marvi.get<string>('composio.api_key', ''))
  const saving = marvi.savingPath?.startsWith('composio') ?? false

  return (
    <div className="grid gap-3">
      <ListRow
        action={
          <Input
            className="max-w-64"
            onBlur={e => {
              // Only write on a real edit — an empty blur (tab-through, no
              // typing) must never silently clear an already-set key.
              if (e.target.value.trim()) {
                void marvi.patch('composio.api_key', e.target.value.trim())
              }
            }}
            placeholder={apiKeySet ? '••••••••' : 'Paste Composio API key'}
            type="password"
          />
        }
        description="Required once — get one at app.composio.dev."
        title="Composio API key"
      />

      <div className="grid gap-1.5">
        <div className="text-[length:var(--conversation-text-font-size)] font-medium text-foreground">Surfaces</div>
        <p className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          Add a surface, then run its connect command in a terminal to finish authenticating.
        </p>

        <StringListEditor
          disabled={saving}
          emptyLabel="No surfaces configured yet."
          onChange={next => void marvi.patch('composio.surfaces', next)}
          placeholder="e.g. gmail"
          values={surfaces}
        />

        {surfaces.length > 0 && (
          <div className="mt-1 grid gap-1">
            {surfaces.map(app => (
              <div className="flex items-center justify-between gap-2 rounded-md bg-(--ui-bg-quinary) px-2.5 py-1.5" key={app}>
                <code className="text-[0.7rem] text-muted-foreground">hermes composio connect {app}</code>
                <Button onClick={() => void copyConnectCommand(app)} size="icon-xs" title="Copy command" variant="ghost">
                  <Clipboard className="size-3" />
                </Button>
              </div>
            ))}
          </div>
        )}

        {SUGGESTED_SURFACES.some(s => !surfaces.includes(s)) && (
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            <span className="text-[0.65rem] text-muted-foreground">Popular:</span>
            {SUGGESTED_SURFACES.filter(s => !surfaces.includes(s)).map(app => (
              <button
                className="rounded-[3px] bg-muted px-1.5 py-0.5 text-[0.65rem] text-muted-foreground transition hover:bg-(--ui-bg-tertiary) hover:text-foreground"
                key={app}
                onClick={() => void marvi.patch('composio.surfaces', [...surfaces, app])}
                type="button"
              >
                {app}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
