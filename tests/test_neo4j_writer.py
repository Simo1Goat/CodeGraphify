"""The Neo4j writer, against a live database.

The sweep is the only operation in this system that destroys data, so most of this
file is about trying to make it delete something it should not: a partial scan, a
project outside the analyzed scope, a shared node another project still uses.

Requires a disposable local Neo4j (``docker compose up -d``). **These tests wipe
the database**, so they skip rather than run against anything that is not local.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from app.contract import AnalysisMode
from app.extractor_runner import ExtractionResult, Outcome, run_extractor
from app.neo4j_writer import (
    Neo4jSettings,
    Neo4jWriter,
    SweepRefusedError,
    UnknownGraphLabelError,
)
from app.neo4j_writer.rows import edge_key
from app.neo4j_writer.schema import safe_label, safe_relationship_type
from tests.conftest import FIXTURE_SOLUTION, requires_dotnet, requires_extractor


def _neo4j_available() -> bool:
    try:
        from neo4j import GraphDatabase
    except ImportError:  # pragma: no cover
        return False

    settings = Neo4jSettings.from_env()
    if "localhost" not in settings.uri and "127.0.0.1" not in settings.uri:
        # These tests delete everything; never aim them at a remote database.
        return False
    try:
        driver = GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password))
        try:
            driver.verify_connectivity()
            return True
        finally:
            driver.close()
    except Exception:  # noqa: BLE001 - any failure here means "no database", and
        # probing must never fail the run; the tests simply skip.
        return False


requires_neo4j = pytest.mark.skipif(
    not _neo4j_available(),
    reason="no local Neo4j; start one with 'docker compose up -d'",
)

pytestmark = [requires_neo4j, requires_extractor, requires_dotnet]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def writer() -> Iterator[Neo4jWriter]:
    """A writer against an empty graph."""
    with Neo4jWriter.connect() as instance:
        with instance._driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        instance.apply_schema()
        yield instance


def _query(writer: Neo4jWriter, cypher: str, **params: object) -> list[dict]:
    with writer._driver.session() as session:
        return [record.data() for record in session.run(cypher, **params)]


def _count(writer: Neo4jWriter, cypher: str, **params: object) -> int:
    result = _query(writer, cypher, **params)
    return int(next(iter(result[0].values()))) if result else 0


@pytest.fixture(scope="module")
def scannable_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A buildable copy of the fixture solution that tests may edit.

    Built here rather than copied with its obj/ directory: project.assets.json holds
    absolute paths, so a copied one describes the original location.
    """
    copy = tmp_path_factory.mktemp("mutable-fixture") / "SampleSolution"
    shutil.copytree(
        FIXTURE_SOLUTION.parent, copy, ignore=shutil.ignore_patterns("bin", "obj")
    )
    dotnet = shutil.which("dotnet") or str(Path.home() / ".dotnet" / "dotnet")
    subprocess.run(
        [dotnet, "build", str(copy / "SampleSolution.sln"), "-v", "q", "--nologo"],
        check=True,
        capture_output=True,
        text=True,
    )
    return copy


def _scan(root: Path, output: Path, scan_id: str) -> ExtractionResult:
    return run_extractor(
        input_path=root / "SampleSolution.sln",
        output_dir=output,
        repo_id="sample",
        scan_id=scan_id,
        repo_root=root,
    )


@pytest.fixture(scope="module")
def clean_scan(tmp_path_factory: pytest.TempPathFactory) -> ExtractionResult:
    return _scan(
        FIXTURE_SOLUTION.parent, tmp_path_factory.mktemp("writer-scan"), "writer-clean"
    )


# --- schema -------------------------------------------------------------------


def test_applying_schema_twice_is_harmless(writer: Neo4jWriter) -> None:
    """It runs before every load, so it has to be idempotent."""
    writer.apply_schema()
    constraints = _count(writer, "SHOW CONSTRAINTS YIELD name RETURN count(*) AS c")
    assert constraints >= 2


def test_the_node_key_is_unique(writer: Neo4jWriter, clean_scan: ExtractionResult) -> None:
    """MERGE relies on this, and without the index it backs, a load on a heavy repo
    degrades to a full label scan per row."""
    writer.ingest(clean_scan)
    duplicates = _count(
        writer,
        "MATCH (n:CodeEntity) WITH n.id AS id, count(*) AS c WHERE c > 1 RETURN count(*) AS d",
    )
    assert duplicates == 0


