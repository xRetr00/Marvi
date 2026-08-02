# Marvi Smart Room Engine — Revised Architecture & Implementation Specification

> **Version:** 0.2 Revised Draft
> **Date:** 2026-07-14
> **Owner:** Shereef / Marvi
> **Status:** Ready for Phase 0 validation
> **Target Platform:** Windows 11, native Python, no Docker or WSL

---

## 1. Executive Summary

The Marvi Smart Room Engine is a lightweight, native Windows service that replaces the current Home Assistant deployment for one room.

It will:

1. Control the Tuya RGBCW bulb locally over the LAN.
2. Read the Tuya HE20 mmWave presence sensor locally where supported.
3. Identify Shereef's iPhone in the room using ESP32 + ESPresense.
4. Combine BLE identity and mmWave occupancy into one reliable presence model.
5. Run room automations through a deterministic state machine.
6. Expose direct tools to Marvi for reading state and controlling the room.
7. Keep transient room state separate from Marvi's long-term memory.
8. Run as a small Windows service without Home Assistant, Docker, or WSL.

This system is intentionally limited to Shereef's personal room and devices. It is not intended to become a general-purpose home automation platform.

---

## 2. Problem Statement

The current Home Assistant deployment creates unnecessary operational cost and complexity for a small setup:

- Docker and WSL consume several gigabytes of RAM.
- Tuya local integrations can become unavailable or require repeated repair.
- Home Assistant exposes far more entities and services than this room needs.
- Automations are spread across helpers, YAML, integrations, and containers.
- Marvi does not receive clean, native real-world context.
- Marvi cannot reliably execute room actions through a typed tool interface.
- Debugging device state across Home Assistant, Docker, MQTT, and Tuya is cumbersome.

The replacement must remain simple, local-first, debuggable, and tightly integrated with Marvi.

---

## 3. Goals

### 3.1 Functional Goals

- Local on/off, brightness, color temperature, RGB, and scene control for the RGBCW bulb.
- Local reading of HE20 occupancy and supported settings.
- Room-level identity detection for Shereef's iPhone.
- Fused occupancy decisions using BLE and mmWave.
- Reading, Focus, Relax, Night, Sleep, Alarm, and Off modes.
- Presence-adaptive light behavior.
- Work-return and evening-sleep routines.
- Full state inspection by Marvi.
- Typed room-control tools for Marvi.
- Durable recovery after process restart or temporary network failure.

### 3.2 Operational Goals

- Native Windows runtime.
- Low idle memory use.
- Start automatically with Windows.
- Recover automatically from MQTT or device disconnections.
- Preserve only necessary state across restarts.
- Produce useful structured logs.
- Avoid cloud dependencies after Tuya local keys are obtained.

### 3.3 Safety and Reliability Goals

- No automation may create rapid command storms.
- Sleep mode must reliably suppress presence-triggered lighting.
- Alarm behavior must have a bounded duration and safe flashing rate.
- Device timeouts must not block the entire event loop.
- Invalid or conflicting modes must be impossible.
- Every Marvi command must return a success or failure result.

---

## 4. Non-Goals for Version 0.1

The following are explicitly deferred:

- Whole-home automation.
- Multiple rooms.
- A public web dashboard.
- Remote internet exposure.
- Cloudflare Tunnel.
- Tailscale control.
- Telegram notifications.
- Weather integration.
- Sunrise and sunset automation.
- Honcho-based pattern learning.
- Multi-user identity tracking.
- A replacement for every Home Assistant feature.

These may be added after the core room runtime is stable.

---

## 5. Devices

| Device | Role | Protocol | Expected Integration |
|---|---|---|---|
| Tuya RGBCW bulb | Room lighting | Wi-Fi / Tuya LAN | TinyTuya worker |
| Tuya HE20 sensor | Human occupancy | Wi-Fi / Tuya LAN | TinyTuya polling or local events if supported |
| ESP32 | BLE room receiver | Wi-Fi + BLE | ESPresense firmware |
| Shereef's iPhone | Identity beacon | BLE | ESPresense secure enrollment |
| Windows PC | Runtime host | Ethernet or Wi-Fi | Python service + Mosquitto |

Device IP addresses should be reserved in the router using DHCP reservations.

---

## 6. Design Principles

