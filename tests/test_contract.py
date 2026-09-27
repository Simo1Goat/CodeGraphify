"""The extractor's output must satisfy the contract the writer relies on.

These validate every emitted row against the pydantic models, so a field the C#
side renames or drops fails here rather than at ingest time.
"""

from __future__ import annotations

import pytest

from app.contract import Diagnostic, Entity, EntityKind, Relation, RelationKind
from app.contract.models import CompilationContext
from app.extractor_runner import ExtractionResult, Outcome
from tests.conftest import read_jsonl, requires_dotnet, requires_extractor

pytestmark = [requires_extractor, requires_dotnet]


def test_scan_of_a_clean_solution_is_semantic_and_sweepable(scan: ExtractionResult) -> None:
    assert scan.exit_code == 0, scan.stderr
    assert scan.manifest is not None
    assert scan.manifest.analysis_mode == "semantic"
    assert scan.manifest.failed_project_keys == ()
    # The whole point of the mode: a clean scan is the only one allowed to delete.
    assert scan.outcome is Outcome.INGEST_AND_SWEEP
    assert scan.may_sweep is True


def test_manifest_reports_the_projects_it_analyzed(scan: ExtractionResult) -> None:
    assert scan.manifest is not None
    analyzed = {key.split("/")[-1] for key in scan.manifest.analyzed_project_keys}
    assert analyzed == {"Core.csproj", "App.csproj"}


def test_manifest_counts_match_the_jsonl_shards(scan: ExtractionResult) -> None:
    assert scan.manifest is not None
    for output_file in scan.manifest.output_files:
        rows = read_jsonl(scan.output_dir / output_file.path)
        assert len(rows) == output_file.count, f"{output_file.path} count disagrees"


def test_every_entity_validates_against_the_contract(entities: dict[str, dict]) -> None:
    assert entities, "no entities emitted"
    for raw in entities.values():
        Entity.model_validate(raw)


def test_every_relation_validates_against_the_contract(relations: list[dict]) -> None:
    assert relations, "no relations emitted"
    for raw in relations:
        Relation.model_validate(raw)


def test_every_compilation_context_validates(compilations: list[dict]) -> None:
    assert compilations
    for raw in compilations:
        CompilationContext.model_validate(raw)


def test_diagnostics_validate(scan: ExtractionResult) -> None:
    for raw in read_jsonl(scan.output_dir / "diagnostics.jsonl"):
        Diagnostic.model_validate(raw)


def test_entity_kinds_are_all_known(entities: dict[str, dict]) -> None:
    emitted = {e["kind"] for e in entities.values()}
    assert emitted <= {k.value for k in EntityKind}, "extractor invented an entity kind"


def test_relation_kinds_are_all_known(relations: list[dict]) -> None:
    emitted = {r["kind"] for r in relations}
    assert emitted <= {k.value for k in RelationKind}, "extractor invented a relation kind"


def test_every_relation_points_at_an_emitted_entity(
    entities: dict[str, dict], relations: list[dict]
) -> None:
    """A dangling edge would MERGE a blank node into Neo4j and corrupt the graph."""
    dangling = [
        (r["kind"], r["source"], r["target"])
        for r in relations
        if r["source"] not in entities or r["target"] not in entities
    ]
    assert dangling == [], f"{len(dangling)} relations reference unemitted entities"


@pytest.mark.parametrize("field", ["repo_id", "scan_id", "resolution_engine"])
def test_every_relation_carries_provenance(relations: list[dict], field: str) -> None:
    """Context section 4: every edge must say which scan and engine produced it."""
    missing = [r["kind"] for r in relations if field not in r.get("metadata", {})]
    assert missing == [], f"{len(missing)} relations missing {field}"


def test_entity_ids_are_unique(scan: ExtractionResult) -> None:
    rows = read_jsonl(scan.output_dir / "entities.jsonl")
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate entity rows would double-write nodes"
