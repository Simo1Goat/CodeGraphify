"""The fallback decision table (context section 10).

Driven with synthetic manifests rather than real scans: a broken build is hard to
produce on demand, and the point being tested is the classification, not Roslyn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contract import AnalysisMode, Manifest
from app.extractor_runner import (
    EXIT_FAILED,
    EXIT_SUCCESS,
    ExtractorNotFoundError,
    Outcome,
    _classify,
    _read_manifest,
    run_extractor,
)


def _manifest(
    mode: AnalysisMode,
    *,
    analyzed: tuple[str, ...] = ("src/A/A.csproj",),
    failed: tuple[str, ...] = (),
) -> Manifest:
    return Manifest(
        repo_id="r",
        scan_id="s",
        analysis_mode=mode,
        resolution_engine="roslyn",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:01:00Z",
        repo_root="/repo",
        analyzed_project_keys=analyzed,
        failed_project_keys=failed,
    )


@pytest.mark.parametrize(
    ("exit_code", "mode", "expected"),
    [
        (EXIT_SUCCESS, AnalysisMode.SEMANTIC, Outcome.INGEST_AND_SWEEP),
        (EXIT_SUCCESS, AnalysisMode.PARTIAL_SEMANTIC, Outcome.INGEST_NO_SWEEP),
        (EXIT_SUCCESS, AnalysisMode.SYNTAX_ONLY, Outcome.INGEST_REDUCED_TRUST),
        (EXIT_SUCCESS, AnalysisMode.FAILED, Outcome.FALL_BACK),
        # A non-zero exit overrides whatever the manifest claims.
        (EXIT_FAILED, AnalysisMode.SEMANTIC, Outcome.FALL_BACK),
        (3, AnalysisMode.FAILED, Outcome.FALL_BACK),
    ],
)
def test_decision_table(exit_code: int, mode: AnalysisMode, expected: Outcome) -> None:
    assert _classify(exit_code, _manifest(mode)) is expected


def test_semantic_with_failed_projects_never_sweeps() -> None:
    """Belt and braces: the cost of a wrong sweep is deleted nodes.

    The extractor should never emit this combination, so if it ever does, the
    runner must still refuse to delete.
    """
    manifest = _manifest(AnalysisMode.SEMANTIC, failed=("src/B/B.csproj",))
    assert _classify(EXIT_SUCCESS, manifest) is Outcome.INGEST_NO_SWEEP


def test_missing_manifest_falls_back() -> None:
    assert _classify(EXIT_SUCCESS, None) is Outcome.FALL_BACK


def test_manifest_is_read_from_the_file_in_preference_to_stdout(tmp_path: Path) -> None:
    """stdout can pick up runtime or MSBuild noise; the file cannot."""
    on_disk = _manifest(AnalysisMode.SEMANTIC).model_copy(update={"scan_id": "from-file"})
    (tmp_path / "manifest.json").write_text(on_disk.model_dump_json())

    stdout = _manifest(AnalysisMode.FAILED).model_copy(update={"scan_id": "from-stdout"})
    manifest, error = _read_manifest(stdout.model_dump_json(), tmp_path)

    assert error is None
    assert manifest is not None
    assert manifest.scan_id == "from-file"


def test_manifest_falls_back_to_stdout_when_no_file_was_written(tmp_path: Path) -> None:
    """stdout is enough on its own: an output directory the extractor could not
    write to is still a usable scan if the manifest came through."""
    expected = _manifest(AnalysisMode.SEMANTIC)
    manifest, error = _read_manifest(expected.model_dump_json(), tmp_path)
    assert manifest is not None
    assert manifest.analysis_mode == AnalysisMode.SEMANTIC
    assert error is None, "a recovered manifest is not an error the caller must handle"


def test_leading_noise_on_stdout_is_tolerated(tmp_path: Path) -> None:
    payload = _manifest(AnalysisMode.SEMANTIC).model_dump_json()
    manifest, _ = _read_manifest(f"warning: something\n{payload}", tmp_path)
    assert manifest is not None
    assert manifest.analysis_mode == AnalysisMode.SEMANTIC


def test_unparseable_output_yields_no_manifest(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json")
    manifest, error = _read_manifest("also not json", tmp_path)
    assert manifest is None
    assert error is not None


def test_manifest_rejects_an_unknown_analysis_mode(tmp_path: Path) -> None:
    """A newer extractor inventing a mode must not be silently trusted."""
    payload = json.loads(_manifest(AnalysisMode.SEMANTIC).model_dump_json())
    payload["analysis_mode"] = "mostly-fine"
    (tmp_path / "manifest.json").write_text(json.dumps(payload))
    manifest, error = _read_manifest("", tmp_path)
    assert manifest is None
    assert error is not None


def test_missing_extractor_raises_rather_than_falling_back_silently(tmp_path: Path) -> None:
    """A deployment error is not a reason to degrade to tree-sitter."""
    with pytest.raises(ExtractorNotFoundError):
        run_extractor(
            input_path=tmp_path,
            output_dir=tmp_path / "out",
            repo_id="r",
            extractor_path=tmp_path / "does-not-exist",
        )


def test_a_timeout_falls_back_rather_than_raising(tmp_path: Path) -> None:
    """A scan that outruns its budget is this engine's failure, not the request's.

    The caller's next move is the tree-sitter fallback, so a timeout must come
    back as an ordinary result.
    """
    slow = tmp_path / "slow-extractor"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)

    result = run_extractor(
        input_path=tmp_path,
        output_dir=tmp_path / "out",
        repo_id="r",
        extractor_path=slow,
        timeout_seconds=0.5,
    )

    assert result.outcome is Outcome.FALL_BACK
    assert result.manifest is None
    assert result.error is not None
    assert "0.5" in result.error
    assert result.ok is False
    assert result.may_sweep is False


def test_an_extractor_that_writes_nothing_falls_back(tmp_path: Path) -> None:
    """No manifest means no basis for trusting anything it did write."""
    silent = tmp_path / "silent-extractor"
    silent.write_text("#!/bin/sh\nexit 0\n")
    silent.chmod(0o755)

    result = run_extractor(
        input_path=tmp_path,
        output_dir=tmp_path / "out",
        repo_id="r",
        extractor_path=silent,
    )

    assert result.outcome is Outcome.FALL_BACK
    assert result.error is not None