1. **Local first:** Room operation must not depend on Tuya Cloud after device keys are obtained.
2. **One authoritative state machine:** The runtime owns the active room mode.
3. **Transient state is not memory:** RSSI, current brightness, and occupancy remain runtime state.
4. **Events over polling where possible:** Publish meaningful state changes, not repetitive noise.
5. **Queues around blocking I/O:** Tuya socket operations must never block MQTT or scheduler callbacks.
6. **Explicit acknowledgements:** Every command has a request ID and result.
7. **Fail safe:** Uncertain presence must not repeatedly toggle the bulb.
8. **Small scope:** Build only what this room and Marvi actually need.

---

## 7. Revised Architecture

```text
                         ┌──────────────────────┐
                         │        Marvi         │
                         │                      │
                         │ smart_room.get_state │
                         │ smart_room.set_mode  │
                         │ smart_room.set_light │
                         └──────────┬───────────┘
                                    │
                         Direct Python tool/API
                                    │
                                    ▼
┌──────────────────┐      ┌──────────────────────────────┐
│ ESP32 /          │ MQTT │ Marvi Smart Room Runtime     │
│ ESPresense       ├─────►│                              │
│ BLE identity     │      │ - Command Router             │
└──────────────────┘      │ - Presence Fusion            │
                          │ - Automation State Machine    │
┌──────────────────┐      │ - Scheduler                  │
│ Mosquitto        │◄────►│ - Runtime State Store        │
│ Windows service  │      │ - Event Publisher            │
└──────────────────┘      │ - Health Monitor             │
                          └──────────────┬───────────────┘
                                         │ command queue
                                         ▼
                              ┌──────────────────────┐
                              │ TinyTuya Worker      │
                              │ retries + timeouts   │
                              └──────────┬───────────┘
                                         │ Tuya LAN
                            ┌────────────┴────────────┐
                            ▼                         ▼
                    ┌──────────────┐          ┌──────────────┐
                    │ RGBCW Bulb   │          │ HE20 Sensor  │
                    └──────────────┘          └──────────────┘
```

### 7.1 Architectural Decision: Marvi Does Not Use Shell MQTT Commands

The primary Marvi integration will be a direct tool or local API, not repeated calls to `mosquitto_pub`.

MQTT remains the transport for ESPresense and optional state events. Marvi receives a typed interface with validation and structured results.

Shell-based MQTT commands may remain available only as a diagnostic fallback.

---

## 8. Components

### 8.1 Mosquitto MQTT Broker

**Purpose:** Receive ESPresense BLE events and optionally publish room state events.

Requirements:

- Runs as a Windows service.
- Listens on the PC's LAN address, not only `127.0.0.1`, because the ESP32 is a separate network device.
- Requires username/password authentication.
- Uses ACL rules so ESPresense can publish only its allowed topics.
- Does not expose port 1883 to the public internet.
- Uses retained messages only for current state snapshots, never for commands.

Example security model:

```text
User: espresense
  publish: espresense/#

User: smart-room-runtime
  subscribe: espresense/#
  publish: smart-room/state/#

User: diagnostics
  subscribe: #
```

### 8.2 ESPresense

**Purpose:** Identify Shereef's iPhone in the smart room.

Requirements:

- Flash ESPresense on the ESP32.
- Configure room ID as `smart_room`.
- Configure the MQTT broker using the Windows PC's LAN IP.
- Securely enroll the iPhone and use a stable device identity such as an IRK-based identifier.
- Do not depend on a changing BLE MAC address.
- Observe the real MQTT topic and payload during Phase 0 before hard-coding parsers.

Expected default topic shape:

```text
espresense/devices/<device_id>/<room_id>
```

The exact payload fields must be taken from the live device output and covered by parser tests.

### 8.3 TinyTuya Worker

**Purpose:** Perform blocking Tuya LAN communication without blocking the runtime.

Responsibilities:

- Discover and validate each device.
- Store protocol version and device-specific DPS mapping.
- Read bulb state.
- Control bulb power, brightness, color temperature, RGB, and scenes.
- Read HE20 occupancy if exposed through local DPS.
- Execute commands from a bounded queue.
- Return structured results to the runtime.
- Apply timeout, retry, backoff, and circuit-breaker behavior.

TinyTuya operations must not run directly inside:

- MQTT callbacks.
- Scheduler callbacks.
- Marvi tool handlers.
- Presence-fusion callbacks.

### 8.4 Presence Fusion

**Purpose:** Distinguish identity, occupancy, and final room-presence decisions.

Inputs:

- BLE identity signal from ESPresense.
- mmWave occupancy signal from HE20.
- Signal timestamps and quality.

Outputs:

- `shereef_in_room`
- `someone_in_room`
- `room_occupied`
- `presence_confidence`
- `presence_reason`