def test_a_kind_outside_the_contract_cannot_reach_cypher() -> None:
    """Kinds are interpolated because Neo4j cannot parameterise a label, so the
    allow-list is the only thing between the contract and Cypher injection."""
    with pytest.raises(UnknownGraphLabelError):
        safe_label("Method) DETACH DELETE n //")
    with pytest.raises(UnknownGraphLabelError):
        safe_relationship_type("CALLS_DIRECT]-() DELETE r //")

    assert safe_label("Method") == "Method"
    assert safe_relationship_type("CALLS_DIRECT") == "CALLS_DIRECT"


# --- ingest -------------------------------------------------------------------


def test_ingest_writes_the_whole_graph(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    report = writer.ingest(clean_scan)
    assert clean_scan.manifest is not None

    assert report.nodes_written == clean_scan.manifest.counts["entities"]
    assert _count(writer, "MATCH (n:CodeEntity) RETURN count(*) AS c") == report.nodes_written

    # Derived edges are counted separately: they are computed from the graph, not
    # written from the payload.
    observed = _count(
        writer, "MATCH ()-[r]->() WHERE r.derived IS NULL RETURN count(*) AS c"
    )
    assert observed == report.relationships_written
    assert _count(
        writer, "MATCH ()-[r]->() WHERE r.derived RETURN count(*) AS c"
    ) == report.depends_on_derived


def test_no_relationship_is_dropped_for_a_missing_endpoint(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """The extractor promises no dangling edges; this is where that promise is
    cashed. A skipped relationship means the two sides disagree about identity."""
    report = writer.ingest(clean_scan)
    assert report.relationships_skipped == 0, report.skipped_kinds


def test_nodes_carry_both_the_shared_label_and_their_kind(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """The shared label is what the constraint and the sweep use; the kind label is
    what makes the graph readable and lets a query narrow cheaply."""
    writer.ingest(clean_scan)
    labels = _query(
        writer,
        "MATCH (n:CodeEntity {name: 'RunAll'}) RETURN labels(n) AS labels",
    )
    assert labels
    assert set(labels[0]["labels"]) == {"CodeEntity", "Method"}


def test_metadata_is_promoted_to_properties(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    writer.ingest(clean_scan)
    rows = _query(
        writer,
        """MATCH (n:Method {name: 'Submit'})
           WHERE n.declaring_type = 'Acme.Core.Orders.OrderService'
             AND n.parameter_count = 2
           RETURN n.accessibility AS accessibility, n.parameter_types AS parameter_types,
                  n.project_key AS project_key, n.target_framework AS target_framework""",
    )
    assert len(rows) == 1
    assert rows[0]["accessibility"] == "Public"
    assert rows[0]["parameter_types"] == ["Acme.Core.Orders.Order", "bool"]
    assert rows[0]["project_key"].endswith("Core.csproj")
    assert rows[0]["target_framework"] == "net8.0"


def test_relationships_carry_their_evidence(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    writer.ingest(clean_scan)
    rows = _query(
        writer,
        """MATCH (:Method {name: 'RunAll'})-[r:CALLS_INTERFACE]->()
           RETURN r.file_path AS file, r.start_line AS line, r.start_column AS col,
                  r.dispatch_kind AS dispatch, r.resolution_status AS status""",
    )
    assert rows
    for row in rows:
        assert row["file"].endswith(".cs")
        assert row["line"] >= 1
        assert row["col"] >= 1
        assert row["dispatch"] == "Interface"
        assert row["status"] == "InterfaceDispatch"


def test_ingesting_the_same_scan_twice_does_not_duplicate_anything(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """A retried ingest must be safe: MERGE on a stable key, never CREATE."""
    first = writer.ingest(clean_scan)
    nodes = _count(writer, "MATCH (n:CodeEntity) RETURN count(*) AS c")
    relationships = _count(writer, "MATCH ()-[r]->() RETURN count(*) AS c")

    writer.ingest(clean_scan)
    assert _count(writer, "MATCH (n:CodeEntity) RETURN count(*) AS c") == nodes
    assert _count(writer, "MATCH ()-[r]->() RETURN count(*) AS c") == relationships
    assert first.nodes_written > 0


def test_a_scan_record_is_written(writer: Neo4jWriter, clean_scan: ExtractionResult) -> None:
    """So "why is this project missing from the graph" has an answer later, when the
    manifest's temp directory is long gone."""
    writer.ingest(clean_scan)
    rows = _query(
        writer,
        "MATCH (s:Scan {scan_id: 'writer-clean'}) "
        "RETURN s.analysis_mode AS mode, s.outcome AS outcome, "
        "s.analyzed_project_keys AS analyzed, s.count_calls AS calls",
    )
    assert len(rows) == 1
    assert rows[0]["mode"] == "semantic"
    assert rows[0]["outcome"] == "ingest-and-sweep"
    assert len(rows[0]["analyzed"]) == 2
    assert rows[0]["calls"] > 0


def test_first_seen_is_kept_across_scans(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """Node age is only visible if the first scan that saw it is not overwritten."""
    writer.ingest(clean_scan)
    assert clean_scan.manifest is not None

    relabelled = replace(
        clean_scan,
        manifest=clean_scan.manifest.model_copy(update={"scan_id": "writer-clean-2"}),
    )
    writer.ingest(relabelled)

    rows = _query(
        writer,
        "MATCH (n:CodeEntity {name: 'RunAll'}) "
        "RETURN n.first_seen_scan_id AS first, n.last_seen_scan_id AS last",
    )
    assert rows[0]["first"] == "writer-clean"
    assert rows[0]["last"] == "writer-clean-2"


# --- the sweep: what it must NOT do -------------------------------------------


def test_a_partial_scan_is_refused_a_sweep(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """The single most important guard here.

    A transient build break makes a scan partial. If that scan were allowed to
    sweep, the projects it failed to compile would be deleted from the graph --
    turning a temporary build failure into permanent data loss.
    """
    assert clean_scan.manifest is not None
    partial = replace(
        clean_scan,
        outcome=Outcome.INGEST_NO_SWEEP,
        manifest=clean_scan.manifest.model_copy(
            update={
                "analysis_mode": AnalysisMode.PARTIAL_SEMANTIC,
                "failed_project_keys": ("src/Core/Core.csproj",),
            }
        ),
    )
    assert partial.may_sweep is False

    with pytest.raises(SweepRefusedError, match="Refusing"):
        writer.ingest(partial, sweep=True)


def test_a_partial_scan_still_ingests(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """Degraded is not useless: what it did observe is still true and still loaded,
    it simply earns no authority to delete."""
    assert clean_scan.manifest is not None
    partial = replace(
        clean_scan,
        outcome=Outcome.INGEST_NO_SWEEP,
        manifest=clean_scan.manifest.model_copy(
            update={"analysis_mode": AnalysisMode.PARTIAL_SEMANTIC}
        ),
    )

    report = writer.ingest(partial)
    assert report.nodes_written > 0
    assert report.swept is False
    assert report.nodes_deleted == 0


def test_a_syntax_only_scan_does_not_sweep(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    assert clean_scan.manifest is not None
    syntax_only = replace(
        clean_scan,
        outcome=Outcome.INGEST_REDUCED_TRUST,
        manifest=clean_scan.manifest.model_copy(
            update={"analysis_mode": AnalysisMode.SYNTAX_ONLY}
        ),
    )
    assert writer.ingest(syntax_only).swept is False


def test_a_sweep_cannot_run_unscoped(writer: Neo4jWriter) -> None:
    """An empty scope would mean "everything", which is exactly the accident this
    guard exists to prevent."""
    with pytest.raises(ValueError, match="at least one project"):
        writer.sweep("some-scan", ())


def test_a_sweep_does_not_touch_projects_outside_its_scope(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """A scan of one project must not delete another project's graph."""
    writer.ingest(clean_scan)
    before = _count(
        writer,
        "MATCH (n:CodeEntity) WHERE n.project_key ENDS WITH 'App.csproj' RETURN count(*) AS c",
    )
    assert before > 0

    # Sweep Core only, with a scan id nothing carries: everything in Core is stale
    # by that measure, and App must still be untouched.
    writer.sweep("a-scan-that-saw-nothing", ("src/Core/Core.csproj",))

    after = _count(
        writer,
        "MATCH (n:CodeEntity) WHERE n.project_key ENDS WITH 'App.csproj' RETURN count(*) AS c",
    )
    assert after == before


def test_a_sweep_does_not_delete_shared_nodes(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """Files, namespaces and referenced assemblies have no project_key because they
    are shared. Judging one stale would need proof that no other project still uses
    it, so they are left alone."""
    writer.ingest(clean_scan)
    before = _count(
        writer,
        "MATCH (n:CodeEntity) WHERE n.project_key IS NULL RETURN count(*) AS c",
    )
    assert before > 0

    writer.sweep(
        "a-scan-that-saw-nothing",
        ("src/Core/Core.csproj", "src/App/App.csproj"),
    )

    after = _count(
        writer, "MATCH (n:CodeEntity) WHERE n.project_key IS NULL RETURN count(*) AS c"
    )
    assert after == before


# --- the sweep: what it must do ----------------------------------------------


def test_rescanning_unchanged_code_deletes_nothing(
    writer: Neo4jWriter, scannable_copy: Path, tmp_path: Path
) -> None:
    """Ids must be stable across scans, or every scan looks like a full rewrite and
    the sweep deletes and recreates the entire graph."""
    first = _scan(scannable_copy, tmp_path / "a", "stability-1")
    writer.ingest(first)
    nodes = _count(writer, "MATCH (n:CodeEntity) RETURN count(*) AS c")
    relationships = _count(writer, "MATCH ()-[r]->() RETURN count(*) AS c")

    second = _scan(scannable_copy, tmp_path / "b", "stability-2")
    report = writer.ingest(second)

    assert report.swept is True
    assert report.nodes_deleted == 0
    assert report.relationships_deleted == 0
    assert _count(writer, "MATCH (n:CodeEntity) RETURN count(*) AS c") == nodes
    assert _count(writer, "MATCH ()-[r]->() RETURN count(*) AS c") == relationships


def test_a_removed_call_is_swept_while_its_endpoints_survive(
    writer: Neo4jWriter, scannable_copy: Path, tmp_path: Path
) -> None:
    """The commonest edit there is: a call goes away, both methods stay.

    The edge is stale while its endpoints are current, so DETACH DELETE on stale
    nodes would never reach it. This is why relationships are swept in their own
    right.
    """
    program = scannable_copy / "src" / "App" / "Program.cs"
    original = program.read_text()

    writer.ingest(_scan(scannable_copy, tmp_path / "before", "call-before"))

    def interface_calls() -> int:
        return _count(
            writer,
            "MATCH (:Method {name: 'RunAll'})-[r:CALLS_INTERFACE]->(t:Method {name: 'Submit'}) "
            "RETURN count(r) AS c",
        )

    assert interface_calls() == 2

    try:
        program.write_text(original.replace("        _service.Submit(order, force: true);\n", ""))
        report = writer.ingest(_scan(scannable_copy, tmp_path / "after", "call-after"))

        assert report.swept is True
        assert report.relationships_deleted >= 1
        assert interface_calls() == 1, "the removed call's edge should be gone"

        # Both endpoints are still referenced elsewhere and must survive.
        assert _count(writer, "MATCH (n:Method {name: 'RunAll'}) RETURN count(*) AS c") == 1
        assert (
            _count(
                writer,
                "MATCH (n:Method {name: 'Submit'}) "
                "WHERE n.declaring_type = 'Acme.Core.Orders.IOrderService' "
                "RETURN count(*) AS c",
            )
            == 2
        )
    finally:
        program.write_text(original)


def test_a_deleted_method_is_swept(
    writer: Neo4jWriter, scannable_copy: Path, tmp_path: Path
) -> None:
    """A node this scan did not observe, inside the scanned scope, goes."""
    auditing = scannable_copy / "src" / "Core" / "Auditing.cs"
    original = auditing.read_text()

    writer.ingest(_scan(scannable_copy, tmp_path / "before", "type-before"))
    assert _count(writer, "MATCH (n:Class {name: 'OrderException'}) RETURN count(*) AS c") == 1

    try:
        without_exception = original.replace(
            """// Inherits an EXTERNAL base class. Unlike the implicit object base, this is a real
// dependency on a framework type and must appear in the graph.
public class OrderException : InvalidOperationException
{
    public OrderException(string message) : base(message)
    {
    }
}

""",
            "",
        )
        assert without_exception != original, "fixture text changed; update this test"
        auditing.write_text(without_exception)

        report = writer.ingest(_scan(scannable_copy, tmp_path / "after", "type-after"))
        assert report.swept is True
        assert (
            _count(writer, "MATCH (n:Class {name: 'OrderException'}) RETURN count(*) AS c") == 0
        )
        # Its sibling in the same file must not have gone with it.
        assert (
            _count(
                writer, "MATCH (n:Class {name: 'TrackedOrderService'}) RETURN count(*) AS c"
            )
            == 1
        )
    finally:
        auditing.write_text(original)


# --- edge identity ------------------------------------------------------------


def test_edge_identity_includes_the_call_site() -> None:
    """One edge per call site, so the same caller calling the same target on two
    lines is two facts rather than one."""
    base = {
        "source": "a",
        "target": "b",
        "kind": "CALLS_DIRECT",
        "resolved": True,
        "evidence": {
            "file_path": "x.cs",
            "start_line": 10,
            "start_column": 5,
            "end_line": 10,
            "end_column": 20,
        },
    }
    same_line = dict(base)
    other_line = {**base, "evidence": {**base["evidence"], "start_line": 11}}
    other_kind = {**base, "kind": "CALLS_VIRTUAL"}

    assert edge_key(base) == edge_key(same_line)
    assert edge_key(base) != edge_key(other_line)
    assert edge_key(base) != edge_key(other_kind)


def test_a_structural_edge_without_evidence_still_has_a_key() -> None:
    """CONTAINS and COMPILES have no single call site; they still need identity."""
    relation = {
        "source": "ns",
        "target": "type",
        "kind": "CONTAINS",
        "resolved": True,
        "evidence": None,
    }
    assert len(edge_key(relation)) == 32


# --- derived edges ------------------------------------------------------------


def test_depends_on_is_derived_as_a_project_roll_up(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """Derived in the database, not emitted per call: emitting it per call site
    would duplicate the whole call graph at a coarser grain."""
    report = writer.ingest(clean_scan)
    assert report.depends_on_derived >= 1

    rows = _query(
        writer,
        """MATCH (source:Project)-[d:DEPENDS_ON]->(target:Project)
           RETURN source.canonical_name AS source, target.canonical_name AS target,
                  d.weight AS weight, d.derived AS derived""",
    )
    assert len(rows) == 1
    assert rows[0]["source"].endswith("App.csproj")
    assert rows[0]["target"].endswith("Core.csproj")
    assert rows[0]["weight"] > 0
    assert rows[0]["derived"] is True


def test_structural_containment_is_not_a_dependency(
    writer: Neo4jWriter, clean_scan: ExtractionResult
) -> None:
    """A namespace holding a type is not a dependency, and counting it would make
    every project depend on itself."""
    writer.ingest(clean_scan)
    self_dependencies = _count(
        writer,
        "MATCH (p:Project)-[:DEPENDS_ON]->(p) RETURN count(*) AS c",
    )
    assert self_dependencies == 0


def test_the_roll_up_reflects_the_graph_after_sweeping(
    writer: Neo4jWriter, scannable_copy: Path, tmp_path: Path
) -> None:
    """Derived after the sweep, so a removed call stops being counted."""
    program = scannable_copy / "src" / "App" / "Program.cs"
    original = program.read_text()

    writer.ingest(_scan(scannable_copy, tmp_path / "before", "weight-before"))
    before = _query(
        writer, "MATCH (:Project)-[d:DEPENDS_ON]->(:Project) RETURN d.weight AS weight"
    )[0]["weight"]

    try:
        program.write_text(
            original.replace("        _service.Submit(order, force: true);\n", "")
        )
        writer.ingest(_scan(scannable_copy, tmp_path / "after", "weight-after"))
        after = _query(
            writer, "MATCH (:Project)-[d:DEPENDS_ON]->(:Project) RETURN d.weight AS weight"
        )[0]["weight"]
        assert after < before, "a removed cross-project call should lower the weight"
    finally:
        program.write_text(original)
