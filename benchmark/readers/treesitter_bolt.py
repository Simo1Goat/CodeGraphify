"""Read Engine T's graph from its Neo4j.

Read over Bolt rather than through its HTTP graph endpoint, which is hard-capped
at 2000 nodes and so cannot return a whole graph.

Two schema facts drive most of this module:

* **There is no fully-qualified name.** `name` is the bare identifier, so the
  declaring type has to be reconstructed by walking `CONTAINS` upward. That walk
  is not a convenience — "is this member hanging off the right type?" is one of
  the claims being measured.
* **Constructors are `:Method` nodes named after their class.** There is no
  Constructor label and no arity, so they are re-derived by comparing a method's
  name to its enclosing type. That is sound: C# forbids a method named as its
  containing class (CS0542).
"""

from __future__ import annotations

from neo4j import GraphDatabase

from benchmark.config import TREE_SITTER, EngineEndpoint
from benchmark.model import Decl, Edge, EdgeClass, EngineGraph, KindClass

KIND_CLASS = {
    "Namespace": KindClass.NAMESPACE,
    "Class": KindClass.TYPE,
    "Record": KindClass.TYPE,
    "Struct": KindClass.TYPE,
    "Interface": KindClass.TYPE,
    "Enum": KindClass.TYPE,
    "Trait": KindClass.TYPE,
    "Protocol": KindClass.TYPE,
    "Object": KindClass.TYPE,
    "Method": KindClass.CALLABLE,
    "Function": KindClass.CALLABLE,
    "Property": KindClass.PROPERTY,
    "File": KindClass.FILE,
    "External": KindClass.EXTERNAL,
    "Package": KindClass.EXTERNAL,
}

EDGE_CLASS = {
    "CONTAINS": EdgeClass.CONTAINMENT,
    "DEFINES": EdgeClass.CONTAINMENT,
    "INHERITS": EdgeClass.HIERARCHY,
    "IMPLEMENTS": EdgeClass.HIERARCHY,
    "CALLS": EdgeClass.CALL,
    "REFERENCE": EdgeClass.PROJECT_REF,
    "DEPENDS_ON": EdgeClass.PROJECT_REF,
    "INJECTS": EdgeClass.DI,
    "ACCEPTS": EdgeClass.DI,
    "RESOLVES_TO": EdgeClass.DI,
    "REGISTERS": EdgeClass.DI,
    "REGISTERED": EdgeClass.DI,
    "REGISTERED_DB_CONTEXT": EdgeClass.DI,
    "HOSTED_SERVICE": EdgeClass.DI,
    "REMOVED": EdgeClass.DI,
    "REPLACED_BY": EdgeClass.DI,
}

TYPE_LABELS = {"Class", "Interface", "Record", "Struct", "Enum", "Trait", "Protocol", "Object"}

PAGE = 2000


def read(repo_id: str, corpus: str, endpoint: EngineEndpoint = TREE_SITTER) -> EngineGraph:
    driver = GraphDatabase.driver(endpoint.uri, auth=(endpoint.user, endpoint.password))
    try:
        with driver.session(database=endpoint.database) as session:
            nodes = _read_nodes(session, repo_id)
            containment = _read_containment(session, repo_id)
            owners = _reconstruct_owners(nodes, containment)
            decls = _to_decls(nodes, owners)
            edges = _read_edges(session, repo_id, nodes)
    finally:
        driver.close()

    return EngineGraph(engine="tree-sitter", corpus=corpus, decls=decls, edges=edges)


def _read_nodes(session, repo_id: str) -> dict[str, dict]:
    """Keyset pagination on `key`; SKIP degrades quadratically on a large graph."""
    nodes: dict[str, dict] = {}
    cursor = ""
    while True:
        rows = list(session.run(
            """
            MATCH (n {repo_id: $repo})
            WHERE n.key IS NOT NULL AND n.key > $cursor
            RETURN n.key AS key, labels(n) AS labels, n.name AS name, n.path AS path,
                   n.line_start AS line_start, n.line_end AS line_end,
                   n.language AS language, n.inferred AS inferred
            ORDER BY n.key LIMIT $page
            """,
            repo=repo_id, cursor=cursor, page=PAGE,
        ))
        if not rows:
            break
        for row in rows:
            nodes[row["key"]] = dict(row)
        cursor = rows[-1]["key"]
    return nodes


def _read_containment(session, repo_id: str) -> list[tuple[str, str]]:
    return [
        (r["src"], r["dst"])
        for r in session.run(
            """
            MATCH (a {repo_id: $repo})-[:CONTAINS]->(b {repo_id: $repo})
            WHERE a.key IS NOT NULL AND b.key IS NOT NULL
            RETURN a.key AS src, b.key AS dst
            """,
            repo=repo_id,
        )
    ]