Presence meanings:

| Input State | Interpretation |
|---|---|
| BLE present + mmWave occupied | Shereef is very likely in the room |
| BLE present + mmWave clear | Shereef's phone is likely in or near the room |
| BLE absent + mmWave occupied | Someone is in the room, identity unknown |
| BLE absent + mmWave clear | Room is likely empty |

Default automation policy:

- Light-on may trigger from BLE entry **or** mmWave occupancy.
- Light-off requires both BLE absence and mmWave clear for a configured timeout.
- BLE alone identifies Shereef.
- mmWave alone proves occupancy but not identity.

### 8.5 Automation State Machine

The runtime uses one authoritative active mode.

```text
off
reading
focus
relax
night
sleep
alarm
```

The runtime must never represent several active scene modes as separate booleans.

Additional control flags are independent from the active mode:

- `manual_override`
- `work_return_enabled`
- `evening_sleep_enabled`
- `adaptive_light_enabled`

Priority order:

```text
alarm > sleep > manual override > selected scene > adaptive default
```

### 8.6 Scheduler

Responsible for:

- Daily alarm.
- Evening sleep.
- Daily reset.
- Work-return settle timer.
- Alarm expiration.
- Delayed presence clear.

Scheduled jobs must be persisted or reconstructed from runtime state after restart.

### 8.7 Runtime State Store

The runtime state store is authoritative for current room status.

It may be held in memory and periodically snapshotted to disk.

The snapshot must be written atomically:

1. Write to a temporary file.
2. Flush and close it.
3. Rename it over the previous snapshot.

Secrets must never be stored in the state snapshot.

### 8.8 Marvi Tool Bridge

Proposed tools:

```text
smart_room.get_state
smart_room.set_mode
smart_room.set_light
smart_room.cancel_sleep
smart_room.set_manual_override
smart_room.get_device_health
smart_room.run_diagnostic
```

The bridge validates arguments, creates a request ID, dispatches the command, waits for a bounded result, and returns a typed response.

### 8.9 Marvi Long-Term Context

The runtime must not write every state update to Marvi memory or Honcho.

Allowed long-term events include:

- A repeated preference for a scene.
- A stable sleep routine.
- Frequent cancellation of a scheduled automation.
- A device reliability pattern.
- A deliberate user preference change.

Disallowed long-term events include:

- Every RSSI change.
- Every brightness poll.
- Repeated identical occupancy values.
- Routine on/off transitions without learning value.

Marvi may read the current room snapshot directly whenever context is needed.

---

## 9. Project Structure

```text
smart_room/
├── app.py                       # Process entry point
├── config/
│   ├── config.yaml              # Non-secret configuration
│   └── scenes.yaml              # Scene definitions
├── smart_room/
│   ├── runtime.py               # Main orchestration
│   ├── models.py                # Typed state and command models
│   ├── command_router.py        # Command validation and dispatch
│   ├── presence_fusion.py       # BLE + mmWave state machine
│   ├── automation_engine.py     # Rules and mode transitions
│   ├── scheduler.py             # Timed jobs
│   ├── state_store.py           # Atomic persistent snapshot
│   ├── event_bus.py             # Internal event distribution
│   ├── health.py                # Device and service health
│   ├── logging_config.py        # Structured rotating logs
│   ├── mqtt/
│   │   ├── client.py            # MQTT connection management
│   │   └── espresense_parser.py # Topic and payload parser
│   ├── tuya/
│   │   ├── worker.py            # Dedicated blocking-I/O worker
│   │   ├── bulb.py              # Bulb adapter
│   │   ├── he20.py              # HE20 adapter
│   │   └── dps_map.py           # Validated device DPS mappings
│   └── marvi/
│       ├── tools.py             # Marvi tool definitions
│       └── context.py           # Curated context events
├── secrets/
│   └── .env.example             # Names only, no real keys
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   └── hardware/
├── scripts/
│   ├── discover_tuya.py
│   ├── inspect_dps.py
│   ├── inspect_mqtt.py
│   └── install_service.ps1
├── logs/
├── pyproject.toml
└── README.md
```

---

## 10. Data Models

### 10.1 Runtime State

