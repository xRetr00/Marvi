import { useRef, useState } from 'react'

import { saveHermesConfig } from '@/hermes'
import { notifyError } from '@/store/notifications'
import type { HermesConfigRecord } from '@/types/hermes'

import { setHermesConfigCache, useHermesConfigRecord } from '../../hooks/use-config-record'
import { getNested, setNested } from '../helpers'

/**
 * Reuses the app's existing shared config channel (`GET`/`PUT /api/config`,
 * the same `useHermesConfigRecord`/`setHermesConfigCache` cache that
 * `ConfigSettings` autosaves through) to read and write the Contract-3 keys
 * (`subconscious.*`, `presence.*`, `composio.*`) that don't yet have a
 * declared backend schema entry. The web server's `PUT /api/config` handler
 * deep-merges the incoming record over what's on disk, so writing keys the
 * schema doesn't know about is safe and forward-compatible with whatever
 * Workstreams A/B/C land.
 *
 * Unlike `ConfigSettings`'s debounced-autosave draft, every `patch` here
 * saves immediately (optimistic update + rollback on failure) — the fields
 * this hook backs are toggles/selects/small lists, not free-typed text.
 */
export function useMarviConfig() {
  const { data, isError, isLoading, refetch } = useHermesConfigRecord()
  const [savingPath, setSavingPath] = useState<null | string>(null)
  // Guards against a slow save clobbering a newer one that already landed.
  const versionRef = useRef(0)

  async function patch(path: string, value: unknown): Promise<void> {
    if (!data) {
      return
    }

    const previous = data
    const next = setNested(data, path, value)
    const version = ++versionRef.current

    setHermesConfigCache(next)
    setSavingPath(path)

    try {
      await saveHermesConfig(next)
    } catch (err) {
      if (versionRef.current === version) {
        setHermesConfigCache(previous)
      }

      notifyError(err, 'Failed to save Marvi setting')
    } finally {
      if (versionRef.current === version) {
        setSavingPath(null)
      }
    }
  }

  function get<T>(path: string, fallback: T): T {
    if (!data) {
      return fallback
    }

    const value = getNested(data, path)

    return value === undefined ? fallback : (value as T)
  }

  return { config: data as HermesConfigRecord | undefined, get, isError, isLoading, patch, refetch, savingPath }
}
