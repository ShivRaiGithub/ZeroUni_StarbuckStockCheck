#!/usr/bin/env bash
# Stop hook: before a turn ends, run the linter and the contract tests (CLAUDE.md rule 3:
# no merge until the contract tests pass). On failure, exit 2 so Claude is told why and keeps
# working instead of stopping. If this hook already blocked once in this stop cycle
# (stop_hook_active), let the turn end so a failure that can't be fixed never loops forever.
set -uo pipefail

input="$(cat)"
if printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi

lint_out="$("$PY" -m ruff check . --output-format concise 2>&1)"; lint_rc=$?
test_out="$("$PY" -m pytest -q -p no:randomly 2>&1)"; test_rc=$?

if [ $lint_rc -eq 0 ] && [ $test_rc -eq 0 ]; then
  summary="$(printf '%s\n' "$test_out" | tail -1)"
  printf '{"systemMessage": "Stop checks passed: ruff clean; contract tests %s"}\n' "${summary//\"/\'}"
  exit 0
fi

{
  echo "Stop checks FAILED. Fix these before ending the turn."
  if [ $lint_rc -ne 0 ]; then
    echo "--- ruff check (exit $lint_rc) ---"
    printf '%s\n' "$lint_out" | tail -30
  fi
  if [ $test_rc -ne 0 ]; then
    echo "--- pytest (exit $test_rc) ---"
    printf '%s\n' "$test_out" | grep -E '^(FAILED|ERROR)' | head -30
    printf '%s\n' "$test_out" | tail -1
  fi
} >&2
exit 2