```json
{
  "schema_version": 1,
  "updated_at": "2026-07-14T18:30:00+03:00",
  "room": {
    "id": "smart_room",
    "active_mode": "reading",
    "previous_mode": "relax",
    "manual_override": false
  },
  "presence": {
    "room_occupied": true,
    "shereef_in_room": true,
    "someone_in_room": true,
    "confidence": 0.94,
    "reason": "ble_and_mmwave",
    "ble": {
      "present": true,
      "rssi": -64,
      "distance": 1.8,
      "last_seen": "2026-07-14T18:29:58+03:00"
    },
    "mmwave": {
      "occupied": true,
      "last_changed": "2026-07-14T18:28:10+03:00"
    }
  },
  "light": {
    "online": true,
    "on": true,
    "brightness": 70,
    "color_temperature_kelvin": 3000,
    "rgb": null,
    "scene": "reading",
    "last_confirmed": "2026-07-14T18:29:55+03:00"
  },
  "automations": {
    "adaptive_light_enabled": true,
    "work_return_enabled": true,
    "evening_sleep_enabled": true,
    "work_sleep_done_today": false,
    "work_sleep_cancelled_today": false,
    "evening_sleep_done_today": false,
    "evening_sleep_cancelled_today": false
  },
  "devices": {
    "bulb": {
      "online": true,
      "consecutive_failures": 0,
      "last_success": "2026-07-14T18:29:55+03:00"
    },
    "he20": {
      "online": true,
      "consecutive_failures": 0,
      "last_success": "2026-07-14T18:29:54+03:00"
    },
    "esp32": {
      "online": true,
      "last_seen": "2026-07-14T18:29:58+03:00"
    },
    "mqtt": {
      "connected": true,
      "last_connected": "2026-07-14T17:58:00+03:00"
    }
  },
  "pending_actions": []
}
```

### 10.2 Command Request

```json
{
  "schema_version": 1,
  "request_id": "cmd-20260714-000123",
  "action": "set_mode",
  "parameters": {
    "mode": "reading"
  },
  "source": "marvi",
  "issued_at": "2026-07-14T18:30:00+03:00",
  "expires_at": "2026-07-14T18:30:10+03:00"
}
```

### 10.3 Command Result

```json
{
  "schema_version": 1,
  "request_id": "cmd-20260714-000123",
  "status": "success",
  "completed_at": "2026-07-14T18:30:01+03:00",
  "message": "Reading mode activated.",
  "state_changes": {
    "active_mode": "reading",
    "light.on": true,
    "light.brightness": 70
  },
  "error": null
}
```

Failure example:

```json
{
  "request_id": "cmd-20260714-000124",
  "status": "failed",
  "message": "The bulb did not respond within the configured timeout.",
  "error": {
    "code": "DEVICE_TIMEOUT",
    "device": "bulb",
    "retryable": true
  }
}
```

---

## 11. Configuration

### 11.1 Non-Secret Configuration

`config/config.yaml`:

```yaml
schema_version: 1

room:
  id: smart_room
  timezone: Europe/Istanbul
  default_mode: relax

mqtt:
  host: 192.168.1.20
  port: 1883
  username_env: SMART_ROOM_MQTT_USERNAME
  password_env: SMART_ROOM_MQTT_PASSWORD
  client_id: marvi-smart-room-runtime
  keepalive_seconds: 30
  reconnect_min_seconds: 1
  reconnect_max_seconds: 60
  espresense_topic: "espresense/devices/retro-phone/smart_room"
  state_topic: "smart-room/state/current"
  event_topic_prefix: "smart-room/events"

tuya_note: "The real key is stored in the environment, not in this file."

tuya:
  command_timeout_seconds: 3
  max_retries: 2
  retry_backoff_seconds: 1
  circuit_breaker_failures: 5
  circuit_breaker_reset_seconds: 60

  bulb:
    device_id: "REPLACE_AFTER_PHASE_0"
    ip: "192.168.1.50"
    local_key_env: SMART_ROOM_BULB_LOCAL_KEY
    protocol_version: "REPLACE_AFTER_PHASE_0"
    dps_profile: "rgbcw_bulb_v1"

  he20:
    device_id: "REPLACE_AFTER_PHASE_0"
    ip: "192.168.1.51"
    local_key_env: SMART_ROOM_HE20_LOCAL_KEY
    protocol_version: "REPLACE_AFTER_PHASE_0"
    dps_profile: "he20_v1"

presence:
  ble:
    enter_rssi: -70
    exit_rssi: -85
    enter_debounce_seconds: 3
    missing_timeout_seconds: 60
  mmwave:
    clear_timeout_seconds: 60
  fusion:
    light_on_when: "ble_or_mmwave"
    light_off_when: "ble_absent_and_mmwave_clear"

polling:
  bulb_seconds: 10
  he20_seconds: 2
  device_health_seconds: 60

scheduler:
  daily_reset: "00:00"
  alarm_time: "23:30"
  alarm_duration_minutes: 30
  evening_sleep_time: "18:00"

work_return:
  enabled: true
  arrival_window_start: "06:00"
  arrival_window_end: "10:00"
  settle_delay_seconds: 300
  source: "ble_arrival"

alarm:
  flash_on_seconds: 1
  flash_off_seconds: 1
  flash_duration_seconds: 60
  steady_brightness_after_flash: 100

logging:
  level: INFO
  json: true
  rotate_mb: 10
  backup_count: 5
```

