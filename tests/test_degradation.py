"""What happens when Roslyn cannot do its job.

These matter more than the happy path. A scan that quietly reports success on a
codebase it could not resolve would hand the writer an empty graph and the
authority to delete the real one.

The legacy fixture cannot be analyzed on Linux by design: .NET Framework has no
Linux targeting pack and the non-SDK format needs Windows MSBuild. That makes it a
faithful stand-in for the failure a real legacy solution produces.
"""

from __future__ import annotations

import pytest

from app.extractor_runner import ExtractionResult, Outcome, run_extractor
from app.preflight import Verdict, probe_repository
from tests.conftest import REPO_ROOT, read_jsonl, requires_dotnet, requires_extractor

pytestmark = [requires_extractor, requires_dotnet]

LEGACY_FIXTURE = REPO_ROOT / "extractor" / "fixtures" / "LegacySolution"


@pytest.fixture(scope="module")
def legacy_scan(tmp_path_factory: pytest.TempPathFactory) -> ExtractionResult:
    return run_extractor(
        input_path=LEGACY_FIXTURE / "LegacySolution.sln",
        output_dir=tmp_path_factory.mktemp("legacy"),
        repo_id="legacy",
        scan_id="legacy-test",
        repo_root=LEGACY_FIXTURE,
    )


def test_an_unanalyzable_solution_is_never_reported_as_semantic(
    legacy_scan: ExtractionResult,
) -> None:
    assert legacy_scan.manifest is not None, legacy_scan.stderr
    assert legacy_scan.manifest.analysis_mode != "semantic"


def test_an_unanalyzable_solution_never_authorizes_a_sweep(
    legacy_scan: ExtractionResult,
) -> None:
    """The single most important property in this file."""
    assert legacy_scan.may_sweep is False
    assert legacy_scan.outcome in {
        Outcome.INGEST_NO_SWEEP,
        Outcome.INGEST_REDUCED_TRUST,
        Outcome.FALL_BACK,
    }


def test_projects_without_a_core_library_are_not_counted_as_analyzed(
    legacy_scan: ExtractionResult,
) -> None:
    """A compilation with no corelib resolves nothing.

    Roslyn still produces such a compilation quite happily, so without an
    explicit check these projects look successful while yielding no real edges.
    """
    assert legacy_scan.manifest is not None
    assert legacy_scan.manifest.analyzed_project_keys == ()
    assert len(legacy_scan.manifest.failed_project_keys) == 2


def test_the_reason_is_reported_and_actionable(legacy_scan: ExtractionResult) -> None:
    diagnostics = read_jsonl(legacy_scan.output_dir / "diagnostics.jsonl")
    codes = {d["code"] for d in diagnostics}
    assert "NO_CORELIB" in codes
    assert "MSBUILD_FAILED" in codes

    corelib = next(d for d in diagnostics if d["code"] == "NO_CORELIB")
    assert corelib["project_key"] is not None
    # It must say what to do about it, not merely that something went wrong.
    assert "targeting pack" in corelib["message"] or "restore" in corelib["message"]


def test_a_failed_project_still_gets_a_real_target_framework(
    legacy_scan: ExtractionResult,
) -> None:
    """MSBuild reports nothing for a project it could not evaluate, but the TFM is
    part of node identity, so "unknown" would make two such projects collide."""
    compilations = read_jsonl(legacy_scan.output_dir / "compilations.jsonl")
    frameworks = {c["target_framework"] for c in compilations}
    assert "unknown" not in frameworks
    assert frameworks == {"net48", "net8.0-windows"}


def test_syntax_is_still_recorded_when_semantics_are_unavailable(
    legacy_scan: ExtractionResult,
) -> None:
    """Degraded is not useless: the file and project inventory is still true."""
    entities = read_jsonl(legacy_scan.output_dir / "entities.jsonl")
    kinds = {e["kind"] for e in entities}
    assert "Project" in kinds
    assert "CompilationVariant" in kinds


def test_the_probe_predicts_the_failure_before_the_scan_happens() -> None:
    """The point of preflight: this verdict is reachable with no SDK at all, so
    the orchestrator can route the repo to a Windows host instead of scanning it
    on Linux and reading the wreckage afterwards."""
    assert probe_repository(LEGACY_FIXTURE).verdict is Verdict.NEEDS_WINDOWS


def test_probe_and_scan_agree_about_the_target_frameworks() -> None:
    """Two independent implementations of the same shallow csproj read."""
    probe = probe_repository(LEGACY_FIXTURE)
    frameworks = {tfm for p in probe.projects for tfm in p.target_frameworks}
    assert frameworks == {"net48", "net8.0-windows"}
