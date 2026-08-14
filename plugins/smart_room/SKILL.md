---
name: smart_room
description: "Control and monitor the smart room — lights, presence, modes, automations."
version: 0.7.0
author: xRetro Labs
---

# Smart Room Skill

## When to use

Use smart_room tools when the user asks about:
- Room state ("am I home?", "is the light on?", "what mode is the room in?")
- Light control ("turn on lights", "dim to 40%", "reading mode")
- Mode changes ("sleep mode", "focus mode", "alarm off")
- Presence ("did I leave?", "when did I get home?")
- Device health ("is the bulb online?", "is ESP32 connected?")
- Diagnostics ("why is the light not working?", "run diagnostics")
- Vision ("look at the room", "am I in bed?", "who was here?", "review this face")

## Tools

| Tool | What it does |
|------|-------------|
| `smart_room_state` | Full room snapshot — presence, light, modes, devices, location |
| `smart_room_set_mode` | Set mode: normal, reading, focus, relax, night, sleep, alarm, off |
| `smart_room_set_light` | Direct light control — on/off, brightness, color temp, RGB |
| `smart_room_cancel_sleep` | Cancel sleep mode and restore previous state |
| `smart_room_override` | Automatic presence, hold light on, or hold light off |
| `smart_room_alarm` | Manage named one-day/daily alarms and acknowledge wake-up |
| `smart_room_health` | Device health check — online/offline status, last seen |
| `smart_room_diagnostic` | Full diagnostic dump for troubleshooting |
| `smart_room_observe` | Fresh camera truth: visibility, people, reviewed identity, zone, posture, sleep and gesture |
| `smart_room_vision_history` | Bounded local visual/cognition evidence |
| `smart_room_faces` | Explicitly review/reject/delete face identities |

## Context

The smart room plugin provides ambient world-context to Marvi:
- One compact context line injected at session start
- Subconscious diff on meaningful transitions (mode changes, arrivals/departures)
- The runtime NEVER writes memory — the subconscious proposes, runtime supplies raw events
- Vision and Smart Room cognition are operational truth. The restricted cognition model may use only room observe/light/mode/speak/silent/recheck/history tools.

## Fusion Model

Presence is determined by fusing three signals:
1. **BLE** (ESP32/ESPresense) — strong evidence FOR identity, weak evidence AGAINST
2. **mmWave** (HE20 sensor) — room occupancy, no identity
3. **Geofence** (OwnTracks over authenticated MQTT) — home/away/work/uni zones

Key axiom: iPhone BLE silence is NOT absence (deep-sleep). Identity is sticky:
once BLE establishes presence, mmWave occupancy holds it even if BLE goes silent.
Confidence decays over time (0.95 → floor 0.6 over 2h).

## Automations

The runtime provides:
- Adaptive light on/off based on presence
- Sleep mode with darkness enforcement
- Alarm mode with bright flash (configurable time, default disabled)
- Reading/Focus/Relax scene modes
- Work return sleep (geofence + time window)
- Evening sleep schedule
- Daily mode flag reset

## Troubleshooting

### Runtime down (port 17842 refused)
Symptom: `smart_room_*` tools fail with `"smart_room runtime is not running on port 17842"` (`DEVICE_TIMEOUT`).

Root cause usually: the machine was off overnight / the gateway booted but the `on_gateway_start` supervisor did not spawn the runtime (observed 2026-08-10: plugin loaded, routes mounted, but no runtime process and no supervisor restart warnings).

Recovery (start it the same way the supervisor does):
```bash
cd D:\hermes-agent
venv/Scripts/python.exe -c "from plugins.smart_room import process_manager as p; p.start()"
```
- `p.start()` defaults `rpc_port` to 17842 and pulls MQTT/Tuya credentials from the profile `.env` at spawn time.
- The spawned venv runtime re-execs itself under the managed cpython (`.hermes-runtime`) — you'll see a launcher parent (~8 MB) plus the real child holding the port. That's normal; do NOT kill the launcher.
- Verify: `netstat -ano | grep 17842` shows LISTENING, then call `smart_room_health`.
- If the runtime was instead killed while the supervisor is alive, the supervisor auto-restarts it within ~30 s — no manual step needed.
- Missing deps (`runtime.log`: "tinytuya not installed — Tuya disabled", MQTT down): `pip install` tinytuya/paho-mqtt/pycryptodome into `hermes-agent/venv`, then `p.start()` again (the supervisor re-checks deps at every start).

### Tuya device "offline" while the device is actually healthy (wedged session)
Symptom: one device (typically `tuya_he20`) shows `online: false` with a high, climbing `consecutive_failures` (hundreds), `last_success` hours old, and every poll returns `code: DEVICE_BUSY` / "command already in progress". The runtime self-heal loop keeps firing ("Tuya self-heal recreated 'he20' connection … attempt=N") but never recovers.

Root cause: a socket thread in the runtime's `TuyaController` hung while holding the per-device lock (`_locks[name]`). `refresh()` pops and recreates the tinytuya device object, but can't clear the held lock, so every subsequent `_run()` returns DEVICE_BUSY before even submitting. Observed 2026-08-11: HE20 stuck 26h at 868 consecutive failures while a fresh tinytuya probe answered in 0.3 s with valid DPS.

Diagnose before restarting (don't trust the health flag alone):
```bash
cd D:\hermes-agent
.venv/Scripts/python.exe plugins/smart_room/scripts/probe_tuya.py   # fresh direct status() for HE20 + bulb
```
- Probe succeeds → device fine, runtime session wedged → recover via supervised restart (RPC shutdown, then `p.start()` — the supervisor may NOT auto-respawn on shutdown, same as the case above):
```bash
# 1) RPC shutdown (token lives at <hermes home>\smart_room\.rpc-token)
.venv/Scripts/python.exe -c "import socket,json;tok=open(r'<HERMES_HOME>\smart_room\.rpc-token').read().strip();s=socket.create_connection(('127.0.0.1',17842),timeout=5);s.sendall((json.dumps({'jsonrpc':'2.0','id':'x','auth':tok,'method':'shutdown','params':{}})+'\n').encode());print(s.recv(8192).decode());s.close()"
# 2) wait ~10 s, then respawn:
.venv/Scripts/python.exe -c "from plugins.smart_room import process_manager as p; p.start()"
```
- Probe also fails / no ARP entry / no ping → device itself is off the network (power/WiFi); needs physical power cycle — software restart won't help. Verify with `arp -a` (no MAC entry = truly absent) and `ping`.

Verify: `smart_room_health` shows `tuya_he20: online: true`, `consecutive_failures: 0`, and `smart_room_state` mmwave updates again.

## Device Setup

### Tuya devices (one-time key extraction)
1. Run `python -m tinytuya scan` to discover devices on local network
2. Get local keys from Tuya IoT Portal (free account, one-time)
3. Save the keys in Desktop Settings → Smart Room; they are stored as secrets, not in config.yaml
4. After keys are set, all control is LAN-only — no cloud dependency

### ESP32 (ESPresense)
1. Flash ESPresense via https://espresense.com/install
2. Configure via web UI: Wi-Fi, MQTT broker IP, room name "smart_room"
3. Enroll iPhone IRK for stable identity tracking
4. Copy the enrolled device ID into Desktop Settings → Smart Room → Owner Device ID

### iPhone location
1. Configure OwnTracks in MQTT mode with the Smart Room broker credentials
2. Set the device/user topic to match `smart_room.owntracks.topic`
3. Create the `home` region and enable background location with Always permission
4. Use a private VPN such as Tailscale for reliable transitions away from home; never expose plaintext MQTT port 1883 to the internet