### 11.2 Secrets

Real secrets are stored in environment variables or Windows Credential Manager:

```text
SMART_ROOM_MQTT_USERNAME
SMART_ROOM_MQTT_PASSWORD
SMART_ROOM_BULB_LOCAL_KEY
SMART_ROOM_HE20_LOCAL_KEY
```

The service account must have read access only to the required credentials and project files.

---

## 12. Light Scenes

Scene values are starting points and must be validated against the bulb's actual Tuya ranges.

| Mode | Color | Brightness | Transition | Notes |
|---|---:|---:|---:|---|
| Reading | 3000K warm white | 70% | 2 s | Comfortable text reading |
| Focus | 5000K cool white | 100% | 2 s | High alertness and work |
| Relax | 2700K warm amber | 40% | 3 s | Evening ambient light |
| Night | 2200K or warm RGB | 15% | 3 s | Low glare |
| Sleep | Off | 0% | 1 s | Suppresses presence-on automation |
| Alarm | 6500K cold white | 100% | Immediate | Bounded flashing, then steady |
| Off | Off | 0% | 1 s | Explicit off mode |

The engine must convert human-friendly values into the bulb's real DPS value ranges.

---

## 13. Automation Specification

### 13.1 Presence-Adaptive Light On

**Trigger:**

- BLE presence enters the room, or
- HE20 changes to occupied.

**Conditions:**

- Adaptive light is enabled.
- Active mode is not Sleep.
- Active mode is not Alarm.
- Manual override is not suppressing automation.

**Action:**

- If the light is off, restore the previous non-sleep scene.
- If no previous scene exists, activate the configured default mode.

**Debounce:** 3 seconds by default.

### 13.2 Light Off After Clear

**Trigger:**

- BLE is absent for the configured timeout, and
- HE20 is clear for the configured timeout.

**Conditions:**

- Active mode is not Alarm.
- Manual override does not require the light to remain on.

**Action:**

- Turn off the bulb.
- Preserve the previous scene for restoration.

### 13.3 Sleep Mode

**Entry:**

- Manual command.
- Work-return routine.
- Evening-sleep routine.

**Actions:**

- Save the previous non-sleep mode.
- Set active mode to Sleep.
- Turn off the light.
- Ignore presence-triggered light-on events.

**Exit:**

- Manual cancellation.
- Explicit mode change.

**Exit action:**

- Restore the previous scene only if room presence still justifies lighting.

### 13.4 Alarm Mode

**Entry:**

- Daily schedule or manual command.

**Actions:**

1. Set active mode to Alarm.
2. Set cold white at 100% brightness.
3. Flash at a safe one-second interval for up to 60 seconds.
4. Continue at steady 100% brightness for the remainder of the alarm duration.

**Exit:**

- Manual cancellation, or
- Automatic timeout after 30 minutes.

**Exit action:**

- Restore the previous valid mode.

### 13.5 Daily Alarm

**Trigger:** 23:30 Europe/Istanbul.

**Condition:** Alarm schedule enabled.

**Action:** Enter Alarm mode.

The alarm should not be skipped merely because Sleep mode is active; waking the user is the purpose of the alarm. A separate `alarm_enabled` flag controls whether the schedule is active.

### 13.6 Work-Return Sleep

Version 0.1 uses BLE arrival, not GPS.

**Trigger:**

- Shereef's iPhone changes from absent to present during the configured arrival window.

**Conditions:**

- Work-return automation enabled.
- Work sleep has not already run or been cancelled today.
- Active mode is not Alarm.

**Action:**

- Start a five-minute settle timer.
- If presence remains valid when the timer expires, enter Sleep mode.

**Cancellation:**

- If Shereef leaves before the timer expires, cancel the pending action.
- If Sleep mode is manually exited within ten minutes, mark work sleep cancelled for the day.

### 13.7 Evening Sleep

**Trigger:** 18:00 Europe/Istanbul.

**Conditions:**

- Evening-sleep automation enabled.
- Evening sleep has not already run or been cancelled today.

