"""Turning contract rows into Neo4j property maps.

Neo4j properties are flat: a scalar, or a list of scalars. The contract nests
``metadata`` and ``evidence``, so both have to be flattened, and a nested value
that survives flattening would fail the write rather than be stored -- so it is
dropped with a diagnostic instead.
"""

from __future__ import annotations

import hashlib
from typing import Any

#: Properties the writer owns; an engine cannot overwrite them from metadata.
RESERVED = frozenset(
    {
        "id",
        "kind",
        "name",
        "canonical_name",
        "file_path",
        "start_line",
        "start_column",
        "end_line",
        "end_column",
        "edge_key",
        "resolved",
        "last_seen_scan_id",
        "first_seen_scan_id",
    }
)


def _is_storable(value: Any) -> bool:
    """Whether Neo4j can hold this as a property value."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, (list, tuple)):
        # Neo4j lists must be homogeneous scalars; a list of maps is not storable.
        return all(isinstance(item, (str, bool, int, float)) for item in value)
    return False


def flatten_metadata(metadata: dict[str, Any], *, skip: frozenset[str]) -> dict[str, Any]:
    """Metadata as Neo4j properties, dropping anything unstorable.

    Keys the writer owns are skipped rather than allowed to shadow structural
    fields: an engine putting ``file_path`` in metadata must not be able to
    overwrite the evidence span's own file path.
    """
    flattened: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in skip:
            continue
        if not _is_storable(value):
            continue
        if isinstance(value, tuple):
            value = list(value)
        flattened[key] = value
    return flattened


def entity_row(entity: dict[str, Any], scan_id: str) -> dict[str, Any]:
    """One node, as {id, props} ready for UNWIND."""
    properties: dict[str, Any] = flatten_metadata(
        entity.get("metadata") or {}, skip=RESERVED
    )

    properties.update(
        {
            "id": entity["id"],
            "kind": entity["kind"],
            "name": entity["name"],
            "canonical_name": entity["canonical_name"],
            "last_seen_scan_id": scan_id,
        }
    )

    for optional in ("file_path", "start_line", "end_line"):
        if entity.get(optional) is not None:
            properties[optional] = entity[optional]

    return {"id": entity["id"], "props": properties}


def edge_key(relation: dict[str, Any]) -> str:
    """Stable identity for one relationship.

    One edge per call site, so the evidence span is part of the key: the same
    caller calling the same target twice on different lines is two facts, and
    collapsing them would lose the ability to point at either.

    Hashed rather than concatenated so the property stays a fixed size regardless of
    path length, and so it can be indexed cheaply.
    """
    evidence = relation.get("evidence") or {}
    parts = [
        relation["source"],
        relation["target"],
        relation["kind"],
        evidence.get("file_path") or "",
        str(evidence.get("start_line") or ""),
        str(evidence.get("start_column") or ""),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def relation_row(relation: dict[str, Any], scan_id: str) -> dict[str, Any]:
    """One relationship, as {source, target, edge_key, props} ready for UNWIND."""
    properties: dict[str, Any] = flatten_metadata(
        relation.get("metadata") or {}, skip=RESERVED
    )

    # Evidence is applied after metadata so the structural span always wins.
    evidence = relation.get("evidence")
    if evidence:
        for field in ("file_path", "start_line", "start_column", "end_line", "end_column"):
            if evidence.get(field) is not None:
                properties[field] = evidence[field]

    key = edge_key(relation)
    properties.update(
        {
            "edge_key": key,
            "resolved": relation["resolved"],
            "last_seen_scan_id": scan_id,
        }
    )

    return {
        "source": relation["source"],
        "target": relation["target"],
        "edge_key": key,
        "props": properties,
    }
