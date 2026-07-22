#!/usr/bin/env python3
"""Prepare a Marvi branch that merges Hermes upstream underneath.

This is intentionally a maintainer/dev tool, not a user updater. User installs
should update from xRetr00/Marvi only. Maintainers use this script to bring
NousResearch/hermes-agent changes into a review branch, then resolve conflicts,
run brand verification, run tests, and merge into Marvi main.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
DEFAULT_REMOTE = "hermes-upstream"
DEFAULT_REPORT = ".git/marvi-upstream-sync-report.md"

PROTECTED_PREFIXES = (
    "AGENTS.md",
    "skills/autonomous-ai-agents/hermes-agent/",
    "website/docs/developer-guide/contributing.md",
    "agent/learning/",
    "agent/memory/",
    "agent/goal_store.py",
    "apps/bootstrap-installer/",
    "apps/desktop/",
    "cron/",
    "cron/scripts/subconscious/",
    "docs/superpowers/specs/",
    "gateway/world_trigger.py",
    "gateway/flow_gate.py",
    "gateway/idle_trigger.py",
    "hermes_cli/web_server.py",
    "hermes_cli/config.py",
    "plugins/smart_room/",
    "tools/brain/",
    "tools/brain_",
    "tools/episodic_tool.py",
    "tools/goal_tools.py",
    "tools/tts_tool.py",
    "tools/voice_",
)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], check=check)


def stdout(*args: str, check: bool = True) -> str:
    return git(*args, check=check).stdout.strip()


def ensure_clean_tree() -> None:
    status = stdout("status", "--porcelain")
    if status:
        print("Working tree is not clean. Commit or stash changes before syncing.", file=sys.stderr)
        print(status, file=sys.stderr)
        raise SystemExit(2)


def ensure_remote(name: str, url: str) -> None:
    current = stdout("remote", "get-url", name, check=False)
    if current:
        if current != url:
            print(f"Remote {name!r} exists with different URL: {current}", file=sys.stderr)
            print(f"Expected: {url}", file=sys.stderr)
            raise SystemExit(2)
        return
    print(f"Adding {name} remote: {url}")
    git("remote", "add", name, url)


def local_branch_exists(branch: str) -> bool:
    return git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def is_protected(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def write_review_report(base_ref: str, upstream_ref: str, report_path: Path) -> tuple[int, int]:
    rows = stdout("diff", "--name-status", "--find-renames", f"{base_ref}...{upstream_ref}").splitlines()
    protected = [row for row in rows if row and is_protected(row.split("\t")[-1])]
    deleted = [row for row in rows if row.startswith("D\t")]
    commits = stdout("log", "--oneline", f"{base_ref}..{upstream_ref}").splitlines()
    lines = [
        "# Marvi upstream sync review",
        "",
        f"- Base: `{base_ref}` (`{stdout('rev-parse', base_ref)}`)",
        f"- Upstream: `{upstream_ref}` (`{stdout('rev-parse', upstream_ref)}`)",
        f"- Upstream commits not in base: {len(commits)}",
        f"- Upstream changed paths: {len(rows)}",
        f"- Protected-path overlaps: {len(protected)}",
        f"- Upstream deletions: {len(deleted)}",
        "",
        "## Protected-path overlap",
        "",
        *(f"- `{row}`" for row in protected),
        "",
        "## Upstream deletions",
        "",
        *(f"- `{row}`" for row in deleted),
        "",
        "## Upstream commits",
        "",
        *(f"- `{row}`" for row in commits),
        "",
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return len(protected), len(deleted)


def run_guard(script: str, label: str) -> int:
    result = run([sys.executable, script], check=False)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode:
        print(f"{label} failed. The merge remains uncommitted for repair.", file=sys.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a Marvi upstream sync branch")
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    parser.add_argument("--upstream-remote", default=DEFAULT_REMOTE)
    parser.add_argument("--upstream-branch", default="main")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--branch", default="")
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--review-only", action="store_true", help="Fetch and report without creating a branch")
    parser.add_argument("--push", action="store_true", help="Push the prepared branch to origin")
    args = parser.parse_args()

    ensure_remote(args.upstream_remote, args.upstream_url)

    print(f"Fetching origin/{args.base_branch} and {args.upstream_remote}/{args.upstream_branch}...")
    git("fetch", "origin", args.base_branch)
    git("fetch", args.upstream_remote, args.upstream_branch)

    upstream_ref = f"{args.upstream_remote}/{args.upstream_branch}"
    base_ref = f"origin/{args.base_branch}"
    upstream_sha = stdout("rev-parse", upstream_ref)
    base_sha = stdout("rev-parse", base_ref)
    current_sha = stdout("rev-parse", "HEAD")

    behind = stdout("rev-list", "--count", f"{base_ref}..{upstream_ref}", check=False) or "0"
    ahead = stdout("rev-list", "--count", f"{upstream_ref}..{base_ref}", check=False) or "0"

    print(f"Marvi base:      {base_sha[:12]} ({base_ref})")
    print(f"Hermes upstream: {upstream_sha[:12]} ({upstream_ref})")
    print(f"Range: Marvi has {ahead} commit(s) not in Hermes; Hermes has {behind} commit(s) not in Marvi.")

    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    protected_count, deletion_count = write_review_report(base_ref, upstream_ref, report_path)
    print(f"Review report: {report_path}")
    print(f"Protected overlap: {protected_count} path(s); upstream deletions: {deletion_count} path(s).")

    if behind == "0":
        print("No upstream sync needed: Marvi main already contains the upstream ref.")
        return 0

    if args.review_only:
        print("Review-only mode complete; no branch or merge was created.")
        return 0

    ensure_clean_tree()

    date = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    branch = args.branch or f"sync/hermes-upstream-{date}-{upstream_sha[:8]}"

    if local_branch_exists(branch):
        print(f"Refusing to overwrite existing branch {branch}. Choose --branch with a new name.", file=sys.stderr)
        return 2

    print(f"Creating sync branch {branch} from {base_ref}...")
    git("switch", "--create", branch, base_ref)

    if run_guard("scripts/verify_marvi_upstream_contract.py", "Pre-merge Marvi contract guard"):
        return 1

    print(f"Merging {upstream_ref} into {branch}...")
    merge = git(
        "merge",
        "--no-ff",
        "--no-commit",
        upstream_ref,
        check=False,
    )
    if merge.returncode != 0:
        print("Merge stopped with conflicts. Resolve them, keep Marvi branding, then run:")
        print("  python scripts/verify_marvi_upstream_contract.py")
        print("  python scripts/verify_marvi_brand.py")
        print(f"  review {report_path}")
        print("  git status")
        print("  git commit")
        return 1

    if run_guard("scripts/verify_marvi_upstream_contract.py", "Marvi contract guard"):
        return 1
    if run_guard("scripts/verify_marvi_brand.py", "Marvi brand guard"):
        return 1

    print("Guards passed; creating the reviewed merge commit.")
    git("commit", "--no-edit")

    if args.push:
        print(f"Pushing {branch} to origin...")
        git("push", "-u", "origin", branch)
        print(f"Pushed {branch}. Open a PR into {args.base_branch} after tests pass.")
    else:
        print(f"Prepared local branch {branch}. Run tests, then push/open a PR.")

    print(f"Return to previous HEAD manually if needed. Previous HEAD was {current_sha[:12]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
