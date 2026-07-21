"""Implementation for ``hermes brain``."""

from __future__ import annotations

import json


def brain_command(args) -> int:
    from hermes_cli.config import load_config, save_config
    from tools.brain.indexer import (
        DEFAULT_SCHEDULE,
        brain_status,
        ensure_index_job,
        index_configured_folders,
    )
    from tools.brain.store import BrainStore

    command = getattr(args, "brain_command", None) or "status"
    cfg = load_config()
    section = dict(cfg.get("brain") or {})
    if command == "enable":
        section["enabled"] = True
        section["folders"] = list(dict.fromkeys([*section.get("folders", []), *args.folders]))
        section.setdefault("schedule", DEFAULT_SCHEDULE)
        cfg["brain"] = section
        ensure_index_job(cfg)
        save_config(cfg)
        print("Brain enabled. Run `hermes brain index` to index now.")
        return 0
    if command == "disable":
        section["enabled"] = False
        cfg["brain"] = section
        if section.get("job_id"):
            from cron.jobs import pause_job

            pause_job(section["job_id"], reason="Brain disabled")
        save_config(cfg)
        print("Brain disabled.")
        return 0
    if command == "index":
        print(json.dumps(index_configured_folders(), indent=2))
        return 0
    if command == "search":
        store = BrainStore()
        try:
            print(json.dumps(store.search(args.query, args.limit), indent=2, ensure_ascii=False))
        finally:
            store.close()
        return 0
    # Default ("status"): the full aggregator -- config + index stats +
    # last-run/discovery/collect info (auto_folders, discovered_folders,
    # collected counts per source, last_discovery, last_collect). Additive
    # over the plain brain_config()+store.status() this used to inline.
    print(json.dumps(brain_status(cfg), indent=2, ensure_ascii=False))
    return 0
