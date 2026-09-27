from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.extractor_runner import ExtractionResult, default_extractor_path, run_extractor

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SOLUTION = REPO_ROOT / "extractor" / "fixtures" / "SampleSolution" / "SampleSolution.sln"


def _dotnet() -> str | None:
    found = shutil.which("dotnet")
    if found:
        return found
    local = Path.home() / ".dotnet" / "dotnet"
    return str(local) if local.exists() else None


requires_extractor = pytest.mark.skipif(
    not default_extractor_path().exists(),
    reason="extractor not built; run dotnet build extractor/src/CodeGraphify.Extractor",
)

requires_dotnet = pytest.mark.skipif(
    _dotnet() is None,
    reason="no .NET SDK on this machine; MSBuildWorkspace cannot open projects",
)


@pytest.fixture(scope="session")
def fixture_built() -> None:
    """The fixture solution must restore before Roslyn can resolve its references."""
    dotnet = _dotnet()
    if dotnet is None:
        pytest.skip("no .NET SDK")
    subprocess.run(
        [dotnet, "build", str(FIXTURE_SOLUTION), "-v", "q", "--nologo"],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def scan(tmp_path_factory: pytest.TempPathFactory, fixture_built: None) -> ExtractionResult:
    """One primary-variant-only scan of the fixture, shared by the whole session."""
    output = tmp_path_factory.mktemp("scan-primary")
    return run_extractor(
        input_path=FIXTURE_SOLUTION,
        output_dir=output,
        repo_id="sample",
        scan_id="test-scan-primary",
    )


@pytest.fixture(scope="session")
def scan_all_tfms(
    tmp_path_factory: pytest.TempPathFactory, fixture_built: None
) -> ExtractionResult:
    """A scan with every target framework, to exercise multi-targeting."""
    output = tmp_path_factory.mktemp("scan-all-tfms")
    return run_extractor(
        input_path=FIXTURE_SOLUTION,
        output_dir=output,
        repo_id="sample",
        scan_id="test-scan-all",
        all_target_frameworks=True,
    )


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


@pytest.fixture(scope="session")
def entities(scan: ExtractionResult) -> dict[str, dict]:
    return {e["id"]: e for e in read_jsonl(scan.output_dir / "entities.jsonl")}


@pytest.fixture(scope="session")
def relations(scan: ExtractionResult) -> list[dict]:
    return read_jsonl(scan.output_dir / "relations.jsonl")


@pytest.fixture(scope="session")
def compilations(scan: ExtractionResult) -> list[dict]:
    return read_jsonl(scan.output_dir / "compilations.jsonl")


@pytest.fixture
def jsonl_reader() -> Iterator[object]:
    yield read_jsonl
