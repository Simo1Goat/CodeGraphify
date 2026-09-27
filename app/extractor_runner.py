"""The Python half of the subprocess contract.

Runs the compiled Roslyn extractor as a child process and decides, from its exit
code and the analysis mode in its manifest, what the caller may do with the
result: ingest and sweep, ingest without sweeping, or fall back to tree-sitter.

The decision table is context section 10. It lives here, in one function, so no
caller can accidentally sweep on a partial scan.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.contract import AnalysisMode, Manifest

logger = logging.getLogger(__name__)

# Exit codes the extractor uses; mirrors Program.cs.
EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_NO_MSBUILD = 3
EXIT_FAILED = 4


class Outcome(StrEnum):
    """What the caller should do next."""

    INGEST_AND_SWEEP = "ingest-and-sweep"
    INGEST_NO_SWEEP = "ingest-no-sweep"
    INGEST_REDUCED_TRUST = "ingest-reduced-trust"
    FALL_BACK = "fall-back"


@dataclass(frozen=True)
class ExtractionResult:
    outcome: Outcome
    exit_code: int
    manifest: Manifest | None
    output_dir: Path
    stderr: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not Outcome.FALL_BACK

    @property
    def may_sweep(self) -> bool:
        """A stale sweep deletes nodes. Only ever true for a complete scan."""
        return self.outcome is Outcome.INGEST_AND_SWEEP

    def entity_file(self) -> Path | None:
        return self._file_of_kind("entities")

    def relation_file(self) -> Path | None:
        return self._file_of_kind("relations")

    def _file_of_kind(self, kind: str) -> Path | None:
        if self.manifest is None:
            return None
        for output_file in self.manifest.output_files:
            if output_file.kind == kind:
                return self.output_dir / output_file.path
        return None


class ExtractorNotFoundError(RuntimeError):
    pass


def default_extractor_path() -> Path:
    """Where the extractor binary is expected.

    CODEGRAPHIFY_EXTRACTOR wins, so a deployment can point at a published
    single-file build without changing code.
    """
    override = os.environ.get("CODEGRAPHIFY_EXTRACTOR")
    if override:
        return Path(override)

    repo_root = Path(__file__).resolve().parent.parent
    return (
        repo_root
        / "extractor"
        / "src"
        / "CodeGraphify.Extractor"
        / "bin"
        / "Debug"
        / "net8.0"
        / "codegraphify-extractor"
    )


def run_extractor(
    *,
    input_path: Path | str,
    output_dir: Path | str,
    repo_id: str,
    scan_id: str | None = None,
    repo_root: Path | str | None = None,
    configuration: str = "Debug",
    all_target_frameworks: bool = False,
    tfm_preference: Sequence[str] | None = None,
    timeout_seconds: float = 3600.0,
    extractor_path: Path | str | None = None,
    extra_env: dict[str, str] | None = None,
) -> ExtractionResult:
    """Run one extraction and classify the result.

    Never raises on a failed scan: a failure is an ExtractionResult with
    Outcome.FALL_BACK, because the caller's next step is the tree-sitter
    fallback, not an exception.
    """
    executable = Path(extractor_path) if extractor_path else default_extractor_path()
    if not executable.exists():
        raise ExtractorNotFoundError(
            f"Extractor not found at {executable}. Build it with "
            "'dotnet build extractor/src/CodeGraphify.Extractor' or set "
            "CODEGRAPHIFY_EXTRACTOR."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    scan_id = scan_id or uuid.uuid4().hex

    command = [
        str(executable),
        "--input",
        str(input_path),
        "--output",
        str(output_dir),
        "--repo-id",
        repo_id,
        "--scan-id",
        scan_id,
        "--configuration",
        configuration,
    ]
    if repo_root is not None:
        command += ["--repo-root", str(repo_root)]
    if all_target_frameworks:
        command.append("--all-target-frameworks")
    if tfm_preference:
        command += ["--tfm-preference", ",".join(tfm_preference)]

    env = _build_env(extra_env)

    logger.info("running extractor: scan_id=%s input=%s", scan_id, input_path)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A timeout on a giant solution is a failure of this engine, not of the
        # request: the caller falls back rather than surfacing an error.
        logger.warning("extractor timed out after %ss", timeout_seconds)
        return ExtractionResult(
            outcome=Outcome.FALL_BACK,
            exit_code=-1,
            manifest=None,
            output_dir=output_dir,
            stderr=_as_text(exc.stderr),
            error=f"Extractor exceeded {timeout_seconds}s and was killed.",
        )

    manifest, parse_error = _read_manifest(completed.stdout, output_dir)
    outcome = _classify(completed.returncode, manifest)

    if outcome is Outcome.FALL_BACK:
        logger.warning(
            "extraction unusable: exit=%s mode=%s",
            completed.returncode,
            manifest.analysis_mode if manifest else "no-manifest",
        )

    return ExtractionResult(
        outcome=outcome,
        exit_code=completed.returncode,
        manifest=manifest,
        output_dir=output_dir,
        stderr=completed.stderr,
        error=parse_error or _first_error(manifest),
    )


def _classify(exit_code: int, manifest: Manifest | None) -> Outcome:
    """Context section 10, as one table.

    Anything unrecognized falls back. Guessing in favour of ingesting would risk
    a sweep against an incomplete graph.
    """
    if manifest is None:
        return Outcome.FALL_BACK

    if exit_code != EXIT_SUCCESS:
        return Outcome.FALL_BACK

    match manifest.analysis_mode:
        case AnalysisMode.SEMANTIC:
            # A sweep is only safe when nothing was skipped. The extractor
            # already refuses to call a scan semantic with failed projects; this
            # re-checks it, because the cost of being wrong is deleted nodes.
            if manifest.failed_project_keys:
                return Outcome.INGEST_NO_SWEEP
            return Outcome.INGEST_AND_SWEEP
        case AnalysisMode.PARTIAL_SEMANTIC:
            return Outcome.INGEST_NO_SWEEP
        case AnalysisMode.SYNTAX_ONLY:
            return Outcome.INGEST_REDUCED_TRUST
        case _:
            return Outcome.FALL_BACK


def _read_manifest(stdout: str, output_dir: Path) -> tuple[Manifest | None, str | None]:
    """Prefer the manifest file; fall back to stdout.

    The file is preferred because stdout can pick up stray output from MSBuild or
    the runtime, while the file is written by the extractor alone.
    """
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        try:
            return Manifest.model_validate_json(manifest_path.read_text()), None
        except (ValueError, OSError) as exc:
            manifest_error = f"manifest.json is not a valid manifest: {exc}"
    else:
        manifest_error = "manifest.json was not written."

    stripped = stdout.strip()
    if stripped:
        try:
            return Manifest.model_validate_json(stripped), None
        except ValueError:
            try:
                # Tolerate leading noise on stdout by taking the last JSON object.
                start = stripped.index("{")
                return Manifest.model_validate(json.loads(stripped[start:])), None
            except (ValueError, KeyError):
                pass

    return None, manifest_error


def _as_text(output: str | bytes | None) -> str:
    """Normalize captured output. TimeoutExpired carries whatever was buffered
    when the child was killed, which may be bytes even in text mode."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _first_error(manifest: Manifest | None) -> str | None:
    if manifest is None or not manifest.errors:
        return None
    return manifest.errors[0]


def _build_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DOTNET_NOLOGO", "1")
    env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")

    # MSBuildWorkspace needs the SDK on PATH. A locally installed SDK
    # (scripts/dotnet-env.sh) is not on a service's PATH by default.
    dotnet_root = env.get("DOTNET_ROOT") or str(Path.home() / ".dotnet")
    if Path(dotnet_root).is_dir() and shutil.which("dotnet", path=env.get("PATH", "")) is None:
        env["DOTNET_ROOT"] = dotnet_root
        env["PATH"] = os.pathsep.join([dotnet_root, env.get("PATH", "")])

    if extra_env:
        env.update(extra_env)
    return env
