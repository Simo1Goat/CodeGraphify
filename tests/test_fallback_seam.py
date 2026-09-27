"""The fallback seam, exercised with a stub.

The real tree-sitter engine is in another repository. These tests pin the
obligations its adapter will have to meet, so the merge is checked against
something rather than assumed to fit.
"""

from __future__ import annotations

from pathlib import Path

from app.contract import AnalysisMode, Manifest
from app.fallback import (
    FallbackAnalyzer,
    FallbackRequest,
    NoFallbackAvailable,
    validate_fallback_manifest,
)


def _manifest(mode: AnalysisMode, engine: str = "tree-sitter") -> Manifest:
    return Manifest(
        repo_id="r",
        scan_id="s",
        analysis_mode=mode,
        resolution_engine=engine,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        repo_root="/repo",
    )


class StubTreeSitter:
    """The shape the other repository's adapter will take."""

    def analyze(self, request: FallbackRequest) -> Manifest | None:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "entities.jsonl").write_text("")
        (request.output_dir / "relations.jsonl").write_text("")
        return _manifest(AnalysisMode.SYNTAX_ONLY)


def test_a_conforming_adapter_satisfies_the_protocol() -> None:
    """Structural, not inherited: the other repo need not import from here."""
    assert isinstance(StubTreeSitter(), FallbackAnalyzer)


def test_the_placeholder_also_satisfies_the_protocol() -> None:
    assert isinstance(NoFallbackAvailable(), FallbackAnalyzer)


def test_the_placeholder_reports_that_nothing_is_wired_up(tmp_path: Path) -> None:
    """A missing fallback is a real gap, not a silent no-op."""
    fallback = NoFallbackAvailable()
    request = FallbackRequest(
        repo_root=tmp_path, repo_id="r", scan_id="s", output_dir=tmp_path / "out"
    )
    assert fallback.analyze(request) is None
    assert "not wired up" in fallback.reason


def test_a_syntax_only_manifest_is_accepted() -> None:
    assert validate_fallback_manifest(_manifest(AnalysisMode.SYNTAX_ONLY)) == []


def test_a_fallback_may_not_claim_semantic_analysis() -> None:
    """The guard that matters: 'semantic' earns a stale sweep, and a syntax-only
    engine cannot justify deleting anything."""
    problems = validate_fallback_manifest(_manifest(AnalysisMode.SEMANTIC))
    assert problems
    assert "may not report" in problems[0]


def test_a_fallback_may_not_claim_partial_semantic_either() -> None:
    assert validate_fallback_manifest(_manifest(AnalysisMode.PARTIAL_SEMANTIC))


def test_a_fallback_reporting_failure_is_acceptable() -> None:
    assert validate_fallback_manifest(_manifest(AnalysisMode.FAILED)) == []


def test_a_fallback_may_not_impersonate_roslyn() -> None:
    """Attribution is what keeps a mixed graph interpretable."""
    problems = validate_fallback_manifest(_manifest(AnalysisMode.SYNTAX_ONLY, engine="roslyn"))
    assert problems
    assert "resolution_engine" in problems[0]


def test_a_stub_writes_the_expected_output_layout(tmp_path: Path) -> None:
    """The contract between engines is the files on disk, not an object model."""
    request = FallbackRequest(
        repo_root=tmp_path, repo_id="r", scan_id="s", output_dir=tmp_path / "out"
    )
    manifest = StubTreeSitter().analyze(request)

    assert manifest is not None
    assert validate_fallback_manifest(manifest) == []
    assert (request.output_dir / "entities.jsonl").exists()
    assert (request.output_dir / "relations.jsonl").exists()


def test_a_request_can_be_scoped_to_specific_projects() -> None:
    """So the fallback can cover only what Roslyn could not."""
    request = FallbackRequest(
        repo_root=Path("/repo"),
        repo_id="r",
        scan_id="s",
        output_dir=Path("/out"),
        project_keys=("src/Legacy/Legacy.csproj",),
        reason="MSBuild could not evaluate the project",
    )
    assert request.project_keys == ("src/Legacy/Legacy.csproj",)
    assert request.reason