**Action:** Enter Sleep mode and mark the routine as completed.

**Cancellation behavior:**

- If Sleep mode is manually exited within ten minutes, prevent the evening routine from retriggering that day.

### 13.8 Scene Selection

Commands may select Reading, Focus, Relax, or Night.

Because `active_mode` is a single enum, selecting a scene automatically replaces the previous scene. No mutex booleans are required.

### 13.9 Daily Reset

**Trigger:** Midnight in Europe/Istanbul.

**Action:** Reset only daily automation markers:

- Work sleep completed.
- Work sleep cancelled.
- Evening sleep completed.
- Evening sleep cancelled.

Do not reset the current scene, manual override, or device health.

### 13.10 Manual Override

Manual override defines how automation may modify the light.

Proposed values:

```text
none
hold_on
hold_off
```

- `none`: normal automation.
- `hold_on`: presence-clear does not turn the light off.
- `hold_off`: presence-entry does not turn the light on.

Manual override remains active until explicitly cleared or until an optional expiry time.

---

## 14. Event Model

Internal events include:

```text
BLE_PRESENCE_ENTERED
BLE_PRESENCE_UPDATED
BLE_PRESENCE_EXITED
MMWAVE_OCCUPIED
MMWAVE_CLEARED
ROOM_OCCUPIED
ROOM_CLEARED
MODE_CHANGE_REQUESTED
MODE_CHANGED
LIGHT_COMMAND_REQUESTED
LIGHT_STATE_CONFIRMED
DEVICE_ONLINE
DEVICE_OFFLINE
SCHEDULE_TRIGGERED
TIMER_EXPIRED
COMMAND_SUCCEEDED
COMMAND_FAILED
```

Events are processed serially by the automation state machine to reduce race conditions.

Device I/O results return asynchronously through the internal event bus.

---

## 15. Concurrency and I/O Model

Recommended implementation:

- Main `asyncio` event loop for MQTT, scheduler, tools, and state-machine events.
- One dedicated TinyTuya worker thread or small executor for blocking sockets.
- Bounded command queue to prevent unlimited command accumulation.
- Per-device command serialization.
- Coalescing of redundant commands.

Example:

```text
set_brightness(40)
set_brightness(50)
set_brightness(70)
```

If these arrive before execution, the queue may coalesce them into the latest safe value.

Command deduplication must prevent repeated identical on/off operations caused by duplicate MQTT or sensor events.

---

## 16. Error Handling

### 16.1 Device Timeout

- Mark the command failed.
- Increment consecutive failure count.
- Retry only within configured limits.
- Do not block other devices.
- Publish a health-state change after the threshold is reached.

### 16.2 MQTT Disconnection

- Continue local Tuya schedules and commands that do not require ESPresense.
- Mark BLE identity as stale after its timeout.
- Reconnect with exponential backoff.
- Re-subscribe after reconnection.

### 16.3 HE20 Offline

- Presence fusion may use BLE identity alone.
- Light-off decisions become more conservative.
- Do not declare the room empty immediately.

### 16.4 ESP32 Offline

- mmWave can still report anonymous occupancy.
- The runtime must not claim Shereef is present without BLE evidence.

### 16.5 Bulb Offline

- Mode changes may update desired state but must mark actual state unconfirmed.
- The runtime should retry after recovery.
- Marvi must receive a clear failure response rather than a false success.

### 16.6 Corrupt State Snapshot

- Keep the previous backup snapshot.
- Start with safe defaults if both snapshots are invalid.
- Log a high-severity recovery event.

---

## 17. Security

- MQTT binds only to the trusted LAN interface.
- MQTT authentication is mandatory.
- ACLs restrict topic access.
- Windows Firewall allows MQTT only from the local subnet and known ESP32 IP where practical.
- Tuya keys are never committed to Git.
- Logs never print local keys or passwords.
- State snapshots exclude secrets.
- Marvi tools validate all mode, color, brightness, and duration values.
- Remote access is not included in the first release.

---

## 18. Observability

### 18.1 Logs

Structured logs should include:

- Timestamp.
- Severity.
- Component.
- Event type.
- Request ID where applicable.
- Device name.
- Duration.
- Result or error code.

Example:

```json
{
  "timestamp": "2026-07-14T18:30:01.112+03:00",
  "level": "INFO",
  "component": "tuya.worker",
  "event": "command_completed",
  "request_id": "cmd-20260714-000123",
  "device": "bulb",
  "action": "set_scene",
  "duration_ms": 284,
  "status": "success"
}
```

