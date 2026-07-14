# Marvi Smart Room Engine v0.3 — Plugin Architecture & World-Awareness Revision

> Version: 0.3 · Date: 2026-07-14 · Implementer: Codex agent
> Base document: `smart-room-spec v0.2` (uploaded; all sections not amended
> here remain in force — v0.3 is a DELTA, read both).
> Status: approved direction; Phase 0 hardware validation still mandatory.

## What changed since v0.2 and why

1. **The engine is a Marvi PLUGIN, not core code and not a separate Windows
   service.** Marvi's gateway is already the always-on, auto-restarting,
   supervised resident process — adding a second NSSM service duplicates all
   of that. The engine ships in `plugins/smart_room/`.
2. **Room state becomes world-awareness context, NOT memory.** Marvi should
   *know* the room the way it knows the desktop (presence layer), without a
   single automatic memory write.
3. **iPhone location joins the model** via iOS Shortcuts geofence webhooks —
   no cloud API, no GPS polling, and it fixes the work-return heuristic.
4. **BLE deep-sleep is handled explicitly.** iPhones suppress BLE advertising
   when locked/idle unless a service needs the radio, so BLE silence is NOT
   evidence of absence. The fusion rules below make it impossible for a
   sleeping phone to cause a wrong decision.
5. **ESP32 maximization is tiered** with an honest assessment of how much of
   the system can live on it.

---

## A. Plugin packaging (replaces v0.2 §9 project layout + Windows-service parts of §3.2/§20 Phase 6)

Ships as `plugins/smart_room/` in the Marvi repo, following the existing
plugin conventions (see `plugins/google_meet/` for the supervised-child
pattern and `plugins/spotify/plugin.yaml` for tool declaration):

```text
plugins/smart_room/
├── plugin.yaml            # name, kind: backend, provides_tools, platforms: [windows]
├── SKILL.md               # teaches Marvi when/how to use the room tools
├── bridge.py              # tool handlers -> runtime RPC (thin, non-blocking)
├── process_manager.py     # spawn/supervise the runtime child process
│                          #   (mirror google_meet/process_manager.py)
├── context.py             # world-context provider (section B)
├── runtime/               # v0.2's engine, unchanged internal design:
│   ├── app.py runtime.py models.py command_router.py presence_fusion.py
│   ├── automation_engine.py scheduler.py state_store.py event_bus.py health.py
│   ├── mqtt/ tuya/ ...    # exactly as specced in v0.2 §8-§16
├── scripts/               # v0.2 Phase-0 scripts (discover_tuya, inspect_dps, inspect_mqtt)
└── tests/
```

