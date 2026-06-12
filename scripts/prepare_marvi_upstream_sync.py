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


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        text=True,
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


def ref_exists(ref: str) -> bool:
    return git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a Marvi upstream sync branch")
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    parser.add_argument("--upstream-remote", default=DEFAULT_REMOTE)
    parser.add_argument("--upstream-branch", default="main")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--branch", default="")
    parser.add_argument("--push", action="store_true", help="Push the prepared branch to origin")
    args = parser.parse_args()

    ensure_clean_tree()
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

    if upstream_sha == base_sha:
        print("No upstream sync needed: Marvi main already matches upstream ref.")
        return 0

    date = dt.datetime.utcnow().strftime("%Y%m%d")
    branch = args.branch or f"sync/hermes-upstream-{date}-{upstream_sha[:8]}"

    if ref_exists(branch):
        print(f"Checking out existing branch {branch}...")
        git("checkout", branch)
        git("reset", "--hard", base_ref)
    else:
        print(f"Creating sync branch {branch} from {base_ref}...")
        git("checkout", "-B", branch, base_ref)

    print(f"Merging {upstream_ref} into {branch}...")
    merge = git(
        "merge",
        "--no-ff",
        "--no-edit",
        upstream_ref,
        check=False,
    )
    if merge.returncode != 0:
        print("Merge stopped with conflicts. Resolve them, keep Marvi branding, then run:")
        print("  python scripts/verify_marvi_brand.py")
        print("  git status")
        print("  git commit")
        return 1

    print("Running Marvi brand guard...")
    guard = run([sys.executable, "scripts/verify_marvi_brand.py"], check=False)
    if guard.returncode != 0:
        print(guard.stdout)
        print(guard.stderr, file=sys.stderr)
        print("Brand guard failed. Fix visible branding before merging this sync branch.")
        return guard.returncode

    print("Brand guard passed.")

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