### 18.2 Health State

The runtime exposes:

- Overall service status.
- MQTT connection state.
- ESP32 freshness.
- Bulb online state.
- HE20 online state.
- Queue depth.
- Consecutive failures.
- Last successful poll.
- Last successful command.

### 18.3 Diagnostic Command

`smart_room.run_diagnostic` should:

1. Verify configuration.
2. Test MQTT connectivity.
3. Check ESP32 event freshness.
4. Query bulb status.
5. Query HE20 status.
6. Validate scene mappings without changing the light unless explicitly requested.
7. Return a concise report to Marvi.

---

## 19. Phase 0 — Hardware and Protocol Validation

No automation engine should be implemented before these checks pass.

### 19.1 Tuya Discovery

For each Tuya device, record:

- Device ID.
- Local IP.
- Local key.
- Protocol version.
- Product name.
- All observed DPS fields.
- Which fields are readable.
- Which fields are writable.
- Value ranges and formats.

### 19.2 Bulb Capability Test

Verify independently:

- On/off.
- Brightness range.
- Color-temperature range.
- RGB encoding.
- Scene support.
- Transition behavior.
- Status polling.
- Recovery after power loss.

### 19.3 HE20 Capability Test

Verify independently:

- Occupancy DPS.
- No-presence transition delay.
- Sensitivity control, if available.
- LED control, if available.
- Poll latency.
- Whether local status changes are event-driven or require polling.
- Reliability for at least one continuous hour.

### 19.4 ESPresense Test

Verify:

- Correct Wi-Fi connection.
- MQTT authentication.
- Stable secure identity for the iPhone.
- Exact MQTT topic.
- Exact payload fields.
- RSSI while at the desk, bed, door, and outside the room.
- False detection from adjacent spaces.
- Time required to enter and exit presence.

### 19.5 Exit Criteria

Phase 0 is complete only when:

- Both Tuya devices can be queried locally.
- The bulb can be controlled reliably.
- HE20 occupancy can be read reliably or a fallback plan is documented.
- The iPhone has a stable ESPresense identity.
- Real MQTT samples are saved as test fixtures.
- Initial presence thresholds are based on measurements, not guesses.

---

## 20. Implementation Phases

### Phase 1 — Infrastructure

- Install and secure Mosquitto.
- Reserve device IP addresses.
- Create the Python project.
- Add typed configuration and secret loading.
- Add structured logging.

### Phase 2 — Device Adapters

- Implement TinyTuya worker.
- Implement bulb adapter.
- Implement HE20 adapter.
- Implement MQTT client.
- Implement ESPresense parser using saved fixtures.

### Phase 3 — State and Presence

- Implement runtime models.
- Implement atomic state store.
- Implement BLE state machine.
- Implement mmWave state machine.
- Implement presence fusion.
- Add unit tests for entry, exit, stale data, and conflicting signals.

### Phase 4 — Modes and Automations

- Implement authoritative mode state machine.
- Implement scenes.
- Implement adaptive light-on and light-off.
- Implement Sleep and Alarm.
- Implement daily reset.
- Implement Work Return and Evening Sleep.
- Implement manual override.

### Phase 5 — Marvi Integration

- Add typed Marvi tools.
- Add request IDs and acknowledgements.
- Add device-health tool.
- Add curated long-term context events.
- Confirm that raw transient state does not pollute Marvi memory.

### Phase 6 — Production Hardening

- Install as a Windows service.
- Configure automatic restart.
- Test restart recovery.
- Test Wi-Fi loss and recovery.
- Test MQTT loss and recovery.
- Test each device offline.
- Run for at least seven days alongside Home Assistant.
- Decommission Home Assistant only after stable parallel operation.

---

## 21. Testing Strategy

### 21.1 Unit Tests

- Mode transition legality.
- Automation priority.
- BLE entry and exit debounce.
- mmWave clear delay.
- Presence fusion truth table.
- Daily reset behavior.
- Work-return cancellation.
- Evening-sleep cancellation.
- Command validation.
- State snapshot migration.

### 21.2 Integration Tests

- MQTT fixture to presence state.
- Marvi command to device queue.
- Device result to command acknowledgement.
- Scheduler trigger to mode change.
- Restart with pending alarm or sleep timer.

### 21.3 Hardware Tests

- Fifty sequential bulb commands without unrecovered failure.
- Presence entry from outside the room.
- Presence clear after leaving the room.
- Remaining still in bed while mmWave is active.
- Phone present while Shereef is outside the room.
- HE20 offline.
- ESP32 offline.
- Bulb power-cycle recovery.
- Router restart recovery.

