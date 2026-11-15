#!/usr/bin/env python3
"""PreToolUse hook: the OpenAPI contracts are fixed (CLAUDE.md rule 1). Refuse any change to them.

Protected: contracts/*.yaml (the committed specs: stock-service, order-service, gateway) and the
superseded drafts under specs/*/contracts/. Covers the file tools (Edit, Write, MultiEdit,
NotebookEdit) and shell commands that would write, move or delete one of those files.
"""
import json
import os
import re
import sys
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]).resolve()
PROTECTED = re.compile(r"(^|/)(contracts/[^/]+\.ya?ml|specs/[^/]+/contracts/[^/]+)$")
SHELL_PATH = re.compile(r"(contracts/[\w.*-]+\.ya?ml|contracts/\*|specs/[\w.*-]+/contracts/)")
PATH_RE = SHELL_PATH.pattern
# Judged per shell segment (split on ; && || | and newlines), and only when that same segment
# names a contract path. Read-only uses (cat, grep, diff, cp FROM a contract) stay allowed.
SEGMENT_WRITES = [
    re.compile(r">{1,2}\s*['\"]?\S*" + PATH_RE),                       # redirect INTO a contract
    re.compile(r"\b(sed|perl)\b.*\s-i"),                               # in-place edit
    re.compile(r"\b(tee|mv|rm|truncate|patch|dd)\b"),
    re.compile(r"\bcp\b.*\s['\"]?\S*" + PATH_RE + r"['\"]?\s*$"),      # contract as cp destination
    re.compile(r"\bgit\s+(checkout|restore|apply|am|stash|rm|mv)\b"),
    re.compile(r"write_text|write_bytes|open\([^)]*['\"][wa+]"),
]
SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")


def shell_write_target(cmd: str) -> str | None:
    for seg in SEGMENT_SPLIT.split(cmd):
        hit = SHELL_PATH.search(seg)
        if hit and any(p.search(seg) for p in SEGMENT_WRITES):
            return hit.group(1)
    return None


def rel(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT / p
    try:
        return p.resolve().relative_to(PROJECT).as_posix()
    except ValueError:
        return p.as_posix()


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main() -> None:
    event = json.load(sys.stdin)
    tool, args = event.get("tool_name", ""), event.get("tool_input", {}) or {}
    msg = ("Blocked by protect-contracts hook: {} is a fixed OpenAPI contract (CLAUDE.md rule 1: every "
           "service's API contract is a fixed OpenAPI spec). Change the implementation to match the "
           "contract instead; a contract change needs the owning teams' sign-off outside this session.")
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        target = rel(args.get("file_path") or args.get("notebook_path") or "")
        if PROTECTED.search(target):
            deny(msg.format(target))
    elif tool == "Bash":
        target = shell_write_target(args.get("command", ""))
        if target:
            deny(msg.format(target) + " (shell command would modify it)")
    sys.exit(0)


if __name__ == "__main__":
    main()
