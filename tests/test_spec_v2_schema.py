"""Tests for the Service Specification v2 JSON Schema.

These tests load `specs/spec.schema.v2.json` directly from the source tree
(same path the production `servicectl validate` will resolve via
importlib.resources after #50 lands) and assert two things:

1. The three shipped example specs validate.
2. A representative set of bad specs is rejected with at least one error.

The schema is the source of truth for the spec shape; these tests are
the source of truth for "the schema does what we claim it does." If a
test fails, the schema drifted from intent and either the schema or the
examples need updating -- do not weaken the test.

Negative cases are kept explicit (not parameterized over a giant blob)
so that a future contributor reading the test file can see, at a glance,
exactly what kind of garbage we want the schema to refuse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "specs" / "spec.schema.v2.json"
EXAMPLES_DIR = REPO_ROOT / "specs" / "examples"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def example_specs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        out[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return out


# --- positive: shipped examples all validate ------------------------------


def test_examples_directory_has_at_least_three_specs(example_specs):
    """Regression: we promised three examples in docs/spec-v2.md."""
    assert len(example_specs) >= 3, (
        f"Expected >=3 example specs in {EXAMPLES_DIR}, found "
        f"{sorted(example_specs)}"
    )


@pytest.mark.parametrize(
    "name",
    ["go-webapi-gcp-cloud-run.json", "python-flask-azure-entra.json", "node-express-local.json"],
)
def test_example_validates(validator, example_specs, name):
    assert name in example_specs, f"Missing example: {name}"
    errs = list(validator.iter_errors(example_specs[name]))
    assert not errs, f"{name} should validate, got: {[e.message for e in errs]}"


# --- negative: schema rejects what it should -----------------------------


@pytest.mark.parametrize(
    ("label", "spec"),
    [
        ("missing schema_version", {"service": {"name": "x"}, "template": "go-webapi"}),
        (
            "schema_version=1 (legacy v1 shape is NOT valid v2)",
            {"schema_version": 1, "service": {"name": "x"}, "template": "go-webapi"},
        ),
        ("missing service", {"schema_version": 2, "template": "go-webapi"}),
        ("missing template", {"schema_version": 2, "service": {"name": "x"}}),
        (
            "bad template value",
            {"schema_version": 2, "service": {"name": "x"}, "template": "rust-axum"},
        ),
        (
            "bad service name (contains space)",
            {"schema_version": 2, "service": {"name": "bad name"}, "template": "go-webapi"},
        ),
        (
            "bad deploy target",
            {
                "schema_version": 2,
                "service": {"name": "x"},
                "template": "go-webapi",
                "deploy": {"target": "lambda"},
            },
        ),
        (
            "coverage_threshold out of range (150)",
            {
                "schema_version": 2,
                "service": {"name": "x"},
                "template": "go-webapi",
                "ci": {"provider": "github-actions", "coverage_threshold": 150},
            },
        ),
        (
            "unknown top-level key (additionalProperties: false)",
            {
                "schema_version": 2,
                "service": {"name": "x"},
                "template": "go-webapi",
                "made_up_field": True,
            },
        ),
        (
            "bad auth overlay",
            {
                "schema_version": 2,
                "service": {"name": "x"},
                "template": "go-webapi",
                "overlays": {"auth": "magic-link"},
            },
        ),
    ],
)
def test_bad_spec_rejected(validator, label, spec):
    errs = list(validator.iter_errors(spec))
    assert errs, f"{label}: expected at least one schema error, got none"
