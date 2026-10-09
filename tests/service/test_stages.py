"""docs/plan/stages.json — the stage map as data (ТЗ-B0 §4, §7).

Checks: the file validates against docs/plan/stages.schema.json; the set of
stage ids equals the committed list (docs/plan/stage_ids.json — the ids the
ТЗ's Stage map names); dependencies refer to known stages; the top-level
`updated` is never older than any stage's `updated`.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "plan"


def _load(name: str):
    return json.loads((PLAN / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stages_file():
    return _load("stages.json")


@pytest.fixture(scope="module")
def schema():
    return _load("stages.schema.json")


def test_validates_against_schema(stages_file, schema):
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    errors = sorted(validator_cls(schema).iter_errors(stages_file), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_schema_rejects_an_unknown_status(stages_file, schema):
    bad = json.loads(json.dumps(stages_file))
    bad["stages"][0]["status"] = "shipped"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_ids_equal_the_committed_list(stages_file):
    ids = [s["id"] for s in stages_file["stages"]]
    assert len(ids) == len(set(ids)), f"duplicate stage ids: {ids}"
    expected = _load("stage_ids.json")["ids"]
    assert set(ids) == set(expected), (
        f"stages.json ids {sorted(set(ids) ^ set(expected))} differ from docs/plan/stage_ids.json — "
        "update both in the same commit (the master plan doc is the source of truth)"
    )


def test_dependencies_are_known_stages(stages_file):
    ids = {s["id"] for s in stages_file["stages"]}
    for s in stages_file["stages"]:
        assert s["id"] not in s["depends_on"], f"{s['id']} depends on itself"
        unknown = set(s["depends_on"]) - ids
        assert not unknown, f"{s['id']} depends on unknown stage(s) {sorted(unknown)}"


def test_dates_parse_and_top_level_is_newest(stages_file):
    top = dt.date.fromisoformat(stages_file["updated"])
    for s in stages_file["stages"]:
        assert dt.date.fromisoformat(s["updated"]) <= top, f"{s['id']} updated after the file's `updated`"


def test_in_progress_stages_are_what_the_shell_claims(stages_file):
    # R0 is the stage this branch builds; nothing else is in progress yet.
    in_progress = sorted(s["id"] for s in stages_file["stages"] if s["status"] == "in_progress")
    assert in_progress == ["R0"]