- **Process model:** the runtime runs as ONE child process spawned and
  supervised by the plugin when the gateway starts (auto-restart with
  backoff, mirroring google_meet's process manager). A runtime crash never
  touches the gateway. No NSSM, no install_service.ps1, no second service —
  delete those from scope. Mosquitto remains the only external Windows
  service (v0.2 §8.1 unchanged).
- **Bridge transport:** JSON-RPC over a local socket or stdio between
  bridge.py and the runtime (request_id + typed acks exactly as v0.2 §10.2/10.3).
  Tool handlers must never block the gateway loop — bounded wait via
  run_in_threadpool/asyncio, timeout returns the structured DEVICE_TIMEOUT
  failure from v0.2 §16.
- **Tools (plugin.yaml `provides_tools`, registered per the tools/registry
  pattern):** `smart_room_state`, `smart_room_set_mode`, `smart_room_set_light`,
  `smart_room_cancel_sleep`, `smart_room_override`, `smart_room_health`,
  `smart_room_diagnostic` (same semantics as v0.2 §8.8, snake_case flat names
  to match the registry's existing naming).
- **Config:** a `smart_room:` section in Marvi's config.yaml (cfg_get
  pattern) replaces the standalone config/config.yaml for wiring-level
  settings; scenes and thresholds may stay in
  `HERMES_HOME/smart_room/{config,scenes}.yaml` owned by the runtime.
  Secrets stay in env / Windows Credential Manager exactly as v0.2 §11.2.
- **State/logs:** `HERMES_HOME/smart_room/` (state snapshot, logs) — same
  atomic-write rules as v0.2 §8.7.

## B. World-awareness, beside control — never memory (replaces v0.2 §8.9)

The principle: Marvi is *aware* of the room the way it is aware of the
desktop — ambient context, zero automatic memory writes.

1. **On-demand:** `smart_room_state` returns the full v0.2 §10.1 snapshot.
2. **Ambient session context:** `context.py` contributes ONE compact line to
   the session-context path (same mechanism as presence session priming in
   gateway/run.py — guarded import, config-gated `smart_room.context.enabled`
   default true): e.g. `Room: reading mode, Shereef present (BLE+mmWave,
   conf 0.94), light 70% @3000K, phone: home.` Injected at session start and
   available to the voice instant lane's prompt suffix the same way deferred
   personal context is.
3. **Subconscious surface (optional, config `smart_room.subconscious.enabled`
   default true):** the runtime appends MEANINGFUL transitions (mode changes,
   arrivals/departures, device-offline) as a `## smart_room` diff section via
   a tiny stage-1 fetcher (`cron/scripts/subconscious/smart_room.py`, reads
   the runtime snapshot over the bridge — follows the fetcher pattern:
   NO_CHANGE contract, cursor = last event id). The world becomes something
   the subconscious can think about ("he got home 20 min ago and the room is
   dark — rhythm says deep work starts soon").
4. **Memory:** the runtime NEVER writes memory/Honcho. If a durable pattern
   is worth keeping (v0.2 §8.9's allowed list), the SUBCONSCIOUS proposes it
   through the suggestions inbox like every other insight — the runtime just
   supplies raw transitions. Delete the runtime-side "curated long-term
   context events" writer from scope.
5. **Activity feed:** meaningful room transitions MAY also append to
   `HERMES_HOME/subconscious/activity.jsonl` with `source: "world"` (additive
   enum value; the Activity UI already renders unknown sources generically —
   verify, else add the chip). This gives the Mind page eyes on the room.

## C. iPhone location (new; upgrades v0.2 §13.6 work-return)

iOS Shortcuts personal automations (Arrive/Leave location) fire reliably in
the background and can POST a webhook — no cloud account, no polling, no app:

- Marvi's gateway already has webhook routes with HMAC auth
  (`hermes webhook subscribe`). Add subscription `smart-room-location` with
  payload `{"who":"shereef","transition":"arrive|leave","zone":"home|uni|bakery","at":ISO}`.
  The plugin consumes it and forwards to the runtime as a
  `PHONE_LOCATION_CHANGED` event (new event in v0.2 §14's list).
- Runtime keeps `phone_location: {zone, since, source}` in the snapshot
  (v0.2 §10.1 gains this block) — world context line includes it.
- **Work-return v2:** trigger = geofence `arrive home` during the arrival
  window (primary) OR BLE arrival (fallback when no webhook seen for >24h).
  Settle timer and cancellation rules unchanged from v0.2 §13.6.
- Setup doc: SKILL.md includes the 2-minute Shortcuts recipe (Automation →
  Arrive → URL → POST). Zones beyond `home` are optional.

## D. BLE deep-sleep: identity must survive a silent phone (amends v0.2 §8.4)

Research-confirmed behavior: iPhones suppress BLE advertisements when locked
unless something needs the radio (Apple Watch, Handoff, iCloud services keep
it chatty; a bare idle phone goes silent). IRK enrollment (ESPresense v3
enroll flow) solves MAC randomization but NOT sleep silence. Therefore:

**Fusion axioms (hard rules, unit-tested):**
1. BLE **presence** is strong evidence FOR identity. BLE **absence** is weak
   evidence and NEVER sufficient on its own for any absent-decision.
2. `shereef_in_room` becomes **sticky**: once established (BLE seen in-room),
   it is held while `mmwave_occupied` remains true, even if BLE goes silent —
   with `identity_confidence` decaying over time (e.g. 0.95 → floor 0.6 over
   2h) and `presence_reason: "ble_sticky_mmwave"`. Rationale: a sleeping
   phone on the desk + a warm body on the bed is Shereef until proven
   otherwise.
3. Sticky identity releases when: mmWave clears for its timeout (room empty),
   OR the geofence webhook says `leave home`, OR another enrolled identity
   appears without Shereef's.
4. Light-off still requires mmWave clear + BLE absent (v0.2 policy) — the
   phone-asleep-in-room case can never darken an occupied room.
5. `someone_in_room` (mmWave alone) never upgrades to `shereef_in_room`
   without at least one BLE/geofence identity signal that session.

**Signal hygiene:**
- ESPresense: use the v3 secure enroll (IRK), room id `smart_room`,
  and capture live topic/payload as fixtures (v0.2 §19.4 unchanged).
- Mitigations that INCREASE BLE chatter are user-optional, documented in
  SKILL.md (Apple Watch presence, Handoff on), never assumed by the fusion.
- Optional third signal, config-gated `presence.wifi_ping.enabled` (default
  off): ARP/ping of the iPhone's reserved IP every 60s; a response is
  positive identity evidence (phones answer intermittently even in standby;
  a timeout means nothing). Positive-only, same axiom as BLE.

## E. ESP32 maximization — tiered honestly (new)

The wish: run as much as possible on the ESP32. The assessment:

- **Tier 1 (v0.3 scope, recommended):** ESP32 runs ESPresense, dedicated —
  the best-supported BLE identity stack (IRK enroll flow, room-level
  filtering, MQTT). The engine stays on the PC because Marvi is on the PC,
  and because the deep-sleep mitigations (geofence webhooks) terminate at
  the gateway anyway. ESP32 also publishes its own telemetry
  (espresense/rooms/smart_room status) — the runtime's health model treats
  ESP32 staleness per v0.2 §16.4.
- **Tier 2 (v0.4, worth doing):** replace ESPresense with a custom
  ESPHome/Arduino firmware that combines NimBLE IRK tracking AND direct Tuya
  LAN control from the ESP32 — feasible: EspTuya implements Tuya local
  protocol 3.4/3.5 on ESP32 (github.com/FrBerger83/EspTuya). Put a FAILSAFE
  rule tier on-device: `mmWave occupied → light on scene default; clear
  timeout → off; sleep-mode flag (retained MQTT) suppresses`. Result: the
  room keeps working when the PC is off; the PC runtime supervises, owns
  schedules/modes, and overrides via retained flags. This is the correct
  meaning of "maximize the ESP32" — autonomy floor on the device, brains on
  the PC.
- **Tier 3 (full system on ESP32) — not recommended:** possible in theory
  (EspTuya + NimBLE + rules), but you lose: the HE20 (it is a Tuya WiFi
  node — the ESP32 would poll it over WiFi anyway, same as the PC), the
  geofence webhook terminus, scheduling robustness, structured logs, tests,
  and every Marvi integration. The ceiling is documented here so nobody
  half-builds it; revisit only if the PC stops being always-on.
- **HE20 note for Phase 0:** if local DPS polling proves unreliable
  (v0.2 §26 open question), the Tier-2 fallback is wiring a cheap UART
  mmWave module (LD2410-class) directly to the ESP32 — flag in the Phase 0
  report; do not buy hardware preemptively.

## F. Scope corrections to v0.2 (deletions/simplifications)

- DELETE: install_service.ps1, Windows-service phase-6 items (plugin
  supervision replaces them; gateway + Electron already auto-start).
- DELETE: runtime-side long-term-context writer (§8.9 writer) — replaced by
  section B.4 (subconscious proposes; runtime never writes memory).
- Alarm default 23:30 in v0.2 §11/§13.5 looks like a placeholder bedtime-
  alarm; make `alarm_time` explicitly nullable/disabled-by-default and
  configurable from the tool (`smart_room_set_mode alarm` + schedule via
  Marvi's own cron if the user wants wake-ups — don't duplicate a scheduler
  feature Marvi already has for the DAILY trigger; the runtime keeps only
  the in-room alarm BEHAVIOR: flash-bounded, auto-expire, restore).
- Same logic for evening-sleep DAILY trigger: the runtime keeps the
  behavior + settle/cancel logic; the schedule may be driven either by its
  internal scheduler (default) or by a Marvi cron job calling the tool —
  keep the internal path (offline autonomy) but document the override.
- v0.2 §7.1 stands and extends: Marvi never shells mosquitto_pub; the plugin
  bridge is the only Marvi-facing surface.

## G. Testing & acceptance deltas

All v0.2 §21 tests stand. Add:
- Fusion axioms: sleeping-phone matrix (BLE silent + mmWave occupied holds
  identity with decay; geofence leave releases; mmWave clear releases;
  positive-only wifi-ping evidence).
- Geofence webhook → PHONE_LOCATION_CHANGED → work-return v2 trigger; HMAC
  rejection; stale/duplicate webhook idempotency.
- Plugin lifecycle: gateway start spawns runtime; runtime crash → backoff
  restart → tools return structured unavailable during downtime (never
  hang); gateway shutdown terminates child cleanly.
- Context line: present/absent per config; NEVER any memory-write call from
  runtime code (assert no memory_tool import in runtime/).
- Acceptance additions: (15) plugin restart-supervision proven by kill test;
  (16) sleeping-phone soak: phone locked in room 4h overnight with mmWave
  occupied → zero false light-offs, identity held ≥ floor confidence.

## H. Phase 0 additions

- Record iPhone BLE advertising behavior EMPIRICALLY: locked-idle at desk
  for 60 min (with and without Apple Watch nearby if available) — log gap
  distribution; these gaps calibrate `missing_timeout_seconds` and the
  sticky-decay curve (measurements, not guesses — same rule as v0.2 §19.5).
- Verify the Shortcuts geofence webhook fires from lock screen state and
  measure delivery latency to the gateway route.

## Sources

- ESPresense Apple guidance and iOS advertising behavior:
  https://espresense.com/apple/ · https://espresense.com/guides/enrolling-devices/
- iPhone stops advertising when locked (community confirmations):
  https://github.com/ESPresense/ESPresense/issues/234 ·
  https://github.com/ESPresense/ESPresense/discussions/492
- IRK enroll flow walkthrough:
  https://www.jamesridgway.co.uk/reliable-ios-presence-detection-with-espresense-v3-enroll-flow-and-irk/
- ESP32 native Tuya LAN control (protocol 3.4/3.5):
  https://github.com/FrBerger83/EspTuya ·
  https://github.com/jasonacox/tinytuya/blob/master/PROTOCOL.md
- ESPHome Tuya MCU component (serial-wired Tuya, Tier-2 reference):
  https://esphome.io/components/tuya/