### 21.4 Soak Test

Run the system continuously for seven days and track:

- Service crashes.
- MQTT reconnects.
- Device timeouts.
- False light-on events.
- False light-off events.
- Average command latency.
- Maximum queue depth.
- Memory growth.

---

## 22. Acceptance Criteria

The first release is accepted when all conditions are met:

1. The runtime starts automatically after Windows boot.
2. Idle memory remains within an agreed lightweight target.
3. Marvi can read the complete room state.
4. Marvi can turn the light on and off with a confirmed result.
5. Marvi can activate every supported mode.
6. Sleep mode blocks presence-triggered lighting.
7. Alarm mode ends automatically and restores a valid state.
8. Light-off requires both BLE absence and mmWave clear.
9. No conflicting active modes can exist.
10. No Tuya command blocks MQTT processing.
11. Device failures produce explicit error responses.
12. Secrets never appear in logs or state files.
13. The service recovers after MQTT, router, ESP32, and Tuya device interruptions.
14. The seven-day soak test completes without an unrecovered crash.

---

## 23. Migration from Home Assistant

### 23.1 Parallel Migration

Home Assistant remains available while the new engine is tested.

Recommended order:

1. Export current automation behavior and scene values.
2. Disable only one matching Home Assistant automation at a time.
3. Enable its Smart Room Engine equivalent.
4. Observe behavior for at least one day.
5. Continue until all required automations are migrated.

### 23.2 Preventing Double Control

During parallel operation:

- Only one platform may own a specific automation.
- Avoid both systems responding to the same presence event.
- Log every state-changing command with its source.

### 23.3 Decommission Criteria

Home Assistant and Docker may be removed after:

- All acceptance criteria pass.
- The seven-day soak test passes.
- No required device or automation depends on Home Assistant.
- A rollback copy of the Home Assistant configuration exists.

---

## 24. Future Extensions

After version 0.1 is stable:

- Additional rooms and ESP32 receivers.
- Local mobile location adapter.
- Temperature and humidity sensors.
- Door and window sensors.
- Energy monitoring.
- Local FastAPI dashboard.
- Tailscale remote access.
- Spotify scene integration.
- Proactive but user-configurable Marvi suggestions.
- Carefully curated behavioral learning.

Every extension must preserve the core principle that current sensor state is runtime context, not automatically long-term memory.

---

## 25. Decisions Locked by This Revision

1. The project remains a custom native Python engine.
2. MQTT is used for ESPresense transport, not as Marvi's primary user-facing control API.
3. Mosquitto must be reachable on the LAN and protected by authentication and ACLs.
4. The iPhone must use stable secure enrollment.
5. GPS is not required for version 0.1.
6. Work Return uses BLE arrival during a configured time window.
7. Modes are represented by one `active_mode` enum.
8. TinyTuya runs behind a dedicated worker queue.
9. Commands use request IDs and structured acknowledgements.
10. Alarm flashing is bounded and slower than 500 ms continuous flashing.
11. Runtime state is separate from Marvi memory and Honcho.
12. Secrets are stored outside configuration and state snapshots.
13. Hardware capability validation is mandatory before implementation.
14. Home Assistant is removed only after parallel testing succeeds.

---

## 26. Remaining Decisions After Phase 0

These cannot be locked until real device behavior is measured:

- Exact Tuya protocol version for each device.
- Exact DPS mappings.
- HE20 local update reliability.
- Final ESPresense topic and payload parser.
- BLE entry and exit thresholds.
- Whether the bulb supports useful native scenes.
- Whether transitions are implemented by the bulb or emulated by the runtime.
- Final polling intervals.
- Target command latency and idle memory limits.

---

## 27. Recommended First Deliverable

The first code milestone should not include all automations.

It should provide:

1. Secure MQTT connection.
2. Live iPhone BLE state.
3. Live HE20 occupancy state.
4. Live bulb state.
5. A fused room-presence snapshot.
6. `smart_room.get_state`.
7. `smart_room.set_light`.
8. Structured logs and health output.

Once this foundation is stable, modes and automations can be added without rebuilding the architecture.

---

## 28. Reference Foundations

- Original uploaded specification: `smart-room-spec.md`, version 0.1.
- Eclipse Mosquitto for the local MQTT broker.
- ESPresense for BLE room-presence detection.
- TinyTuya for local Tuya LAN communication.
- Python `asyncio` for orchestration and an isolated worker for blocking device I/O.
