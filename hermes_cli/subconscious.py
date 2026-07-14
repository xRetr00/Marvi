"""``hermes subconscious`` command implementation.

Thin CLI surface over ``cron/subconscious.py`` — enable/disable/status for
the subconscious tick (one built-in cron job, no second engine). See the
2026-07-09-marvi-subconscious-presence design spec, Contract 3 for the
``subconscious.*`` config keys this drives.
"""

from __future__ import annotations

import sys

from hermes_cli.colors import Colors, color


def _print_status(info: dict) -> None:
    state = "enabled" if info.get("enabled") else "disabled"
    state_color = Colors.GREEN if info.get("enabled") else Colors.DIM
    print(color(f"Subconscious: {state}", state_color))
    print(f"  Interval:            every {info.get('interval')}")
    print(f"  Idle trigger:        {info.get('idle_trigger_minutes')}m of silence")
    tiers = info.get("tiers") or {}
    if tiers:
        tier_str = ", ".join(f"{k}={v}" for k, v in sorted(tiers.items()))
        print(f"  Category tiers:      {tier_str}")
    else:
        print("  Category tiers:      (none configured — everything defaults to 'propose')")
    job_id = info.get("job_id")
    if job_id:
        print(f"  Tick job:            {job_id} ({info.get('job_state') or 'unknown'})")
        if info.get("last_run_at"):
            print(f"  Last run:            {info.get('last_run_at')}")
        if info.get("next_run_at"):
            print(f"  Next run:            {info.get('next_run_at')}")
    else:
        print("  Tick job:            (none yet — run `hermes subconscious enable`)")
    reflection_id = info.get("reflection_job_id")
    if reflection_id:
        print(f"  Reflection job:      {reflection_id} ({info.get('reflection_job_state') or 'unknown'})")
        print(f"  Reflection schedule: {info.get('reflection_schedule')}")


def subconscious_command(args) -> int:
    """Handle ``hermes subconscious <enable|disable|status>``."""
    from cron.subconscious import disable, enable, status

    subcmd = getattr(args, "subconscious_command", None)

    if subcmd is None or subcmd == "status":
        _print_status(status())
        return 0

    if subcmd == "enable":
        interval = getattr(args, "interval", None)
        info = enable(interval=interval)
        print(color("Subconscious enabled.", Colors.GREEN))
        _print_status(info)
        return 0

    if subcmd == "disable":
        info = disable()
        print(color("Subconscious disabled.", Colors.DIM))
        _print_status(info)
        return 0

    print(f"Unknown subconscious command: {subcmd}", file=sys.stderr)
    print("Usage: hermes subconscious [enable|disable|status]", file=sys.stderr)
    return 1
