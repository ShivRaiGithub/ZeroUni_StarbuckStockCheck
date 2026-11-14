"""Check real HTTP responses against the committed OpenAPI 3.1 contracts in contracts/.

For every response a test passes to ``Contract.check``:
  * the request's method + path must match an operation in the contract,
  * the status code must be one the operation documents,
  * every header the response documents must be present,
  * a JSON body must validate against the documented schema (JSON Schema 2020-12, with
    cross-file $refs such as ./order-service.yaml#/... resolved).

Each (contract, path, method, status) that a test exercised is recorded in ``COVERED`` so the
session can fail if a documented status was never exercised.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.jsonschema import DRAFT202012

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
BASE_URI = "file:///contracts/"
CONTRACT_FILES = ("stock-service.yaml", "order-service.yaml", "gateway.yaml")
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}

DOCS: dict[str, dict] = {
    name: yaml.safe_load((CONTRACTS_DIR / name).read_text(encoding="utf-8")) for name in CONTRACT_FILES
}
REGISTRY = Registry().with_resources(
    (BASE_URI + name, DRAFT202012.create_resource(doc)) for name, doc in DOCS.items()
)

# (contract file, path template, method, status)
COVERED: set[tuple[str, str, str, str]] = set()


def documented_responses() -> set[tuple[str, str, str, str]]:
    out = set()
    for name, doc in DOCS.items():
        for template, item in doc["paths"].items():
            for method, op in item.items():
                if method in HTTP_METHODS:
                    for status in op["responses"]:
                        out.add((name, template, method, status))
    return out


def _pointer_token(token: str) -> str:
    return quote(token.replace("~", "~0").replace("/", "~1"), safe="~")


def _template_regex(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$")


class Contract:
    def __init__(self, filename: str):
        if filename not in DOCS:
            raise ValueError(f"unknown contract {filename}")
        self.name = filename
        self.doc = DOCS[filename]
        self.uri = BASE_URI + filename

    def operation(self, method: str, path: str) -> tuple[str, dict]:
        method = method.lower()
        for template, item in self.doc["paths"].items():
            if _template_regex(template).match(path) and method in item:
                return template, item[method]
        raise AssertionError(f"{self.name}: {method.upper()} {path} is not an operation in the contract")

    def check(self, response: httpx.Response):
        """Assert the response conforms to the contract; return the parsed JSON body (or None)."""
        request = response.request
        template, op = self.operation(request.method, request.url.path)
        method = request.method.lower()
        status = str(response.status_code)
        documented = sorted(op["responses"])
        assert status in op["responses"], (
            f"{self.name}: {request.method} {template} returned {status}; "
            f"contract documents {documented}. Body: {response.text[:500]}"
        )
        pointer = "/paths/" + _pointer_token(template) + f"/{method}/responses/{status}"
        resp_obj = op["responses"][status]
        if "$ref" in resp_obj:
            ref = resp_obj["$ref"]
            assert ref.startswith("#/"), f"only local response $refs are supported, got {ref}"
            pointer = ref[1:]
            resp_obj = self.doc
            for part in ref[2:].split("/"):
                resp_obj = resp_obj[part.replace("~1", "/").replace("~0", "~")]
        COVERED.add((self.name, template, method, status))

        for header in resp_obj.get("headers", {}):
            assert header.lower() in response.headers, (
                f"{self.name}: {request.method} {template} {status} must send header {header}"
            )

        content = resp_obj.get("content")
        if not content:
            return None
        assert "application/json" in content, f"{self.name}: only application/json bodies are checked"
        assert response.headers.get("content-type", "").startswith("application/json"), (
            f"{self.name}: {request.method} {template} {status} must be application/json, "
            f"got {response.headers.get('content-type')!r}"
        )
        body = response.json()
        schema_uri = f"{self.uri}#{pointer}/content/{_pointer_token('application/json')}/schema"
        validator = Draft202012Validator(
            {"$ref": schema_uri}, registry=REGISTRY, format_checker=FormatChecker()
        )
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path))
        assert not errors, (
            f"{self.name}: {request.method} {template} {status} body violates the contract:\n"
            + "\n".join(f"  at {list(e.absolute_path)}: {e.message[:300]}" for e in errors[:10])
        )
        return body


STOCK = Contract("stock-service.yaml")
ORDER = Contract("order-service.yaml")
GATEWAY = Contract("gateway.yaml")
