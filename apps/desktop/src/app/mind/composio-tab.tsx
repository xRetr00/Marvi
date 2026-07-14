import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { Link as LinkIcon, Search } from '@/lib/icons'
import { $gateway } from '@/store/gateway'
import { runIslandCardAction } from '@/store/island-cards'
import { notify, notifyError } from '@/store/notifications'

import { NEW_CHAT_ROUTE } from '../routes'
import { Pill, SectionHeading } from '../settings/primitives'

interface ComposioStatus {
  sdk_configured: boolean
  mcp_configured: boolean
  mcp_enabled: boolean
  snapshot_surfaces: string[]
}

interface Toolkit {
  slug: string
  name: string
  description: string
  categories: string[]
}

interface ToolkitResponse {
  toolkits: Toolkit[]
  total?: null | number
}

const SNAPSHOT_SURFACES = ['gmail', 'github', 'calendar', 'slack'] as const

export function ComposioTab() {
  const navigate = useNavigate()
  const { t } = useI18n()
  const copy = t.mind.composio
  const [status, setStatus] = useState<ComposioStatus | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [consumerApiKey, setConsumerApiKey] = useState('')
  const [query, setQuery] = useState('')
  const [toolkits, setToolkits] = useState<Toolkit[]>([])
  const [total, setTotal] = useState<null | number>(null)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await window.hermesDesktop.api<ComposioStatus>({ path: '/api/composio/status' }))
    } catch (error) {
      notifyError(error, copy.loadFailed)
    }
  }, [copy.loadFailed])

  const loadToolkits = useCallback(
    async (search = '') => {
      setLoading(true)

      try {
        const result = await window.hermesDesktop.api<ToolkitResponse>({
          path: `/api/composio/toolkits?limit=100&search=${encodeURIComponent(search)}`
        })

        setToolkits(result.toolkits ?? [])
        setTotal(typeof result.total === 'number' ? result.total : null)
      } catch (error) {
        setToolkits([])
        notifyError(error, copy.catalogFailed)
      } finally {
        setLoading(false)
      }
    },
    [copy.catalogFailed]
  )

  useEffect(() => {
    void loadStatus()
  }, [loadStatus])

  useEffect(() => {
    if (!status?.sdk_configured) {
      return
    }

    const handle = window.setTimeout(() => void loadToolkits(query.trim()), 300)

    return () => window.clearTimeout(handle)
  }, [loadToolkits, query, status?.sdk_configured])

  async function saveKey() {
    const key = apiKey.trim()
    const consumerKey = consumerApiKey.trim()

    if (!key && !consumerKey) {
      return
    }

    setSaving(true)

    try {
      const next = await window.hermesDesktop.api<ComposioStatus>({
        path: '/api/composio/setup',
        method: 'POST',
        body: { api_key: key, consumer_api_key: consumerKey }
      })

      setStatus(next)
      setApiKey('')
      setConsumerApiKey('')
      const gateway = $gateway.get()

      if (gateway) {
        // Refresh global discovery without attaching it to an existing chat:
        // new sessions get Composio immediately, while no live conversation's
        // byte-stable tool prefix is invalidated behind the user's back.
        try {
          await gateway.request('reload.env', {})
          await gateway.request('reload.mcp', { confirm: true })
        } catch {
          // Setup is already durable; gateway reconnect/new process discovery
          // will pick it up if this best-effort live refresh is unavailable.
        }
      }

      notify({ kind: 'success', message: copy.saved })

      if (next.sdk_configured) {
        await loadToolkits()
      }
    } catch (error) {
      notifyError(error, copy.saveFailed)
    } finally {
      setSaving(false)
    }
  }

  async function toggleSnapshot(surface: string) {
    const current = status?.snapshot_surfaces ?? []
    const surfaces = current.includes(surface) ? current.filter(item => item !== surface) : [...current, surface]

    try {
      setStatus(
        await window.hermesDesktop.api<ComposioStatus>({
          path: '/api/composio/snapshots',
          method: 'PUT',
          body: { surfaces }
        })
      )
      notify({ kind: 'success', message: copy.snapshotsSaved })
    } catch (error) {
      notifyError(error, copy.snapshotsFailed)
    }
  }

  function connect(toolkit: Pick<Toolkit, 'name' | 'slug'>) {
    navigate(NEW_CHAT_ROUTE)
    window.setTimeout(() => {
      runIslandCardAction(
        `Use Composio to connect my ${toolkit.name} (${toolkit.slug}) account. Start the authorization flow and show me the link.`
      )
    }, 0)
  }

  return (
    <div className="grid gap-7">
      <section>
        <SectionHeading
          icon={LinkIcon}
          meta={status?.sdk_configured && status?.mcp_configured ? copy.keyConfigured : copy.keyMissing}
          title={copy.title}
        />
        <p className="mb-3 text-xs text-muted-foreground">{copy.description}</p>
        <div className="rounded-xl border border-(--ui-stroke-secondary) p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-medium">
              {copy.keyTitle}
              <Input
                className="mt-1.5"
                onChange={event => setApiKey(event.target.value)}
                placeholder={status?.sdk_configured ? '••••••••' : copy.keyPlaceholder}
                type="password"
                value={apiKey}
              />
            </label>
            <label className="text-xs font-medium">
              {copy.consumerKeyTitle}
              <Input
                className="mt-1.5"
                onChange={event => setConsumerApiKey(event.target.value)}
                placeholder={status?.mcp_configured ? '••••••••' : copy.consumerKeyPlaceholder}
                type="password"
                value={consumerApiKey}
              />
            </label>
          </div>
          <div className="mt-2 grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
            <p>{copy.keyDescription}</p>
            <p>{copy.consumerKeyDescription}</p>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button disabled={saving || (!apiKey.trim() && !consumerApiKey.trim())} onClick={() => void saveKey()}>
              {saving ? copy.saving : copy.saveKey}
            </Button>
            <Pill tone={status?.sdk_configured ? 'primary' : 'muted'}>
              SDK: {status?.sdk_configured ? copy.keyConfigured : copy.keyMissing}
            </Pill>
            <Pill tone={status?.mcp_enabled ? 'primary' : 'muted'}>
              MCP: {status?.mcp_enabled ? copy.mcpReady : copy.mcpMissing}
            </Pill>
          </div>
          <div className="mt-4 border-t border-(--ui-stroke-secondary) pt-3">
            <div className="text-xs font-medium">{copy.snapshotTitle}</div>
            <p className="mt-1 text-xs text-muted-foreground">{copy.snapshotDescription}</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {SNAPSHOT_SURFACES.map(surface => (
                <Button
                  disabled={!status?.sdk_configured}
                  key={surface}
                  onClick={() => void toggleSnapshot(surface)}
                  size="sm"
                  variant={status?.snapshot_surfaces.includes(surface) ? 'default' : 'outline'}
                >
                  {surface}
                </Button>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section>
        <SectionHeading
          icon={Search}
          meta={total === null ? copy.allApps : copy.totalApps(total)}
          title={copy.catalogTitle}
        />
        <p className="mb-3 text-xs text-muted-foreground">{copy.catalogDescription}</p>
        <Input
          disabled={!status?.sdk_configured}
          onChange={event => setQuery(event.target.value)}
          placeholder={copy.searchPlaceholder}
          value={query}
        />
        <div className="mt-3 max-h-[28rem] overflow-y-auto rounded-lg border border-(--ui-stroke-secondary)">
          {loading ? (
            <div className="p-6 text-center text-xs text-muted-foreground">{copy.loading}</div>
          ) : toolkits.length === 0 ? (
            <div className="p-6 text-center text-xs text-muted-foreground">{copy.empty}</div>
          ) : (
            <div className="divide-y divide-(--ui-stroke-secondary)">
              {toolkits.map(toolkit => (
                <div className="p-3" key={toolkit.slug}>
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-sm font-medium">{toolkit.name}</div>
                    <div className="flex items-center gap-2">
                      <code className="text-[0.65rem] text-muted-foreground">{toolkit.slug}</code>
                      <Button
                        disabled={!status?.mcp_configured}
                        onClick={() => connect(toolkit)}
                        size="sm"
                        variant="outline"
                      >
                        {copy.connect}
                      </Button>
                    </div>
                  </div>
                  {toolkit.description && (
                    <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{toolkit.description}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="rounded-xl border border-primary/20 bg-primary/5 p-4">
        <div className="text-sm font-medium">{copy.askTitle}</div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{copy.askDescription}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          {SNAPSHOT_SURFACES.map(surface => (
            <Button
              disabled={!status?.mcp_configured}
              key={surface}
              onClick={() => connect({ name: surface, slug: surface })}
              size="sm"
              variant="outline"
            >
              {copy.connect} {surface}
            </Button>
          ))}
        </div>
      </section>
    </div>
  )
}
