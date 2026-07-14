import { describe, expect, it } from 'vitest'

import { DuplexSessionMachine, INITIAL_DUPLEX_STATE } from './duplex-session'

describe('DuplexSessionMachine', () => {
  it('starts connecting', () => {
    const machine = new DuplexSessionMachine()
    expect(machine.state).toEqual(INITIAL_DUPLEX_STATE)
  })

  it('moves to listening on ready', () => {
    const machine = new DuplexSessionMachine()
    const commands = machine.applyEvent({ type: 'ready' })

    expect(commands).toEqual([])
    expect(machine.state.phase).toBe('listening')
  })

  it('tracks the live partial caption', () => {
    const machine = new DuplexSessionMachine()
    machine.applyEvent({ type: 'ready' })
    machine.applyEvent({ type: 'partial', text: 'hey mar' })

    expect(machine.state.partialCaption).toBe('hey mar')
  })

  it('finalizes an utterance: sets speaker + caption, clears the partial and any stale reply', () => {
    const machine = new DuplexSessionMachine()
    machine.applyEvent({ type: 'ready' })
    machine.applyEvent({ type: 'partial', text: 'what is' })
    machine.applyEvent({ type: 'utterance', text: 'what is the weather', speaker: 'owner', speaker_name: 'Shereef' })

    expect(machine.state).toMatchObject({
      partialCaption: null,
      phase: 'replying',
      replySource: null,
      replyText: null,
      speaker: 'owner',
      speakerName: 'Shereef',
      utteranceCaption: 'what is the weather'
    })
  })

  it('defaults an unrecognized speaker to unknown (validated at the raw-event boundary)', () => {
    const machine = new DuplexSessionMachine()
    // applyRawEvent is what actually goes through parseDuplexServerEvent's
    // validation — applyEvent trusts an already-typed DuplexServerEvent, so
    // the normalization has to be exercised at this boundary instead.
    machine.applyRawEvent({ type: 'utterance', text: 'hi', speaker: 'robot' })

    expect(machine.state.speaker).toBe('unknown')
  })

  describe('instant reply streaming', () => {
    it('accumulates instant_delta chunks into replyText and enters replying', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'ready' })
      machine.applyEvent({ type: 'utterance', text: 'tell me a joke', speaker: 'owner' })
      machine.applyEvent({ type: 'instant_delta', text: 'Why did ' })
      machine.applyEvent({ type: 'instant_delta', text: 'the chicken cross the road?' })

      expect(machine.state.phase).toBe('replying')
      expect(machine.state.replySource).toBe('instant')
      expect(machine.state.replyText).toBe('Why did the chicken cross the road?')
    })

    it('instant_done replaces the accumulated text with the final version', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'utterance', text: 'hi', speaker: 'owner' })
      machine.applyEvent({ type: 'instant_delta', text: 'partial...' })
      machine.applyEvent({ type: 'instant_done', text: 'Hello there!' })

      expect(machine.state.replyText).toBe('Hello there!')
      expect(machine.state.replySource).toBe('instant')
    })
  })

  describe('TTS playback lifecycle', () => {
    it('tts_start enters speaking, arms bargeable, and resets playback', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'instant_done', text: 'Hello!' })
      const commands = machine.applyEvent({ type: 'tts_start' })

      expect(commands).toEqual([{ type: 'reset_playback' }])
      expect(machine.state.phase).toBe('speaking')
      expect(machine.state.bargeable).toBe(true)
    })

    it('tts_chunk while speaking produces an enqueue_audio command', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      const commands = machine.applyEvent({ type: 'tts_chunk', data: 'AAAA', seq: 0 })

      expect(commands).toEqual([{ type: 'enqueue_audio', data: 'AAAA', seq: 0 }])
    })

    it('ignores tts_chunk when not in a speaking session', () => {
      const machine = new DuplexSessionMachine()
      const commands = machine.applyEvent({ type: 'tts_chunk', data: 'AAAA', seq: 0 })

      expect(commands).toEqual([])
      expect(machine.state.phase).toBe('connecting')
    })

    it('tts_end requests a playback-end watch but keeps phase speaking until playback actually drains', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      const commands = machine.applyEvent({ type: 'tts_end' })

      expect(commands).toEqual([{ type: 'expect_playback_end' }])
      expect(machine.state.phase).toBe('speaking')
    })

    it('notifyPlaybackFinished returns to listening and sends playback_done, only once', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_end' })

      const first = machine.notifyPlaybackFinished()
      expect(first).toEqual([{ type: 'send_playback_done' }])
      expect(machine.state.phase).toBe('listening')
      expect(machine.state.bargeable).toBe(false)

      const second = machine.notifyPlaybackFinished()
      expect(second).toEqual([])
    })

    it('notifyPlaybackFinished before any tts_end is a no-op', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })

      expect(machine.notifyPlaybackFinished()).toEqual([])
      expect(machine.state.phase).toBe('speaking')
    })
  })

  describe('phase-downgrade guard (no flapping while audio plays/queues)', () => {
    it('instant_delta arriving after tts_start accumulates replyText but does not knock phase down to replying', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_chunk', data: 'AAAA', seq: 0 })

      // Next sentence's text streams in while the current sentence's audio is
      // still playing/queued — this used to flip phase to 'replying' ("[phase]
      // thinking") even though audio kept playing underneath it.
      machine.applyEvent({ type: 'instant_delta', text: 'And another thing, ' })

      expect(machine.state.phase).toBe('speaking')
      expect(machine.state.replyText).toBe('And another thing, ')

      machine.applyEvent({ type: 'instant_delta', text: 'the sky is blue.' })
      expect(machine.state.phase).toBe('speaking')
      expect(machine.state.replyText).toBe('And another thing, the sky is blue.')
    })

    it('instant_done arriving after tts_start replaces replyText but does not knock phase down to replying', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })

      machine.applyEvent({ type: 'instant_done', text: 'Final sentence text.' })

      expect(machine.state.phase).toBe('speaking')
      expect(machine.state.replyText).toBe('Final sentence text.')
      expect(machine.state.replySource).toBe('instant')
    })

    it('covers a full TTS-sentence-gap turn: tts_end, then next sentence text streams in before the next tts_start, phase never leaves speaking until playback drains', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_chunk', data: 'AAAA', seq: 0 })
      machine.applyEvent({ type: 'tts_end' })
      expect(machine.state.phase).toBe('speaking')

      // Server gap between sentences: next sentence's reply text arrives
      // before its tts_start, while the previous sentence's audio may still
      // be draining in the audio transport.
      machine.applyEvent({ type: 'instant_delta', text: 'Next sentence...' })
      expect(machine.state.phase).toBe('speaking')

      machine.applyEvent({ type: 'tts_start' })
      expect(machine.state.phase).toBe('speaking')

      // Only once THIS sentence's playback actually drains (its own tts_end
      // fires the watch tts_start above reset) does the phase leave speaking.
      machine.applyEvent({ type: 'tts_end' })
      const commands = machine.notifyPlaybackFinished()
      expect(commands).toEqual([{ type: 'send_playback_done' }])
      expect(machine.state.phase).toBe('listening')
    })

    it('a barge_in still tears speaking down to listening immediately even mid-guard', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'instant_delta', text: 'partial next sentence' })
      expect(machine.state.phase).toBe('speaking')

      const commands = machine.applyEvent({ type: 'barge_in' })

      expect(commands).toEqual([{ type: 'reset_playback' }])
      expect(machine.state.phase).toBe('listening')
      expect(machine.state.replyText).toBeNull()
    })

    it('instant_delta before any tts_start (normal reply flow, not speaking yet) still enters replying as before', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'utterance', text: 'hi', speaker: 'owner' })
      machine.applyEvent({ type: 'instant_delta', text: 'Hello' })

      expect(machine.state.phase).toBe('replying')
      expect(machine.state.replyText).toBe('Hello')
    })
  })

  describe('barge-in', () => {
    it('kills playback and returns to listening immediately, clearing the in-flight reply', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'instant_done', text: 'Some reply' })
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_chunk', data: 'AAAA', seq: 0 })

      const commands = machine.applyEvent({ type: 'barge_in' })

      expect(commands).toEqual([{ type: 'reset_playback' }])
      expect(machine.state.phase).toBe('listening')
      expect(machine.state.bargeable).toBe(false)
      expect(machine.state.replyText).toBeNull()
    })

    it('suppresses a stale playback_done after a barge-in interrupts mid-speech', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'barge_in' })

      // A tts_end that was already in flight when the barge_in landed should
      // not resurrect an "expect playback end" watch for audio that was just
      // flushed.
      const endCommands = machine.applyEvent({ type: 'tts_end' })
      expect(endCommands).toEqual([])
      expect(machine.notifyPlaybackFinished()).toEqual([])
    })

    it('does not cancel an outstanding escalation (deep task keeps running through a barge-in on the ack)', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'escalated', ack_text: 'Let me look into that.', task_id: 'task-1' })
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_chunk', data: 'AAAA', seq: 0 })

      machine.applyEvent({ type: 'barge_in' })

      expect(machine.state.deepWork).toEqual({ ackText: 'Let me look into that.', taskId: 'task-1', mode: 'thinking' })
    })
  })

  describe('escalation + deep results', () => {
    it('escalated sets the ack as the reply text and raises deepWork', () => {
      const machine = new DuplexSessionMachine()

      const commands = machine.applyEvent({
        type: 'escalated',
        ack_text: 'Give me a moment to check.',
        task_id: 'task-42'
      })

      expect(commands).toEqual([])
      expect(machine.state.phase).toBe('replying')
      expect(machine.state.replyText).toBe('Give me a moment to check.')
      expect(machine.state.deepWork).toEqual({
        ackText: 'Give me a moment to check.',
        taskId: 'task-42',
        mode: 'thinking'
      })
    })

    it('stays interactive after escalation: a new utterance is accepted normally while deepWork is pending', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'escalated', ack_text: 'Working on it.', task_id: 'task-1' })
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_end' })
      machine.notifyPlaybackFinished()

      expect(machine.state.phase).toBe('listening')
      expect(machine.state.deepWork).not.toBeNull()

      machine.applyEvent({ type: 'utterance', text: 'also add oat milk', speaker: 'owner' })
      machine.applyEvent({ type: 'instant_done', text: 'Sure, adding oat milk.' })

      expect(machine.state.utteranceCaption).toBe('also add oat milk')
      expect(machine.state.replyText).toBe('Sure, adding oat milk.')
      // The earlier escalation is still tracked — a second, unrelated turn
      // must not clear it.
      expect(machine.state.deepWork).toEqual({ ackText: 'Working on it.', taskId: 'task-1', mode: 'thinking' })
    })

    it('tracks delegated work and live tool activity separately from thinking', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({
        type: 'escalated',
        ack_text: "I'll hand this to a sub-agent.",
        task_id: 'task-work',
        mode: 'delegating'
      })

      expect(machine.state.deepWork?.mode).toBe('delegating')
      expect(machine.state.activity).toEqual({ kind: 'delegation', label: 'Sub-agent is working' })

      machine.applyEvent({ type: 'activity', status: 'started', kind: 'file', label: 'Reviewing files' })
      expect(machine.state.activity).toEqual({ kind: 'file', label: 'Reviewing files' })

      machine.applyEvent({ type: 'activity', status: 'completed', kind: 'file', label: 'Reviewing files' })
      expect(machine.state.activity).toEqual({ kind: 'delegation', label: 'Sub-agent is working' })
      expect(machine.state.deepWork?.mode).toBe('delegating')
    })

    it('deep_result after further utterances still applies and clears deepWork', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'escalated', ack_text: 'On it.', task_id: 'task-7' })
      machine.applyEvent({ type: 'utterance', text: 'one more thing', speaker: 'owner' })
      machine.applyEvent({ type: 'instant_done', text: 'Sure.' })

      const commands = machine.applyEvent({ type: 'deep_result', task_id: 'task-7', text: 'Here is the full answer.' })

      expect(commands).toEqual([])
      expect(machine.state.phase).toBe('replying')
      expect(machine.state.replyText).toBe('Here is the full answer.')
      expect(machine.state.replySource).toBe('deep')
      expect(machine.state.deepWork).toBeNull()
    })

    it('tracks concurrent background tasks and clears only the matching result', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'escalated', ack_text: 'Researching it.', task_id: 'research-1' })
      machine.applyEvent({
        type: 'escalated',
        ack_text: 'Delegating that change.',
        task_id: 'work-2',
        mode: 'delegating'
      })

      expect(machine.state.backgroundTasks.map(task => task.taskId)).toEqual(['research-1', 'work-2'])

      machine.applyEvent({ type: 'deep_result', task_id: 'research-1', text: 'Research finished.' })

      expect(machine.state.backgroundTasks.map(task => task.taskId)).toEqual(['work-2'])
      expect(machine.state.deepWork?.taskId).toBe('work-2')
    })

    it('deep_result followed by its own TTS cycle plays like any other reply', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'escalated', ack_text: 'On it.', task_id: 'task-1' })
      machine.applyEvent({ type: 'deep_result', task_id: 'task-1', text: 'Final answer.' })

      const startCommands = machine.applyEvent({ type: 'tts_start' })
      const chunkCommands = machine.applyEvent({ type: 'tts_chunk', data: 'BBBB', seq: 0 })

      expect(startCommands).toEqual([{ type: 'reset_playback' }])
      expect(chunkCommands).toEqual([{ type: 'enqueue_audio', data: 'BBBB', seq: 0 }])
      expect(machine.state.phase).toBe('speaking')
    })
  })

  describe('error handling', () => {
    it('records an unattributed error without erasing background work', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'escalated', ack_text: 'On it.', task_id: 'task-1' })

      const commands = machine.applyEvent({ type: 'error', error: 'instant model unreachable' })

      expect(commands).toEqual([])
      expect(machine.state.lastError).toBe('instant model unreachable')
      expect(machine.state.deepWork?.taskId).toBe('task-1')
    })
  })

  describe('malformed / unknown events', () => {
    const cases: Array<[string, unknown]> = [
      ['null', null],
      ['a number', 42],
      ['a string', 'ready'],
      ['an array', ['ready']],
      ['missing type', { text: 'hi' }],
      ['unknown type', { type: 'nonsense' }],
      ['partial missing text', { type: 'partial' }],
      ['utterance missing text', { type: 'utterance', speaker: 'owner' }],
      ['tts_chunk missing data', { type: 'tts_chunk', seq: 0 }],
      ['tts_chunk missing seq', { type: 'tts_chunk', data: 'AAAA' }],
      ['escalated missing ack_text', { type: 'escalated', task_id: 'x' }],
      ['deep_result missing task_id', { type: 'deep_result', text: 'x' }]
    ]

    it.each(cases)('ignores %s safely: no commands, no state change', (_label, raw) => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'ready' })
      const before = machine.state

      const commands = machine.applyRawEvent(raw)

      expect(commands).toEqual([])
      expect(machine.state).toEqual(before)
    })

    it('never throws when fed garbage', () => {
      const machine = new DuplexSessionMachine()
      expect(() => machine.applyRawEvent(undefined)).not.toThrow()
      expect(() => machine.applyRawEvent(() => {})).not.toThrow()
      expect(() => machine.applyRawEvent(Symbol('x'))).not.toThrow()
    })

    it('an error event with a non-string error field falls back to a generic message', () => {
      const machine = new DuplexSessionMachine()
      machine.applyRawEvent({ type: 'error' })

      expect(machine.state.lastError).toBe('Unknown duplex error')
    })
  })

  describe('close', () => {
    it('sends stop + resets playback and marks the session closed', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })

      const commands = machine.close()

      expect(commands).toEqual([{ type: 'send_stop' }, { type: 'reset_playback' }])
      expect(machine.state.phase).toBe('closed')
      expect(machine.state.bargeable).toBe(false)
    })

    it('suppresses a pending playback-end watch once closed', () => {
      const machine = new DuplexSessionMachine()
      machine.applyEvent({ type: 'tts_start' })
      machine.applyEvent({ type: 'tts_end' })
      machine.close()

      expect(machine.notifyPlaybackFinished()).toEqual([])
    })
  })
})
