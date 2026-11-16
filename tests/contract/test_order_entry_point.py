"""Held test 2 of 2 for plan.md line 75 ("fail_saves never ships"): the code test.

The order image's entry point, create_app_from_env, must pass fail_saves=None and must never read
it from the environment. Checked two ways: by calling the entry point with the environment set
up as in production and capturing what it passes to create_app, and by reading the source: no
environment read anywhere in the order service names a FAIL key.
"""
import ast
from pathlib import Path

from tests.contract.harness import ROOT, load_service

ORDER_APP = ROOT / "services" / "order" / "app"


def test_entry_point_passes_fail_saves_none_and_never_reads_it_from_env(tmp_path, monkeypatch):
    main = load_service("order")
    for key in ("STOCK_URL", "PAYMENT_URL", "ORDER_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STOCK_URL", "http://stock:8000")
    monkeypatch.setenv("PAYMENT_URL", "http://payment:8000")
    monkeypatch.setenv("ORDER_DB_PATH", str(tmp_path / "order.db"))
    captured = {}
    monkeypatch.setattr(main, "create_app", lambda **kwargs: captured.update(kwargs) or "app")

    assert main.create_app_from_env() == "app"
    assert "fail_saves" in captured, "the entry point must pass fail_saves explicitly"
    assert captured["fail_saves"] is None

    # Source check: every environment read in the order service uses a literal key, and none of
    # those keys is a fault switch. (The refusal check reads os.environ's keys, never a value.)
    env_keys = []
    for path in sorted(Path(ORDER_APP).glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                env_keys.append((path.name, ast.unparse(node.slice)))
            if isinstance(node, ast.Call) and ast.unparse(node.func) in ("os.environ.get", "os.getenv"):
                env_keys.append((path.name, ast.unparse(node.args[0]) if node.args else "<none>"))
    assert env_keys, "expected the entry point to read its settings from the environment"
    for filename, key in env_keys:
        assert key.startswith(("'", '"')), f"{filename}: environment key {key} is not a literal"
        assert "FAIL" not in key.upper(), f"{filename}: reads {key} from the environment"
    assert {k for _, k in env_keys} == {"'ORDER_DB_PATH'", "'STOCK_URL'", "'PAYMENT_URL'"}
