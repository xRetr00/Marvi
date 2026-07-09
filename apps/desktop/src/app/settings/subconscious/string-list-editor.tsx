import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { triggerHaptic } from '@/lib/haptics'
import { Plus, Trash2 } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { CONTROL_TEXT } from '../constants'

/** Small add/remove editor for a list of free-text strings (e.g. `presence.denylist`). */
export function StringListEditor({
  values,
  onChange,
  placeholder,
  emptyLabel,
  disabled
}: {
  values: string[]
  onChange: (next: string[]) => void
  placeholder?: string
  emptyLabel?: string
  disabled?: boolean
}) {
  const [draft, setDraft] = useState('')

  function add(value: string) {
    const trimmed = value.trim()

    if (!trimmed || values.includes(trimmed)) {
      return
    }

    onChange([...values, trimmed])
    setDraft('')
    triggerHaptic('selection')
  }

  function remove(value: string) {
    onChange(values.filter(v => v !== value))
  }

  return (
    <div className="grid gap-2">
      {values.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {values.map(value => (
            <li
              className="flex items-center gap-1 rounded-[3px] bg-muted px-1.5 py-0.5 text-[0.7rem] text-foreground"
              key={value}
            >
              <span className="max-w-56 truncate">{value}</span>
              <button
                aria-label={`Remove ${value}`}
                className="text-muted-foreground hover:text-destructive"
                disabled={disabled}
                onClick={() => remove(value)}
                type="button"
              >
                <Trash2 className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      ) : emptyLabel ? (
        <p className="text-[0.7rem] text-muted-foreground">{emptyLabel}</p>
      ) : null}

      <div className="flex items-center gap-1.5">
        <Input
          className={cn('h-7 max-w-64', CONTROL_TEXT)}
          disabled={disabled}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add(draft)
            }
          }}
          placeholder={placeholder}
          value={draft}
        />
        <Button
          className="h-7 gap-1"
          disabled={disabled || !draft.trim()}
          onClick={() => add(draft)}
          size="sm"
          type="button"
          variant="outline"
        >
          <Plus className="size-3" />
          Add
        </Button>
      </div>
    </div>
  )
}
