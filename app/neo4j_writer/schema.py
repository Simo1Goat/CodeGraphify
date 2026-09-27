"""Labels, constraints and indexes the writer depends on.

Applied before any load. MERGE against an unindexed property degrades to a full
label scan per row, which on a heavy repo is the difference between minutes and
never finishing -- context section 9 calls this out for exactly that reason.

Labelling
---------
Every node carries ``:CodeEntity`` plus a label for its kind (``:Method``,
``:Class``). The shared label is what the uniqueness constraint and the stale
sweep work against, so both are a single indexed operation; the kind label is what
makes the graph readable in the browser and lets a query narrow cheaply.

Neo4j 5.26 cannot parameterise a label or a relationship type, so kinds are
interpolated into Cypher. Every interpolated name is checked against the contract
enums first -- see ``safe_label`` and ``safe_relationship_type``. Nothing else may
build a statement from a kind.
"""

from __future__ import annotations

from app.contract import EntityKind, RelationKind

#: Label every node carries, alongside its kind label.
BASE_LABEL = "CodeEntity"

#: Label for the record of a scan itself.
SCAN_LABEL = "Scan"

_VALID_LABELS = frozenset(kind.value for kind in EntityKind) | {BASE_LABEL, SCAN_LABEL}
_VALID_RELATIONSHIP_TYPES = frozenset(kind.value for kind in RelationKind)


class UnknownGraphLabelError(ValueError):
    """A kind that is not in the contract reached a Cypher statement."""


def safe_label(kind: str) -> str:
    """Validate a node label before it is interpolated into Cypher.

    The allow-list is the contract's own enum, so an engine inventing a kind is
    rejected here rather than becoming an arbitrary Cypher fragment.
    """
    if kind not in _VALID_LABELS:
        raise UnknownGraphLabelError(
            f"{kind!r} is not a known entity kind; refusing to build a query from it"
        )
    return kind


def safe_relationship_type(kind: str) -> str:
    """Validate a relationship type before it is interpolated into Cypher."""
    if kind not in _VALID_RELATIONSHIP_TYPES:
        raise UnknownGraphLabelError(
            f"{kind!r} is not a known relation kind; refusing to build a query from it"
        )
    return kind


def node_statements() -> list[str]:
    """Constraints and indexes for nodes."""
    return [
        # The node key. Uniqueness implies an index, which is what makes MERGE fast.
        (
            f"CREATE CONSTRAINT code_entity_id IF NOT EXISTS "
            f"FOR (n:{BASE_LABEL}) REQUIRE n.id IS UNIQUE"
        ),
        # The stale sweep filters on exactly this pair, and it is the one query in
        # the system that deletes, so it should never be a full scan.
        (
            f"CREATE INDEX code_entity_scope IF NOT EXISTS "
            f"FOR (n:{BASE_LABEL}) ON (n.project_key, n.last_seen_scan_id)"
        ),
        # Humans and tools look symbols up by name, not by hash.
        (
            f"CREATE INDEX code_entity_canonical_name IF NOT EXISTS "
            f"FOR (n:{BASE_LABEL}) ON (n.canonical_name)"
        ),
        (
            f"CREATE INDEX code_entity_kind IF NOT EXISTS "
            f"FOR (n:{BASE_LABEL}) ON (n.kind)"
        ),
        (
            f"CREATE CONSTRAINT scan_id IF NOT EXISTS "
            f"FOR (s:{SCAN_LABEL}) REQUIRE s.scan_id IS UNIQUE"
        ),
    ]


def relationship_statements() -> list[str]:
    """Indexes for relationships, one set per type.

    Relationship uniqueness constraints are an Enterprise feature, so identity is
    enforced by MERGE on an ``edge_key`` computed by the writer, and these indexes
    are what stop that MERGE scanning every relationship of a node.
    """
    statements = []
    for kind in RelationKind:
        relationship_type = safe_relationship_type(kind.value)
        suffix = relationship_type.lower()
        statements.append(
            f"CREATE INDEX rel_{suffix}_edge_key IF NOT EXISTS "
            f"FOR ()-[r:{relationship_type}]-() ON (r.edge_key)"
        )
        statements.append(
            f"CREATE INDEX rel_{suffix}_scope IF NOT EXISTS "
            f"FOR ()-[r:{relationship_type}]-() ON (r.project_key, r.last_seen_scan_id)"
        )
    return statements


def all_statements() -> list[str]:
    return node_statements() + relationship_statements()
