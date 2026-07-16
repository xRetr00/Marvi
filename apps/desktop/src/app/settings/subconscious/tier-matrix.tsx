import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { triggerHaptic } from '@/lib/haptics'
import { Plus, Trash2 } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { CONTROL_TEXT } from '../constants'

import type { SubconsciousTier, TierMap } from './types'

const TIER_OPTIONS: { value: SubconsciousTier; label: string; hint: string }[] = [
  { value: 'notify', label: 'Notify', hint: 'Marvi tells you, no action taken' },
  { value: 'propose', label: 'Propose', hint: 'One-tap suggestion you approve' },
  { value: 'auto', label: 'Auto', hint: 'Pre-approved — Marvi just does it' }
]

// Suggested starting categories so the matrix isn't empty on first open. Purely
// a UI convenience — nothing is written to config until a row's tier is
// actually changed, or the row is explicitly added.
const SUGGESTED_CATEGORIES = ['goals', 'overnight_diff', 'calendar', 'mail', 'presence']

/** Editor for `subconscious.tiers`: category -> notify | propose | auto. */
export function TierMatrix({
  tiers,
  onChange,
  disabled,
  learned = []
}: {
  tiers: TierMap
  onChange: (next: TierMap) => void
  disabled?: boolean
  learned?: string[]
}) {
  const [newCategory, setNewCategory] = useState('')
  const savedCategories = Object.keys(tiers).sort((a, b) => a.localeCompare(b))
  const suggestions = SUGGESTED_CATEGORIES.filter(c => !tiers[c])

  function setTier(category: string, tier: SubconsciousTier) {
    onChange({ ...tiers, [category]: tier })
  }

  function removeCategory(category: string) {
    const next = { ...tiers }
    delete next[category]
    onChange(next)
  }

  function addCategory(category: string) {
    const trimmed = category.trim().toLowerCase().replace(/\s+/g, '_')

    if (!trimmed || tiers[trimmed]) {
      return
    }

    onChange({ ...tiers, [trimmed]: 'propose' })
    setNewCategory('')
    triggerHaptic('selection')
  }

  return (
    <div className="rounded-md border border-(--ui-stroke-secondary)">
      {savedCategories.length === 0 ? (
        <div className="px-3 py-4 text-center text-xs text-muted-foreground">
          No categories configured yet — Marvi defaults new suggestions to <span className="font-mono">propose</span>.
        </div>
      ) : (
        <div className="divide-y divide-(--ui-stroke-secondary)">
          {savedCategories.map(category => (
            <div className="flex items-center justify-between gap-2 px-3 py-2" key={category}>
              <span className="flex min-w-0 items-center gap-1.5 truncate font-mono text-xs text-foreground">
                {category}
                {learned.includes(category) && (
                  <span className="rounded-full bg-primary/10 px-1.5 py-0.5 font-sans text-[0.6rem] text-primary">learned</span>
                )}
              </span>
              <div className="flex shrink-0 items-center gap-1">
                <Select
                  disabled={disabled}
                  onValueChange={value => setTier(category, value as SubconsciousTier)}
                  value={tiers[category]}
                >
                  <SelectTrigger className={cn('h-7 min-w-32', CONTROL_TEXT)}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TIER_OPTIONS.map(option => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  aria-label={`Remove ${category}`}
                  disabled={disabled}
                  onClick={() => removeCategory(category)}
                  size="icon-xs"
                  type="button"
                  variant="ghost"
                >
                  <Trash2 />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-(--ui-stroke-secondary) px-3 py-2">
        <Input
          className={cn('h-7 max-w-40', CONTROL_TEXT)}
          onChange={e => setNewCategory(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault()
              addCategory(newCategory)
            }
          }}
          placeholder="Category name"
          value={newCategory}
        />
        <Button
          className="h-7 gap-1"
          disabled={disabled || !newCategory.trim()}
          onClick={() => addCategory(newCategory)}
          size="sm"
          type="button"
          variant="outline"
        >
          <Plus className="size-3" />
          Add
        </Button>

        {suggestions.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[0.65rem] text-muted-foreground">Suggested:</span>
            {suggestions.map(category => (
              <button
                className="rounded-[3px] bg-muted px-1.5 py-0.5 text-[0.65rem] text-muted-foreground transition hover:bg-(--ui-bg-tertiary) hover:text-foreground"
                disabled={disabled}
                key={category}
                onClick={() => addCategory(category)}
                type="button"
              >
                {category}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export { TIER_OPTIONS }
