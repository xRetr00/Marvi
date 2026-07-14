"""Implementation for ``hermes brain``."""

from __future__ import annotations

import json


def brain_command(args) -> int:
    from hermes_cli.config import load_config, save_config
    from tools.brain.indexer import brain_config, ensure_index_job, index_configured_folders
    from tools.brain.store import BrainStore

    command = getattr(args, "brain_command", None) or "status"
    cfg = load_config()
    section = dict(cfg.get("brain") or {})
    if command == "enable":
        section["enabled"] = True
        section["folders"] = list(dict.fromkeys([*section.get("folders", []), *args.folders]))
        section.setdefault("schedule", "every 30m")
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
    store = BrainStore()
    try:
        if command == "search":
            print(json.dumps(store.search(args.query, args.limit), indent=2, ensure_ascii=False))
        else:
            print(json.dumps({**brain_config(cfg), **store.status()}, indent=2))
    finally:
        store.close()
    return 0
