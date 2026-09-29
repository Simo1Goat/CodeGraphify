"""One command to scan a .NET codebase and load the graph into Neo4j.

    python -m app.scan --input /path/to/Your.sln

Runs the preflight probe, then the Roslyn extractor, then the Neo4j writer, and
reports what each step decided. Whether the stale sweep runs is derived from the
scan's analysis mode -- never asserted here -- because a sweep on a scan that did
not earn one deletes real data.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from app.extractor_runner import ExtractorNotFoundError, Outcome, run_extractor
from app.preflight import Verdict, probe_repository

logger = logging.getLogger("scan")

EXIT_OK = 0
EXIT_UNUSABLE = 1
EXIT_SETUP = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scan",
        description="Scan a .NET codebase into a dependency graph and load it into Neo4j.",
    )
    parser.add_argument(
        "--input", required=True, help="Solution (.sln), project (.csproj) or directory."
    )
    parser.add_argument(
        "--repo-id",
        help="Repository identity in the graph. Defaults to the solution's directory name.",
    )
    parser.add_argument("--scan-id", help="Scan identity. Defaults to a fresh UUID.")
    parser.add_argument(
        "--repo-root",
        help="Root that emitted paths are relative to. Defaults to the input's git root.",
    )
    parser.add_argument(
        "--output", help="Where the JSONL and manifest go. Defaults to out/<repo-id>/<scan-id>."
    )
    parser.add_argument("--configuration", default="Debug", help="Debug (default) or Release.")
    parser.add_argument(
        "--all-target-frameworks",
        action="store_true",
        help="Analyze every target framework instead of one primary variant per project.",
    )
    parser.add_argument(
        "--no-ingest", action="store_true", help="Extract only; do not touch Neo4j."
    )
    parser.add_argument(
        "--no-sweep",
        action="store_true",
        help="Ingest without reconciling, even if the scan qualifies.",
    )
    parser.add_argument(
        "--timeout", type=float, default=3600.0, help="Seconds before the extractor is killed."
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the final summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if arguments.quiet else logging.INFO,
        format="%(message)s",
    )
    # This module reports every step itself; the writer logging the same lines again
    # would double every count and make a run look like it ran twice.
    logging.getLogger("app.neo4j_writer.writer").setLevel(logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    # Without this, our stdout is block-buffered when piped while the driver writes to
    # stderr unbuffered, and the two arrive out of order.
    sys.stdout.reconfigure(line_buffering=True)

    target = Path(arguments.input).expanduser().resolve()
    if not target.exists():
        print(f"error: {target} does not exist", file=sys.stderr)
        return EXIT_SETUP

    repo_root = (
        Path(arguments.repo_root).expanduser().resolve()
        if arguments.repo_root
        else (target if target.is_dir() else target.parent)
    )
    repo_id = arguments.repo_id or repo_root.name
    scan_id = arguments.scan_id or uuid.uuid4().hex

    output = (
        Path(arguments.output).expanduser().resolve()
        if arguments.output
        else Path(__file__).resolve().parent.parent / "out" / repo_id / scan_id
    )

    # 1. Preflight. Costs milliseconds and needs no SDK, and it is the only thing
    #    that can say "this host cannot analyze this codebase" before the expensive
    #    part starts.
    print(f"==> preflight: {repo_root}")
    probe = probe_repository(repo_root)
    print(probe.summary())

    if probe.verdict is Verdict.NEEDS_WINDOWS:
        print(
            "\nwarning: every project here needs a Windows host, so this scan will be "
            "degraded and will not reconcile the graph. Continuing anyway.",
            file=sys.stderr,
        )
    elif probe.verdict is Verdict.MIXED:
        print(
            "\nwarning: some projects need a Windows host and will be excluded.",
            file=sys.stderr,
        )

    if probe.restore_needed:
        print(
            f"\nwarning: {len(probe.restore_needed)} project(s) are not restored. "
            "Run 'dotnet restore' or the scan will be partial.",
            file=sys.stderr,
        )

    # 2. Extract. The subprocess writes JSONL plus a manifest; this returns the
    #    manifest and the decision about what may be done with it.
    print(f"\n==> extracting to {output}")
    try:
        result = run_extractor(
            input_path=target,
            output_dir=output,
            repo_id=repo_id,
            scan_id=scan_id,
            repo_root=repo_root,
            configuration=arguments.configuration,
            all_target_frameworks=arguments.all_target_frameworks,
            timeout_seconds=arguments.timeout,
        )
    except ExtractorNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_SETUP

    if result.manifest is None:
        print(f"error: the extractor produced no manifest ({result.error})", file=sys.stderr)
        if result.stderr:
            print(result.stderr[-2000:], file=sys.stderr)
        return EXIT_UNUSABLE

    manifest = result.manifest
    print(f"    mode      : {manifest.analysis_mode}")
    print(f"    outcome   : {result.outcome}")
    print(f"    analyzed  : {len(manifest.analyzed_project_keys)} project(s)")
    if manifest.failed_project_keys:
        print(f"    failed    : {', '.join(manifest.failed_project_keys)}")
    print(f"    counts    : {manifest.counts}")

    if result.outcome is Outcome.FALL_BACK:
        print(
            "\nThis scan produced nothing usable. A tree-sitter fallback would run here, "
            "but it lives in the backend repository and is not wired up yet.",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE

    if arguments.no_ingest:
        print(f"\nExtract only. Graph payload is in {output}")
        return EXIT_OK

    # 3. Ingest. Imported here so --no-ingest works without a database or driver.
    print("\n==> ingesting into Neo4j")
    from app.neo4j_writer import Neo4jWriter

    try:
        with Neo4jWriter.connect() as writer:
            # Constraints and indexes first: MERGE without them is a full label scan
            # per row.
            writer.apply_schema()
            report = writer.ingest(result, sweep=False if arguments.no_sweep else None)
    except Exception as error:  # noqa: BLE001 - the driver raises many types; all mean
        # the same thing to a caller: the database is not usable right now.
        print(f"error: could not ingest ({type(error).__name__}: {error})", file=sys.stderr)
        print(
            "Is Neo4j running? 'docker compose up -d' in the repository root.",
            file=sys.stderr,
        )
        return EXIT_SETUP

    print(report.summary())
    print("\nDone. Browse the graph at http://localhost:7474 (neo4j / codegraphify)")
    print(f"Graph payload kept in {output}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
