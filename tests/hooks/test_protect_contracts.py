"""PreToolUse hook .claude/hooks/protect-contracts.py: what it refuses and what it lets through.

The hook is run exactly as Claude Code runs it: the tool call as JSON on stdin, a decision as JSON
on stdout. Protected: the OpenAPI contracts and CLAUDE.md. A shell command is judged per segment,
so reading a protected file stays allowed.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "protect-contracts.py"
RULES = "CLAUDE" + ".md"  # the project rules file
ORDER_SPEC = "contracts/order-service.yaml"


def decide(tool: str, **tool_input) -> tuple[str, str]:
    """Returns (decision, reason). decision is "deny" or "allow" (the hook printed nothing)."""
    run = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                         capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)})
    assert run.returncode == 0, run.stderr
    if not run.stdout.strip():
        return "allow", ""
    out = json.loads(run.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out["permissionDecision"], out["permissionDecisionReason"]


# ---------------------------------------------------------------- the rules file: file tools

@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize("path", [RULES, str(ROOT / RULES)], ids=["relative", "absolute"])
def test_file_tools_are_refused_on_the_rules_file(tool, path):
    decision, reason = decide(tool, file_path=path)
    assert decision == "deny"
    assert RULES in reason and "project rules" in reason


def test_a_same_named_file_elsewhere_is_not_the_rules_file():
    assert decide("Edit", file_path="docs/OTHER-" + RULES)[0] == "allow"
    assert decide("Edit", file_path="services/order/app/flow.py")[0] == "allow"


# ---------------------------------------------------------------- the rules file: shell writes

@pytest.mark.parametrize("command", [
    f"echo x >> {RULES}",
    f"echo x > {RULES}",
    f"sed -i s/a/b/ {RULES}",
    f"cp /tmp/other {RULES}",
    f"mv /tmp/other {RULES}",
    f"rm {RULES}",
    f"git checkout -- {RULES}",
    f"printf x | tee {RULES}",
    f"python3 -c \"import pathlib; pathlib.Path('{RULES}').write_text('')\"",
    f"ls && echo x >> {RULES}",
])
def test_shell_writes_to_the_rules_file_are_refused(command):
    decision, reason = decide("Bash", command=command)
    assert decision == "deny"
    assert RULES in reason and "shell command would modify it" in reason


# ---------------------------------------------------------------- the rules file: read-only use

@pytest.mark.parametrize("command", [
    f"cat {RULES}",
    f"cat {RULES} | head -5",
    f"grep -n store_closed {RULES}",
    f"grep -n rule {RULES} > /tmp/matches.txt",
    f"cp {RULES} /tmp/copy.md",
    f"wc -l {RULES}; ls",
    f"git diff -- {RULES}",
    "ls",
])
def test_read_only_shell_use_of_the_rules_file_is_allowed(command):
    assert decide("Bash", command=command) == ("allow", "")


# ---------------------------------------------------------------- the order spec (unchanged behaviour)

@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_file_tools_are_refused_on_the_order_spec(tool):
    decision, reason = decide(tool, file_path=ORDER_SPEC)
    assert decision == "deny"
    assert ORDER_SPEC in reason and "fixed OpenAPI contract" in reason


def test_shell_writes_to_the_order_spec_are_refused_and_reads_allowed():
    assert decide("Bash", command=f"sed -i s/a/b/ {ORDER_SPEC}")[0] == "deny"
    assert decide("Bash", command=f"echo x >> {ORDER_SPEC}")[0] == "deny"
    assert decide("Bash", command=f"cat {ORDER_SPEC}") == ("allow", "")
    assert decide("Bash", command=f"grep -n version {ORDER_SPEC} | head") == ("allow", "")
