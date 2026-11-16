"""Session rules for the contract-tests check (Platform Reliability, ServiceBoundary):

* A skipped or xfailed test is reported as a FAILURE: skips do not count toward coverage.
* A test that passes without a single passing assert is reported as a FAILURE.
  (Uses pytest's assertion-pass hook, enabled in pytest.ini.)
* When the whole contract suite runs and passes, every documented (path, method, status) in
  contracts/*.yaml except 500 must have been exercised, otherwise the session fails.
* "No tests collected" already fails the run (pytest exit code 5).
"""
from __future__ import annotations

import pytest

ASSERT_COUNT = pytest.StashKey[int]()


def pytest_runtest_setup(item):
    item.stash[ASSERT_COUNT] = 0


def pytest_assertion_pass(item, lineno, orig, expl):
    item.stash[ASSERT_COUNT] = item.stash.get(ASSERT_COUNT, 0) + 1


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if hasattr(report, "wasxfail"):
        report.outcome = "failed"
        report.longrepr = f"xfail is not allowed in contract tests ({report.wasxfail})"
        del report.wasxfail
    elif report.skipped:
        report.outcome = "failed"
        report.longrepr = f"skipped tests are not allowed in contract tests: {report.longrepr}"
    elif report.when == "call" and report.passed and item.stash.get(ASSERT_COUNT, 0) == 0:
        report.outcome = "failed"
        report.longrepr = "test passed without a single passing assert; tests must assert something real"


def _ran_full_contract_suite(session) -> bool:
    opt = session.config.option
    if opt.collectonly:
        return False  # nothing executed, so nothing can have been exercised
    if opt.keyword or opt.markexpr or opt.lf or getattr(opt, "deselect", None):
        return False
    contract_items = [i for i in session.items if "tests/contract/" in i.nodeid]
    return bool(contract_items) and session.testscollected == len(session.items)


def pytest_sessionfinish(session, exitstatus):
    if exitstatus != 0 or not _ran_full_contract_suite(session):
        return
    from tests.contract.openapi_check import COVERED, documented_responses

    files = {i.nodeid.split("::")[0] for i in session.items}
    expected = {
        "tests/contract/test_stock_contract.py", "tests/contract/test_order_contract.py",
        "tests/contract/test_gateway_contract.py",
    }
    if not expected <= files:
        return
    missing = sorted(r for r in documented_responses() - COVERED if r[3] != "500")
    if missing:
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        tr.write_sep("=", "documented responses never exercised by a contract test", red=True)
        for name, path, method, status in missing:
            tr.write_line(f"  {name}: {method.upper()} {path} -> {status}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
