"""The seam where the existing tree-sitter analyzer plugs in.

The tree-sitter engine lives in a different repository, so this module declares
what it must look like rather than importing it. Nothing here knows anything about
tree-sitter; it knows only that some analyzer can produce the same graph contract
with weaker guarantees.

Declaring the seam as a Protocol means:

- this repository has no dependency on the other one, so it can be developed and
  tested on its own;
- the merge is an adapter, not a rewrite: whatever the other repo already calls
  its parser gets wrapped to satisfy ``FallbackAnalyzer``;
- the fallback path is testable now, with a stub, instead of being speculative
  code that first runs on the day the repos meet.

Roslyn stays authoritative. This runs only when Roslyn produced nothing usable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.contract import AnalysisMode, Manifest


@dataclass(frozen=True)
class FallbackRequest:
    """Everything an analyzer needs to produce a payload for one scan."""

    repo_root: Path
    repo_id: str
    scan_id: str
    output_dir: Path
    """Where to write the JSONL shards and manifest.json, same layout as the
    Roslyn extractor's output."""

    project_keys: tuple[str, ...] = ()
    """Restrict the work to these projects when non-empty. Set when Roslyn handled
    part of the solution and only the rest needs covering."""

    reason: str = ""
    """Why the fallback was invoked, for logging and for the scan record."""


@runtime_checkable
class FallbackAnalyzer(Protocol):
    """What the tree-sitter side must provide.

    One method, because the contract between engines is the payload on disk, not
    an object model. An implementation must:

    1. write ``entities.jsonl``, ``relations.jsonl`` and ``manifest.json`` into
       ``request.output_dir``, matching ``app.contract``;
    2. set ``analysis_mode`` to ``syntax-only`` -- it has no semantic model, so it
       may never claim ``semantic`` or ``partial-semantic``;
    3. set ``resolution_engine`` to ``tree-sitter`` on the manifest and on every
       relation, so a mixed graph stays attributable;
    4. record uncertainty rather than guessing: an unresolvable call becomes an
       ``UnresolvedReference`` with source evidence, never a fabricated edge.

    Returning ``None`` means the fallback could not produce anything either, and
    the scan has genuinely failed.
    """

    def analyze(self, request: FallbackRequest) -> Manifest | None: ...


class NoFallbackAvailable:
    """Stands in until the other repository is merged.

    Deliberately explicit rather than silently doing nothing: a scan that needed
    the fallback and did not get one is a real gap, and the scan record should say
    so instead of looking like a clean failure.
    """

    reason = "tree-sitter fallback is not wired up in this repository yet"

    def analyze(self, request: FallbackRequest) -> Manifest | None:
        return None


def validate_fallback_manifest(manifest: Manifest) -> list[str]:
    """Check a fallback's output against the promises above.

    Returns the problems found, empty when the manifest is acceptable. Worth
    enforcing rather than trusting: a fallback that claimed ``semantic`` would
    earn a stale sweep it cannot justify, and delete the graph.
    """
    problems: list[str] = []

    if manifest.analysis_mode not in {AnalysisMode.SYNTAX_ONLY, AnalysisMode.FAILED}:
        problems.append(
            f"a syntax-only engine may not report analysis_mode={manifest.analysis_mode!r}; "
            "only 'syntax-only' or 'failed' are permitted"
        )

    if manifest.resolution_engine == "roslyn":
        problems.append("resolution_engine must identify the fallback engine, not 'roslyn'")

    if not manifest.resolution_engine:
        problems.append("resolution_engine is required so mixed graphs stay attributable")

    return problems
