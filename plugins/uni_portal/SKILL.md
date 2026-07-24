---
name: uni_portal
description: Check the user's Duzce University student system (grades, announcements, schedule) and tell them proactively when something changes. Use when the user asks Marvi to watch their student portal, check grades, or after they run `hermes uni login`.
version: 1.0.0
metadata:
  hermes:
    tags: [university, duzce, browser-automation, autonomy]
---

# uni_portal

## What this is

A daily, browser-automated check of the user's own Duzce University student
system — grades, announcements, class schedule — with a proactive message
only when something actually changed since the last check. Part of the
Marvi freedom/autonomy spec (Part 1, §1.3): Marvi visiting an account the
user explicitly asked it to watch, on their own machine, with their own
credentials.

## Security / credential boundary — read this before touching this plugin

- **Credentials are never stored in `config.yaml`, in code, or in any file
  Marvi's other memory/logging paths touch.** They live exclusively in the
  Windows Credential Manager, written and read through
  `plugins/uni_portal/credentials.py`'s thin `ctypes` wrapper around
  `Advapi32.dll` (`CredWrite`/`CredRead`/`CredDelete`) — the same class of
  OS-native secret store other Hermes credential flows use, not a bespoke
  encrypted file.
- **Enrollment is interactive and CLI-only.** The user runs `hermes uni
  login`, types their username/password directly into the terminal prompt,
  and `hermes_cli/subcommands/uni.py` stores it immediately. Marvi (the
  agent/LLM) never sees the password — it isn't in any prompt, tool
  argument, or model context at any point.
- **The plaintext password is read from the credential store only inside
  `plugins/uni_portal/portal.py::login()`**, at the moment it's typed into
  the portal's login form, and the local Python reference is dropped
  immediately after. It is never logged, never written to a snapshot, and
  never included in the proactive notification message or the episodic/graph
  records `plugins/uni_portal/check.py` creates.
- **2FA and CAPTCHA are a hard stop, never a bypass target.** If the portal
  shows either after a login attempt, `portal.py::login()` raises
  `LoginBlocked` and `check.py` routes straight to the ask-user channel
  (`agent.autonomy.ask.ask_user`) asking the human to handle it. There is no
  code path that attempts to solve or circumvent either.
- **No raw portal transcripts are kept.** Only the diffed snapshot
  (`HERMES_HOME/uni_portal/snapshot.json` — course/grade pairs,
  announcement title+date, schedule rows) persists between runs; the actual
  page HTML/accessibility-tree snapshots used during a single check are
  never written to disk.

## Enrollment

```
hermes uni login              # interactive: prompts for username + password,
                               # stores to the OS credential store, enables
                               # uni_portal.enabled and schedules the daily job
hermes uni status              # shows whether credentials are stored + last check result
hermes uni check                # runs one check immediately (same as the daily job)
hermes uni login --logout      # deletes stored credentials, disables the daily job
```

## Configuration (`config.yaml`, not user-facing in the Mind UI today)

```yaml
uni_portal:
  enabled: false                 # flipped true by `hermes uni login`
  check_schedule: "0 18 * * *"   # daily at 18:00 local
  portal_url: ""                 # the login page URL -- fill in for your Duzce portal
  grades_path: ""                # grades page URL, once logged in
  announcements_path: ""         # announcements page URL
  schedule_path: ""              # class schedule page URL
```

`portal_url`/`grades_path`/`announcements_path`/`schedule_path` are
deliberately not hardcoded — Duzce's actual portal URLs need to be filled in
against the real site during enrollment (the login-field/submit-button
detection in `portal.py` is a best-effort keyword heuristic over the page's
accessibility tree, not portal-specific selectors, so it also benefits from
being pointed at the real login page during setup rather than guessed).

## What still needs live validation

The control flow (login → collect → diff → notify → save snapshot,
2FA/CAPTCHA stop-and-ask, credential-store round-trip) is unit-tested with
fakes in `tests/plugins/uni_portal/`. The actual DOM interaction against the
live Duzce portal — whether the username/password/submit-button keyword
heuristics in `portal.py` correctly find the right elements, and whether the
generic table-row scraper in `collect_grades`/`collect_announcements`
extracts the right columns — has not been (and cannot be, without a real
account) exercised end-to-end. Expect to tune the `*_path` config values and
possibly the heuristic hint tuples in `portal.py` after first live
enrollment.
