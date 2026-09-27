"""Loading a scan's graph into Neo4j, and reconciling what is no longer there.

Ingest is streamed and batched: a heavy repo's JSONL is read a line at a time and
sent in batches, so neither this process nor the driver holds the whole graph.

The stale sweep is the only destructive operation in the system, so it is guarded
rather than merely documented. It runs only for a complete semantic scan, only
within the projects that scan actually analyzed, and asking for it on any other
kind of scan raises instead of proceeding -- a transient build break must not be
able to wipe the graph (context section 4, Stage 4).
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from neo4j import Driver, GraphDatabase

from app.extractor_runner import ExtractionResult
from app.neo4j_writer import rows
from app.neo4j_writer.schema import (
    BASE_LABEL,
    SCAN_LABEL,
    all_statements,
    safe_label,
    safe_relationship_type,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 1000

#: Cap on rows deleted per transaction, so a sweep of a large stale set does not
#: build one enormous transaction.
DELETE_BATCH_SIZE = 5000


class SweepRefusedError(RuntimeError):
    """A sweep was requested for a scan that has not earned one."""


@dataclass
class IngestReport:
    scan_id: str
    nodes_written: int = 0
    relationships_written: int = 0
    relationships_skipped: int = 0
    nodes_deleted: int = 0
    relationships_deleted: int = 0
    swept: bool = False
    sweep_scope: tuple[str, ...] = ()
    depends_on_derived: int = 0
    skipped_kinds: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            (
                f"scan {self.scan_id}: "
                f"{self.nodes_written} nodes, {self.relationships_written} relationships"
            ),
        ]
        if self.relationships_skipped:
            lines.append(
                f"  {self.relationships_skipped} relationships skipped "
                f"(endpoint not in graph): {self.skipped_kinds}"
            )
        if self.swept:
            lines.append(
                f"  swept {self.nodes_deleted} nodes and "
                f"{self.relationships_deleted} relationships "
                f"across {len(self.sweep_scope)} project(s)"
            )
        else:
            lines.append("  no sweep (scan did not qualify)")
        if self.depends_on_derived:
            lines.append(f"  derived {self.depends_on_derived} DEPENDS_ON edge(s)")
        return "\n".join(lines)


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "codegraphify"
    database: str = "neo4j"

    @classmethod
    def from_env(cls) -> Neo4jSettings:
        return cls(
            uri=os.environ.get("NEO4J_URI", cls.uri),
            user=os.environ.get("NEO4J_USER", cls.user),
            password=os.environ.get("NEO4J_PASSWORD", cls.password),
            database=os.environ.get("NEO4J_DATABASE", cls.database),
        )


class Neo4jWriter:
    """Writes one scan's graph. Not thread-safe; use one per scan."""

    def __init__(
        self,
        driver: Driver,
        *,
        database: str = "neo4j",
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._driver = driver
        self._database = database
        self._batch_size = batch_size

    @classmethod
    def connect(cls, settings: Neo4jSettings | None = None, **kwargs: Any) -> Neo4jWriter:
        settings = settings or Neo4jSettings.from_env()
        driver = GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password))
        return cls(driver, database=settings.database, **kwargs)

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- schema --------------------------------------------------------------

    def apply_schema(self) -> None:
        """Create constraints and indexes. Idempotent, and required before a load:
        MERGE without the uniqueness constraint's index is a full label scan per
        row."""
        with self._driver.session(database=self._database) as session:
            for statement in all_statements():
                session.run(statement)
        logger.info("schema applied")

    # --- ingest --------------------------------------------------------------

    def ingest(self, result: ExtractionResult, *, sweep: bool | None = None) -> IngestReport:
        """Load a scan, then reconcile if it qualifies.

        ``sweep`` defaults to what the scan's analysis mode allows. Passing ``True``
        for a scan that does not qualify raises: the guard exists precisely for the
        caller who is sure, because being wrong deletes real data.
        """
        if result.manifest is None:
            raise ValueError("cannot ingest a scan with no manifest")

        manifest = result.manifest

        if sweep is None:
            sweep = result.may_sweep
        elif sweep and not result.may_sweep:
            raise SweepRefusedError(
                f"scan {manifest.scan_id} reported analysis_mode="
                f"{manifest.analysis_mode!r} with "
                f"{len(manifest.failed_project_keys)} failed project(s); a sweep would "
                "delete nodes this scan could not observe. Refusing."
            )

        report = IngestReport(scan_id=manifest.scan_id)

        entities_file = result.entity_file()
        if entities_file is not None and entities_file.exists():
            report.nodes_written = self._ingest_entities(entities_file, manifest.scan_id)

        relations_file = result.relation_file()
        if relations_file is not None and relations_file.exists():
            written, skipped, by_kind = self._ingest_relations(relations_file, manifest.scan_id)
            report.relationships_written = written
            report.relationships_skipped = skipped
            report.skipped_kinds = by_kind

        self._record_scan(result, report)

        if sweep:
            scope = tuple(manifest.analyzed_project_keys)
            if not scope:
                # Nothing was analyzed, so nothing can be judged stale.
                logger.warning("sweep skipped: scan reported no analyzed projects")
            else:
                nodes, relationships = self.sweep(manifest.scan_id, scope)
                report.swept = True
                report.sweep_scope = scope
                report.nodes_deleted = nodes
                report.relationships_deleted = relationships

        # After the sweep, so the roll-up counts what survived rather than what was
        # about to be deleted.
        report.depends_on_derived = self.derive_depends_on(
            manifest.scan_id,
            tuple(manifest.analyzed_project_keys),
            prune=report.swept,
        )

        logger.info("%s", report.summary())
        return report

    def _ingest_entities(self, path: Path, scan_id: str) -> int:
        written = 0
        for kind, batch in self._batched_by_kind(path, scan_id, rows.entity_row):
            written += self._write_nodes(kind, batch)
        return written

    def _write_nodes(self, kind: str, batch: list[dict[str, Any]]) -> int:
        # Kind is interpolated because Neo4j 5 cannot parameterise a label; it is
        # validated against the contract enum first.
        label = safe_label(kind)
        query = (
            f"UNWIND $rows AS row "
            f"MERGE (n:{BASE_LABEL} {{id: row.id}}) "
            f"SET n:{label}, n += row.props "
            # Kept so the age of a node is visible; only set when absent.
            f"SET n.first_seen_scan_id = coalesce(n.first_seen_scan_id, row.props.last_seen_scan_id)"
        )
        with self._driver.session(database=self._database) as session:
            session.run(query, rows=batch).consume()
        return len(batch)

    def _ingest_relations(self, path: Path, scan_id: str) -> tuple[int, int, dict[str, int]]:
        written = 0
        skipped = 0
        skipped_kinds: dict[str, int] = defaultdict(int)

        for kind, batch in self._batched_by_kind(path, scan_id, rows.relation_row):
            batch_skipped = self._write_relationships(kind, batch)
            written += len(batch) - batch_skipped
            skipped += batch_skipped
            if batch_skipped:
                skipped_kinds[kind] += batch_skipped

        return written, skipped, dict(skipped_kinds)

    def _write_relationships(self, kind: str, batch: list[dict[str, Any]]) -> int:
        relationship_type = safe_relationship_type(kind)
        # OPTIONAL MATCH plus FOREACH rather than MATCH: a missing endpoint must be
        # counted and reported, not silently drop the row. MERGE would be worse
        # still -- it would invent a blank node for the missing endpoint.
        query = (
            f"UNWIND $rows AS row "
            f"OPTIONAL MATCH (source:{BASE_LABEL} {{id: row.source}}) "
            f"OPTIONAL MATCH (target:{BASE_LABEL} {{id: row.target}}) "
            f"FOREACH (_ IN CASE WHEN source IS NULL OR target IS NULL THEN [] ELSE [1] END | "
            f"  MERGE (source)-[r:{relationship_type} {{edge_key: row.edge_key}}]->(target) "
            f"  SET r += row.props "
            f") "
            f"RETURN sum(CASE WHEN source IS NULL OR target IS NULL THEN 1 ELSE 0 END) AS skipped"
        )
        with self._driver.session(database=self._database) as session:
            record = session.run(query, rows=batch).single()
        skipped = int(record["skipped"]) if record else 0
        if skipped:
            logger.warning("%s: %d relationship(s) had a missing endpoint", kind, skipped)
        return skipped

    def _batched_by_kind(
        self,
        path: Path,
        scan_id: str,
        to_row: Any,
    ) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        """Stream JSONL, grouping into per-kind batches.

        Grouped by kind because each kind needs its own statement, and batched so a
        repo with millions of rows never has more than ``batch_size`` in memory at a
        time.
        """
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        pending = 0

        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                buckets[record["kind"]].append(to_row(record, scan_id))
                pending += 1

                if pending >= self._batch_size:
                    yield from self._flush(buckets)
                    pending = 0

        yield from self._flush(buckets)

    @staticmethod
    def _flush(
        buckets: dict[str, list[dict[str, Any]]],
    ) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        for kind, batch in list(buckets.items()):
            if batch:
                yield kind, batch
        buckets.clear()

    # --- scan record ---------------------------------------------------------

    def _record_scan(self, result: ExtractionResult, report: IngestReport) -> None:
        """A node per scan, so a failed or partial scan leaves a trace.

        Without this, "why is the graph missing a project" has no answer after the
        fact: the manifest lives in a temp directory that is gone by then.
        """
        manifest = result.manifest
        assert manifest is not None

        properties = {
            "scan_id": manifest.scan_id,
            "repo_id": manifest.repo_id,
            "analysis_mode": str(manifest.analysis_mode),
            "resolution_engine": manifest.resolution_engine,
            "outcome": str(result.outcome),
            "started_at": manifest.started_at,
            "finished_at": manifest.finished_at,
            "analyzed_project_keys": list(manifest.analyzed_project_keys),
            "failed_project_keys": list(manifest.failed_project_keys),
            "exit_code": result.exit_code,
            "nodes_written": report.nodes_written,
            "relationships_written": report.relationships_written,
            "relationships_skipped": report.relationships_skipped,
        }
        for key, value in manifest.counts.items():
            properties[f"count_{key}"] = value

        with self._driver.session(database=self._database) as session:
            session.run(
                f"MERGE (s:{SCAN_LABEL} {{scan_id: $scan_id}}) SET s += $props",
                scan_id=manifest.scan_id,
                props=properties,
            ).consume()

    # --- derived edges -------------------------------------------------------

    #: Edge types that mean "this depends on that". Structural containment
    #: (CONTAINS, DECLARES, COMPILES) is excluded: a namespace holding a type is not
    #: a dependency, and including it would make every project depend on itself.
    DEPENDENCY_EDGE_TYPES = (
        "CALLS_DIRECT",
        "CALLS_VIRTUAL",
        "CALLS_INTERFACE",
        "CALLS_UNRESOLVED",
        "CREATES",
        "INHERITS",
        "IMPLEMENTS",
        "IMPLEMENTS_MEMBER",
        "OVERRIDES",
        "RESOLVES_TO",
    )

    def derive_depends_on(
        self, scan_id: str, scope: tuple[str, ...] = (), *, prune: bool = False
    ) -> int:
        """Roll up fine-grained edges into project-level DEPENDS_ON.

        Derived in the database rather than emitted per call, as context section 7
        asks: emitting it per call site would duplicate the entire call graph at a
        coarser grain, and the roll-up is a single aggregation over what is already
        there.

        Runs after the sweep, so it reflects the reconciled graph rather than a
        pre-sweep snapshot that still counts deleted calls.
        """
        types = "|".join(safe_relationship_type(t) for t in self.DEPENDENCY_EDGE_TYPES)
        query = (
            f"MATCH (source:{BASE_LABEL})-[edge:{types}]->(target:{BASE_LABEL}) "
            f"WHERE source.project_key IS NOT NULL AND target.project_key IS NOT NULL "
            f"  AND source.project_key <> target.project_key "
            f"WITH source.project_key AS source_key, target.project_key AS target_key, "
            f"     count(*) AS weight "
            f"MATCH (source_project:Project {{canonical_name: source_key}}) "
            f"MATCH (target_project:Project {{canonical_name: target_key}}) "
            f"MERGE (source_project)-[dependency:DEPENDS_ON]->(target_project) "
            f"SET dependency.weight = weight, "
            f"    dependency.derived = true, "
            f"    dependency.project_key = source_key, "
            f"    dependency.last_seen_scan_id = $scan_id, "
            f"    dependency.edge_key = source_key + '->' + target_key "
            f"RETURN count(*) AS derived"
        )
        with self._driver.session(database=self._database) as session:
            record = session.run(query, scan_id=scan_id).single()
            derived = int(record["derived"]) if record else 0

            # A dependency that no longer exists leaves an edge MERGE cannot reach,
            # so the derivation reconciles its own output. Only when the scan earned
            # a sweep: otherwise a project it failed to analyze would lose a
            # dependency that is still perfectly real.
            if prune and scope:
                session.run(
                    "MATCH (:Project)-[d:DEPENDS_ON]->(:Project) "
                    "WHERE d.project_key IN $keys AND d.last_seen_scan_id <> $scan_id "
                    "DELETE d",
                    keys=list(scope),
                    scan_id=scan_id,
                ).consume()

        logger.info("derived %d DEPENDS_ON edge(s)", derived)
        return derived

    # --- stale sweep ---------------------------------------------------------

    def sweep(self, scan_id: str, project_keys: tuple[str, ...]) -> tuple[int, int]:
        """Delete what this scan did not observe, within the given projects.

        Scoped rather than global: a scan of one project must not delete another
        project's graph, and a partial scan must not reach this code at all.

        Relationships are swept as well as nodes, not only via DETACH DELETE: the
        common edit removes a call while leaving both methods in place, so the edge
        is stale while its endpoints are current.

        Nodes with no ``project_key`` -- repository, files, namespaces, referenced
        assemblies -- are shared across projects and are left alone. They are few,
        and deciding one is stale would need to prove no other project still uses it.
        """
        if not project_keys:
            raise ValueError("a sweep must be scoped to at least one project")

        relationships_deleted = 0
        for relationship_type in self._relationship_types():
            relationships_deleted += self._delete_in_batches(
                f"MATCH ()-[r:{relationship_type}]->() "
                f"WHERE r.project_key IN $keys AND r.last_seen_scan_id <> $scan_id "
                f"WITH r LIMIT $limit "
                f"DELETE r "
                f"RETURN count(*) AS deleted",
                scan_id=scan_id,
                keys=list(project_keys),
            )

        nodes_deleted = self._delete_in_batches(
            f"MATCH (n:{BASE_LABEL}) "
            f"WHERE n.project_key IN $keys AND n.last_seen_scan_id <> $scan_id "
            f"WITH n LIMIT $limit "
            f"DETACH DELETE n "
            f"RETURN count(*) AS deleted",
            scan_id=scan_id,
            keys=list(project_keys),
        )

        logger.info(
            "swept %d nodes and %d relationships in %d project(s)",
            nodes_deleted,
            relationships_deleted,
            len(project_keys),
        )
        return nodes_deleted, relationships_deleted

    @staticmethod
    def _relationship_types() -> list[str]:
        """Types the observation sweep considers.

        DEPENDS_ON is excluded: it is derived, not observed, so it is stale by scan
        id on every single scan. Sweeping it here would delete and recreate it each
        time, reporting phantom deletions on an unchanged rescan. It reconciles
        itself in ``derive_depends_on`` instead.
        """
        from app.contract import RelationKind

        return [
            safe_relationship_type(kind.value)
            for kind in RelationKind
            if kind is not RelationKind.DEPENDS_ON
        ]

    def _delete_in_batches(self, query: str, **params: Any) -> int:
        """Run a LIMIT-ed delete until it stops deleting.

        Keeps each transaction bounded: a sweep after a large refactor could
        otherwise try to delete millions of rows in one transaction.
        """
        total = 0
        with self._driver.session(database=self._database) as session:
            while True:
                record = session.run(query, limit=DELETE_BATCH_SIZE, **params).single()
                deleted = int(record["deleted"]) if record else 0
                total += deleted
                if deleted < DELETE_BATCH_SIZE:
                    break
        return total