def _reconstruct_owners(nodes: dict[str, dict], containment: list[tuple[str, str]]) -> dict[str, str]:
    """Rebuild each declaration's qualified owner, as favourably as the data allows.

    Engine T stores no qualified name, and — measured, not assumed — its Namespace
    nodes are NOT ancestors in the CONTAINS chain: a File contains its types
    directly and the namespace hangs off the file as a sibling. So walking CONTAINS
    yields only the enclosing type names.

    The namespace is therefore recovered from the file the declaration is in. That
    is the most favourable reconstruction its data supports, which is the point: a
    low owner-match rate should reflect the engine's limits, not a lazy reading of
    its output. Where a file declares more than one namespace the recovery is
    ambiguous and is skipped rather than guessed.
    """
    parent = {dst: src for src, dst in containment}

    def label_of(key: str) -> str:
        labels = [l for l in nodes[key]["labels"] if l != "Repo"]
        return labels[0] if labels else ""

    # path -> namespace, only where the file declares exactly one.
    by_path: dict[str, set[str]] = {}
    for node in nodes.values():
        if "Namespace" in node["labels"] and node.get("path"):
            by_path.setdefault(node["path"], set()).add(node["name"])
    namespace_of_path = {path: next(iter(names)) for path, names in by_path.items() if len(names) == 1}

    def owner_of(key: str) -> str | None:
        chain: list[str] = []
        current = parent.get(key)
        seen = set()
        while current is not None and current in nodes and current not in seen:
            seen.add(current)
            if label_of(current) in TYPE_LABELS:
                chain.append(nodes[current]["name"])
            current = parent.get(current)

        namespace = namespace_of_path.get(nodes[key].get("path") or "")
        parts = ([namespace] if namespace else []) + list(reversed(chain))
        return ".".join(parts) if parts else None

    return {key: owner for key in nodes if (owner := owner_of(key)) is not None}


def _to_decls(nodes: dict[str, dict], owners: dict[str, str]) -> list[Decl]:
    decls = []
    for key, node in nodes.items():
        labels = [l for l in node["labels"] if l != "Repo"]
        if not labels:
            continue
        label = labels[0]
        kind_class = KIND_CLASS.get(label, KindClass.OTHER)
        owner = owners.get(key)
        name = node["name"]

        # Re-derive constructors: a Method whose name equals its enclosing type.
        kind_raw = label
        if label == "Method" and owner and owner.rsplit(".", 1)[-1] == name:
            kind_raw = "Constructor"

        decls.append(Decl(
            engine="tree-sitter",
            kind_raw=kind_raw,
            kind_class=kind_class,
            name=name,
            path=node["path"] or "",
            start_line=node["line_start"],
            owner=owner,
            arity=None,                     # never recorded by this engine
            external=label == "External" or bool(node.get("inferred")),
            raw={"uid": key, "labels": labels},
        ))
    return decls


def _read_edges(session, repo_id: str, nodes: dict[str, dict]) -> list[Edge]:
    edges: list[Edge] = []
    for record in session.run(
        """
        MATCH (a {repo_id: $repo})-[r]->(b {repo_id: $repo})
        WHERE a.key IS NOT NULL AND b.key IS NOT NULL
        RETURN a.key AS src, b.key AS dst, type(r) AS rel,
               b.name AS dst_name, labels(b) AS dst_labels,
               b.inferred AS dst_inferred, properties(r) AS props
        ORDER BY a.key, type(r), b.key
        """,
        repo=repo_id,
    ):
        rel = record["rel"]
        props = record["props"] or {}
        dst_external = "External" in (record["dst_labels"] or [])
        edges.append(Edge(
            engine="tree-sitter",
            edge_class=EDGE_CLASS.get(rel, EdgeClass.OTHER),
            rel_raw=rel,
            src_uid=record["src"],
            dst_uid=None if dst_external else record["dst"],
            dst_name=record["dst_name"],
            dst_external=record["dst_name"] if dst_external else None,
            path=props.get("file"),
            # Ordinary C# CALLS edges carry NO line: `add_edge` is called without
            # attrs. Only DI-derived edges have one. That absence is a result, so
            # it is left as None rather than filled in.
            line=props.get("line"),
            dispatch=None,
            resolution=None,
            # No resolved flag is persisted; the only observable proxy is whether
            # the target is a real node or an inferred External one.
            confident=not dst_external,
            raw=dict(props),
        ))
    return edges
