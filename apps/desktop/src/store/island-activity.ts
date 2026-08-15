import { atom } from 'nanostores'

// Short, human label for the agent's current tool action — shown on the island
// so Marvi narrates what it's doing across the system.
export const $islandActivity = atom<string | null>(null)

const TOOL_LABELS: Record<string, string> = {
  terminal: 'Running a command',
  process: 'Managing a process',
  read_file: 'Reading a file',
  write_file: 'Writing a file',
  patch: 'Editing a file',
  search_files: 'Searching files',
  web_search: 'Searching the web',
  web_extract: 'Reading a page',
  vision_analyze: 'Looking at an image',
  image_generate: 'Generating an image',
  video_generate: 'Generating a video',
  delegate_task: 'Delegating a task',
  show_card: 'Showing a card',
  skill_view: 'Reading a skill',
  computer_use: 'Using the computer',
  cronjob: 'Scheduling a job'
}

export function activityLabelForTool(name: string | null | undefined): string {
  if (!name) {
    return 'Working'
  }

  return TOOL_LABELS[name] ?? 'Working'
}

export function setIslandActivity(label: string | null): void {
  $islandActivity.set(label)
}
